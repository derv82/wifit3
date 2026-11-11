"""Replay-diff the rtl8192cu driver against the RTL8188CUS cold-boot capture, end to end.

The capture opens with USB-core enumeration (GET_DESCRIPTOR / SET_CONFIGURATION); those frames
carry no Realtek vendor request and are not driver ops. The window starts at the first vendor
op (rtl92c_read_chip_version's REG_SYS_CFG read) and runs to the last op. The endpoint
descriptors fed to _rtl_usb_init come from the capture's own configuration descriptor.

Two kernel contexts write the wire: mac80211's (probe, interface up, channel tunes, filter
updates) and the rtlwifi workqueue's watchdog tick (base.c:2036), which runs every 2 s and
interleaves with the first at register-op granularity. Each context replays in its own thread;
a scheduler grants the next capture op to whichever thread is blocked on exactly that op. A
watchdog tick is spawned when the next capture op is its opener, the HW_VAR_FWLPS_RF_ON read of
REG_RCR (hw.c:1416), which nothing else reads. The mac80211-side steps after interface up are
read off the capture by their first op: a REG_RCR write is a configure_filter update, a
RTXAGC_A_CCK1_MCS32 read is a channel tune (the channel from its RF_CHNLBW write).

Run: uv run python scripts/chips/rtl8192cu/verify_pcap.py [capture-2]
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8192cu import SUPPORTED_IDS
from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu.base import rtl_watchdog_wq_callback
from wifit3.chips.rtl8192cu.core import rtl_op_configure_filter
from wifit3.chips.rtl8192cu.driver import MONITOR_FILTER_FLAGS, RTL8192CUDriver
from wifit3.chips.rtl8192cu.priv import RtlPriv
from wifit3.chips.rtl8192cu.usb import UsbEndpointDescriptor

CAP_DIR = REPO / "driver_captures" / "captures_rtl8192cu_caponly"
RF_ADDR_SHIFT = 20  # _rtl92c_phy_rf_serial_write: data_and_addr = (offset << 20) | data


class Aborted(Exception):
    """Raised in a replay thread when the scheduler stops the run."""


class Worker:
    def __init__(self, name: str, body: Callable[[], None]) -> None:
        self.name = name
        self.body = body
        self.pending: dict | None = None
        self.result: int | None = None
        self.error: BaseException | None = None
        self.done = False
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def _run(self) -> None:
        try:
            self.body()
        except BaseException as error:  # noqa: BLE001 - reported by the scheduler
            self.error = error
        finally:
            with SCHED.cond:
                self.done = True
                SCHED.cond.notify_all()


class Scheduler:
    """Grants capture ops, in order, to the worker blocked on each one."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.ops: list[dict] = []
        self.i = 0
        self.workers: list[Worker] = []
        self.ticks = 0
        self.failure: str | None = None
        self.tick_body: Callable[[], None] | None = None

    def request(self, kind: str, addr: int, width: int, value: int | None) -> int:
        worker = next(w for w in self.workers if w.thread is threading.current_thread())
        with self.cond:
            worker.pending = {"kind": kind, "addr": addr, "width": width, "value": value}
            self.cond.notify_all()
            while worker.pending is not None and self.failure is None:
                self.cond.wait()
            if self.failure is not None:
                raise Aborted(self.failure)
            return worker.result or 0

    def _settled(self) -> bool:
        return all(w.done or w.pending is not None for w in self.workers)

    @staticmethod
    def _matches(req: dict, op: dict) -> bool:
        if req["kind"] != op["kind"] or req["addr"] != op["addr"] or req["width"] != op["width"]:
            return False
        return req["kind"] == "R" or req["value"] == op["value"]

    def run(self, main: Worker) -> None:
        self.workers.append(main)
        main.thread.start()
        with self.cond:
            while True:
                while not self._settled():
                    self.cond.wait()
                errors = [w for w in self.workers if w.error is not None and not isinstance(w.error, Aborted)]
                if errors:
                    self._fail(f"{errors[0].name} raised {errors[0].error!r}")
                    return
                live = [w for w in self.workers if not w.done]
                if self.i >= len(self.ops):
                    if live:
                        self._fail("port emitted " + "; ".join(f"{w.name} {_fmt_req(w.pending)}" for w in live)
                                   + " past the end of the capture")
                    return
                op = self.ops[self.i]
                worker = next((w for w in live if self._matches(w.pending, op)), None)
                if worker is not None:
                    worker.result = op.get("value") if op["kind"] == "R" else None
                    worker.pending = None
                    self.i += 1
                    self.cond.notify_all()
                    continue
                if not live and not (op["kind"] == "R" and op["addr"] == C.REG_RCR):
                    return
                if op["kind"] == "R" and op["addr"] == C.REG_RCR and not any(w.name.startswith("tick") for w in live):
                    self.ticks += 1
                    tick = Worker(f"tick{self.ticks}", self.tick_body)
                    self.workers.append(tick)
                    tick.thread.start()
                    continue
                self._fail(f"op#{self.i} frame {op['frame']}: capture has {rp.ReplayTransport._fmt(op)}; "
                           + "; ".join(f"{w.name} wants {_fmt_req(w.pending)}" for w in live))
                return

    def _fail(self, message: str) -> None:
        self.failure = message
        self.cond.notify_all()


def _fmt_req(req: dict | None) -> str:
    if req is None:
        return "nothing"
    if req["kind"] == "R":
        return f"R 0x{req['addr']:04x}/{req['width']}"
    return f"W 0x{req['addr']:04x}/{req['width']}=0x{req['value']:0{req['width'] * 2}x}"


SCHED = Scheduler()


class ScheduledIo:
    """rtlpriv->io over the scheduler; rtl_write_chunk is the wide control write."""

    def read8(self, addr: int) -> int:
        return SCHED.request("R", addr, 1, None)

    def read16(self, addr: int) -> int:
        return SCHED.request("R", addr, 2, None)

    def read32(self, addr: int) -> int:
        return SCHED.request("R", addr, 4, None)

    def write8(self, addr: int, value: int) -> None:
        SCHED.request("W", addr, 1, value & 0xFF)

    def write16(self, addr: int, value: int) -> None:
        SCHED.request("W", addr, 2, value & 0xFFFF)

    def write32(self, addr: int, value: int) -> None:
        SCHED.request("W", addr, 4, value & 0xFFFFFFFF)

    def write_chunk(self, addr: int, data: bytes) -> None:
        SCHED.request("W", addr, len(data), int.from_bytes(bytes(data), "little"))


def _endpoints(pcap: Path, dev: int) -> tuple[list[UsbEndpointDescriptor], bool]:
    """The card's endpoint descriptors from its recorded configuration descriptor."""
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y", f"usb.device_address=={dev} && usb.bEndpointAddress",
         "-T", "fields", "-e", "usb.bEndpointAddress", "-e", "usb.bmAttributes", "-e", "usb.wMaxPacketSize"],
        capture_output=True, text=True, check=True).stdout
    line = next(row for row in out.splitlines() if row.strip())
    addrs, attrs, sizes = (col.split(",") for col in line.split("\t")[:3])
    endpoints = [UsbEndpointDescriptor(int(a, 0), int(b, 0)) for a, b in zip(addrs, attrs)]
    is_high_speed = any(int(s, 0) == C.USB_HIGH_SPEED_BULK_SIZE for s in sizes)
    return endpoints, is_high_speed


def _mac80211_steps(ops: list[dict], start: int) -> list[tuple[str, int]]:
    """The mac80211-side calls after hw_init, in capture order, keyed by their first op."""
    steps: list[tuple[str, int]] = []
    for i in range(start, len(ops)):
        op = ops[i]
        if op["kind"] == "W" and op["addr"] == C.REG_RCR:
            steps.append(("configure_filter", 0))
        elif op["kind"] == "R" and op["addr"] == C.RTXAGC_A_CCK1_MCS32:
            rf18 = next(o for o in ops[i:] if o["kind"] == "W" and o["addr"] == C.RFPGA0_XA_LSSIPARAMETER
                        and (o["value"] >> RF_ADDR_SHIFT) & 0xFF == C.RF_CHNLBW)
            steps.append(("tune", rf18["value"] & 0xFF))
    return steps


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None
    name = Path(cap or "capture-2").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev)
    endpoints, is_high_speed = _endpoints(pcap, dev)
    hw_init_start = next(i for i, o in enumerate(ops) if o["kind"] == "R" and o["addr"] == C.REG_APS_FSMCO)
    after_hw_init = next(i for i, o in enumerate(ops) if o["kind"] == "R" and o["addr"] == C.REG_RCR)
    steps = _mac80211_steps(ops, after_hw_init)
    if steps[:2] != [("configure_filter", 0), ("tune", 1)]:
        print(f"FAIL: interface up is not configure_filter + tune(1): {steps[:2]}")
        return 1
    print(f"{name}: card=dev{dev}, {len(ops)} driver ops; USB-core enumeration precedes frame "
          f"{ops[0]['frame']}")
    print(f"  probe ops 0..{hw_init_start - 1}, rtl92cu_hw_init {hw_init_start}..{after_hw_init - 1}, "
          f"monitor + hops + watchdog {after_hw_init}..{len(ops) - 1}; endpoints "
          f"{[hex(e.bEndpointAddress) for e in endpoints]}, high-speed={is_high_speed}")

    entry = next(d for d in SUPPORTED_IDS if (d.vid, d.pid) == (0x0BDA, 0x8176))
    driver = RTL8192CUDriver(MagicMock(), entry)
    driver.priv = rtlpriv = RtlPriv(ScheduledIo())
    driver._endpoints = endpoints
    driver._is_high_speed = is_high_speed
    tunes = [ch for kind, ch in steps[2:] if kind == "tune"]

    def mac80211_context() -> None:
        driver._bringup()
        for kind, channel in steps[2:]:
            if kind == "tune":
                driver._tune(channel)
            else:
                rtl_op_configure_filter(rtlpriv, C.FIF_ALLMULTI, MONITOR_FILTER_FLAGS)

    SCHED.ops = ops
    SCHED.tick_body = lambda: rtl_watchdog_wq_callback(rtlpriv)
    SCHED.run(Worker("mac80211", mac80211_context))
    if SCHED.failure is not None:
        print(f"  FAIL at {SCHED.i}/{len(ops)}:\n    {SCHED.failure}")
        return 1
    if SCHED.i != len(ops):
        print(f"  FAIL: replay stopped at {SCHED.i}/{len(ops)}; next capture op "
              f"{rp.ReplayTransport._fmt(ops[SCHED.i])} (frame {ops[SCHED.i]['frame']})")
        return 1

    hal, efuse, usb, phy, dm = rtlpriv.rtlhal, rtlpriv.efuse, rtlpriv.usb, rtlpriv.phy, rtlpriv.dm
    print(f"  PASS: {SCHED.i}/{len(ops)} ops byte-for-byte")
    print(f"      version=0x{hal.version:02x} rf_type={phy.rf_type} board_type={efuse.board_type} "
          f"autoload_fail={efuse.autoload_failflag} efuse_used={efuse.efuse_usedbytes}B "
          f"eeprom_did=0x{efuse.eeprom_did:04x} oem_id={hal.oem_id} fw={hal.fw_name}")
    print(f"      out_eps={usb.out_eps[:usb.out_ep_nums]} out_queue_sel=0x{usb.out_queue_sel:x} "
          f"ep_mapping={usb.ep_mapping} fw v{hal.fw_version}.{hal.fw_subversion}")
    print(f"      rx_conf=0x{rtlpriv.mac80211.rx_conf:08x}, {len(tunes) + 1} tunes (last ch {phy.current_channel}), "
          f"{SCHED.ticks} watchdog ticks, thermal 0x{dm.thermalvalue:02x} lck 0x{dm.thermalvalue_lck:02x}")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
