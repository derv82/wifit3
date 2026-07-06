"""RTL8812AU DKMS — record the REAL register-write sequence our bring-up emits on
live hardware, in the morrownr-pcap-oracle line format, so ``diff_trace.py`` can
locate exactly where our port diverges from the working vendor driver
(``ref/morrownr_capture2_bringup.txt``, produced by ``pcap_regtrace.py``).

One passive HW bring-up cycle — control transfers + monitor RX only, no 802.11 TX.
Mirrors ``test_hw.py --phase beacon`` step-for-step, wrapping the transport so every
``writeN`` is logged. Writes ``ref/ourport_bringup.txt`` and prints a short RX baseline.

    uv run python scripts/rtl8812au_dkms/trace_bringup.py [--channel 1] [--rx-secs 8]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl88xxau_base import registers as R
from wifit3.chips.rtl88xxau_base.transport import Rtl88xxauTransport
from wifit3.chips.rtl8812au_dkms import bb, chan, dig, efuse, firmware, mac, monitor, rf, rx, txpower
from wifit3.chips.rtl8812au_dkms.constants import USB_PID_AWUS036ACH, USB_VID_REALTEK
from wifit3.wlan.packet import WlanFrameParser

OUT = Path(__file__).parent / "ref" / "ourport_bringup.txt"


class RecordingTransport(Rtl88xxauTransport):
    """Transport that records every vendor write (addr, bytes) in order, plus phase
    markers, then replays it to real hardware unchanged."""

    def __init__(self, dev: usb.core.Device):
        super().__init__(dev)
        self.log: list = []   # entries: (addr:int, data:bytes) | ("MARK", name:str)

    def mark(self, name: str) -> None:
        self.log.append(("MARK", name))

    def writeN(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        self.log.append((addr & 0xFFFF, data))
        super().writeN(addr, data)


def _open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID_REALTEK, idProduct=USB_PID_AWUS036ACH, backend=backend)
    if dev is None:
        print(f"[FAIL] AWUS036ACH not found ({USB_VID_REALTEK:04x}:{USB_PID_AWUS036ACH:04x}). "
              "Plug it in, confirm Zadig bound it to WinUSB.")
        return None
    print(f"[*] Found AWUS036ACH at bus {dev.bus}, address {dev.address}")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)
    return dev


def _dump(log: list) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    fw_pages = 0
    for entry in log:
        if entry[0] == "MARK":
            lines.append(f"# --- {entry[1]} ---")
            continue
        addr, data = entry
        if len(data) > 4:           # FW page / bulk EFUSE block — not a config register
            fw_pages += 1
            continue
        val = int.from_bytes(data, "little")
        line = f"BB 0x{addr:04X}=0x{val:0{len(data) * 2}X}"
        if addr in (0x0C90, 0x0E90):
            p = "A" if addr == 0x0C90 else "B"
            line += f"  -> RF[{p}] 0x{(val >> 20) & 0xFF:02X}=0x{val & 0xFFFFF:05X}"
        lines.append(line)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"[*] wrote {len(lines)} trace lines ({fw_pages} FW/bulk page-writes omitted) -> {OUT}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--rx-secs", type=float, default=8.0)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    dev = _open_device()
    if dev is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        print(f"[FAIL] claim_interface(0): {e}  (a running wifit3 may hold the card)")
        return 1

    t = RecordingTransport(dev)
    try:
        sys_cfg = t.read32(R.REG_SYS_CFG)
        print(f"  REG_SYS_CFG (0xF0) = 0x{sys_cfg:08x}")
        if sys_cfg in (0, 0xFFFFFFFF):
            print("[FAIL] implausible REG_SYS_CFG — unplug 5s, replug, rerun.")
            return 1

        t.mark("efuse")
        params = efuse.read_chip_params(t)
        jp = efuse.build_jaguar_params(params, sys_cfg)
        print(f"  EFUSE rfe_type={params.rfe_type} mac={params.mac_address} "
              f"crystal_cap=0x{params.crystal_cap:02x}")
        print(f"  phy_cond params: board_type=0x{jp.board_type:02x} cut_version={jp.cut_version}")

        t.mark("fw_download")
        fw = firmware.load_firmware_blob()
        if not firmware.bring_up(t, fw):
            print("[FAIL] FW bring_up did not reach ready.")
            return 1

        t.mark("mac_init")
        mac.phy_mac_config(t)
        mac.mac_init_misc(t)

        t.mark("bb_rf_init")
        bb.phy_bb_config(t, crystal_cap=params.crystal_cap, params=jp)
        rf.phy_rf_config(t, params=jp)

        t.mark("chan_tune")
        chan.set_chnl_bw(t, ch=args.channel, bb_swing_2g_a=params.bb_swing_2g[0],
                         bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type)

        t.mark("txpower")
        txpower.set_tx_power(t, args.channel, params.tx_power_2g)

        t.mark("hal_init_pre")
        mac.hal_init_misc_pre(t)
        t.mark("init_hal_dm")
        dig.init_hal_dm(t, search_edcca=False)
        t.mark("hal_init_post")
        mac.hal_init_misc_post(t)
        t.mark("enter_monitor")
        monitor.enter_monitor(t)
        t.mark("rx_loop")
        _dump(t.log)

        # Passive RX baseline — confirm current (expected-deaf) state in the same cycle.
        beacons: Counter = Counter()
        frames = 0
        start = time.monotonic()
        while time.monotonic() - start < args.rx_secs:
            buf = t.bulk_in()
            if not buf:
                continue
            for frame, r in rx.iter_frames(buf):
                frames += 1
                parsed = WlanFrameParser.parse_80211_frame(frame, r)
                if parsed and parsed.type == "beacon":
                    b = (parsed.bssid or "").lower()
                    if b and b != "ff:ff:ff:ff:ff:ff":
                        beacons[b] += 1
        print(f"[RX baseline] ch{args.channel}: {len(beacons)} APs, {sum(beacons.values())} beacons, "
              f"{frames} frames in {args.rx_secs:g}s")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")


if __name__ == "__main__":
    sys.exit(main())
