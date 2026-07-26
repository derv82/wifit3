"""Single-cursor verify_pcap for the mt76-USB connac family (MT7921AU; the template the
MT7925U port copies). Drives the port's real load_firmware, post_boot_init, and the
operational tail over one cursor via mt76_verify_replay.py, and reports coverage plus every
named waiver. Run: uv run python scripts/mt76_verify_pcap.py [<pcap>] [--verbose].
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import mt76_verify_replay as E  # noqa: E402
import wifit3.chips.mt7921au as mt_pkg  # noqa: E402
from wifit3.chips.mt7921au import init as mt_init  # noqa: E402
from wifit3.chips.mt7921au import mac as mt_mac  # noqa: E402
from wifit3.chips.mt7921au import mcu as mt_mcu  # noqa: E402
from wifit3.chips.mt7921au import rx as mt_rx  # noqa: E402
from wifit3.chips.mt7921au import tx as mt_tx  # noqa: E402
from wifit3.chips.mt7921au.constants import (  # noqa: E402
    MT_MIB_SDR9, MT_MIB_SDR3, MT_SDIO_TXD_SIZE, MT_TXD3_REM_TX_COUNT, MT_TXD3_NO_ACK,
    EP_OUT_MCU, EP_OUT_FW,
)
from wifit3.chips.mt7921au.firmware import MT7921AUFirmwareLoader  # noqa: E402
from wifit3.chips.mt7921au.transport import MT7921AUTransport  # noqa: E402

DEFAULT_CAP = "usb_dumps_new/captures_mt7921u_pau0f-no-adapter-scatter/capture-3.pcap"
ASSETS = Path(mt_pkg.__file__).parent / "assets"

# First register read of each periodic mac_work sub-sequence, so its burst is recognisable.
_SURVEY_FIRST = MT_MIB_SDR9(0)      # mt792x_phy_update_channel busy_time read
_MIB_FIRST = MT_MIB_SDR3(0)         # mt792x_mac_update_mib_stats fcs_err read
_TXD3_OFF = 4 + 3 * 4               # SDIO/USB header (4) + connac2 TXD word 3
_TXD3_ACKCFG = MT_TXD3_REM_TX_COUNT | MT_TXD3_NO_ACK
_EP_TX_HCCA = 0x09                  # mgmt TX endpoint (aireplay -0 deauth path)


def waivers() -> E.WaiverSet:
    """The waivers for the mt7921au pau0f capture, each named and counted in the report."""
    return E.WaiverSet(
        E.Waiver(
            "USB enumeration",
            "GET_DESCRIPTOR / SET_ADDRESS / SET_CONFIGURATION and friends: standard-type "
            "control requests usbcore issues while enumerating. The wifi driver never emits them.",
            match=lambda op: op.cls == "ctrl" and op.reqtype == "standard",
        ),
        E.Waiver(
            "Linux-probe chip-rev read (0x70010204)",
            "The Linux mt7921 probe reads the chip revision at 0x70010204 after the chip-id at "
            "0x70010200. The port reads only the id, so this read is on the wire but not emitted.",
            match=lambda op: op.cls == "ctrl" and op.is_in and op.addr == 0x70010204,
        ),
        E.Waiver(
            "boot-status poll the port adds (0x00300000)",
            "Between the ROM patch and RAM upload the port issues two 64-byte boot-status reads "
            "(bRequest 0x01, wValue 0x30) per firmware.py:156. The pau0f wire never did them and "
            "the result is discarded. Port-emitted, no wire op.",
            extra=lambda p: (not p.is_bulk and p.is_in and p.breq == 0x01
                             and p.addr == 0x00300000),
        ),
        E.Waiver(
            "UHW-bus to unified-bus (WinUSB compat)",
            "Linux reaches MT_SSUSB_EPCTL_CSR_EP_RST_OPT (0x74011890) over the UHW bus (bRequest "
            "0x01/0x02). UHW returns Errno 5 on WinUSB, so the port reaches the same register "
            "with the same value over the unified bus (0x63/0x66). See firmware.py:_epctl_rst_opt.",
            sub=lambda p, w: (
                not p.is_bulk and w.cls == "ctrl" and p.addr == w.addr
                and ((p.is_in and w.is_in and p.breq == 0x63 and w.breq == 0x01)
                     or (not p.is_in and not w.is_in and p.breq == 0x66 and w.breq == 0x02
                         and p.payload == w.data))),
        ),
        E.Waiver(
            "TX NO_ACK bit (inject requests ACK)",
            "The port's inject clears connac2 TXD3 NO_ACK so the chip retries until the peer "
            "ACKs; the aireplay-ng reference set NO_ACK. REM_TX_COUNT matches (15). The frame is "
            "byte-identical to the wire except the TXD3 ACK-config field.",
            sub=lambda p, w: (
                p.is_bulk and w.cls == "bulk" and p.ep == w.ep and p.ep in (EP_OUT_FW, _EP_TX_HCCA)
                and _mask_txd3(p.payload) == _mask_txd3(w.data)),
        ),
        E.Waiver(
            "aireplay RTS injection-test frame (control, <24B)",
            "aireplay-ng --test emits short 802.11 control frames (RTS, 16 bytes) as a link "
            "probe. tx.build_tx builds management/data frames only (>=24B), never RTS, so these "
            "wire TX frames have no port counterpart.",
            match=lambda op: (op.cls == "bulk" and op.ep in (EP_OUT_FW, _EP_TX_HCCA)
                              and _is_ctrl_tx(op.data)),
        ),
        # Books zero on the single-function pau0f capture. It keeps a composite WiFi+BT
        # capture's btusb traffic named rather than dropped, since btusb shares the devnum.
        E.Waiver(
            "btusb boot-status poll (composite BT-coex)",
            "bRequest 0x01 wValue 0x30 64-byte reads from the btusb function on composite "
            "WiFi+BT adapters sharing this devnum. Another driver's traffic, not a wifi register op.",
            match=lambda op: op.cls == "ctrl" and op.is_in and op.breq == 0x01 and op.wval == 0x30,
        ),
    )


def _mask_txd3(buf: bytes) -> bytes:
    """Zero the connac2 TXD3 REM_TX_COUNT / NO_ACK bits so a TX compare ignores only the
    ACK-config field (every other byte must still match)."""
    if len(buf) < _TXD3_OFF + 4:
        return buf
    b = bytearray(buf)
    w = int.from_bytes(b[_TXD3_OFF:_TXD3_OFF + 4], "little") & ~_TXD3_ACKCFG
    b[_TXD3_OFF:_TXD3_OFF + 4] = w.to_bytes(4, "little")
    return bytes(b)


# Operational-phase dispatch: recognise each burst from the wire and drive the real routine
# that emits it (mac_work MIB, monitor/hop MCU, TX descriptor).

def _decode_operational_mcu(f: bytes):
    """A captured operational MCU frame to (cmd, payload) for the real builder, params read
    off the wire. None if unrecognised (a frontier)."""
    std = len(f) > 39 and f[38] == 0x00 and f[39] == 0x80
    if not std:                                          # UNI command
        cid = f[38] | (f[39] << 8)
        if cid == mt_mcu.UNI_CMD_SNIFFER:
            band_idx = f[52]
            tag = f[56] | (f[57] << 8)
            if tag == 0:
                return mt_mcu.set_sniffer(bool(f[60]), band_idx)
            if tag == 1:
                return mt_mcu.config_sniffer(f[64], band_idx)
        return None
    cid = f[40]                                          # STD command id
    if cid == mt_mcu.CE_CMD_SET_RX_FILTER:
        fif = struct.unpack_from("<I", f, 76)[0]
        bit_map = struct.unpack_from("<I", f, 80)[0]
        return mt_mcu.set_rxfilter(fif, f[84], bit_map)
    if cid == mt_mcu.CE_CMD_SET_BSS_ABORT:
        return mt_mcu.set_bss_abort()
    if cid == mt_mcu.CE_CMD_CHIP_CONFIG:
        idx = f.find(b"KeepFullPwr ")
        return mt_mcu.set_deep_sleep(idx >= 0 and f[idx + 12:idx + 13] == b"0")
    return None


def _sniffer_channel(f: bytes):
    """The control channel a config_sniffer (UNI SNIFFER, tlv tag 1) tunes to, else None."""
    if len(f) < 65 or (f[38] == 0x00 and f[39] == 0x80):
        return None
    if (f[38] | (f[39] << 8)) != mt_mcu.UNI_CMD_SNIFFER:
        return None
    if (f[56] | (f[57] << 8)) != 1:
        return None
    return f[64]


def _tx_frame(data: bytes):
    """The 802.11 frame inside a captured TX bulk-OUT ([4B len][SDIO TXD][frame]), or None
    if it is not a management/data TX (e.g. a stray FW-path write)."""
    if len(data) < 4 + MT_SDIO_TXD_SIZE:
        return None
    framelen = int.from_bytes(data[0:4], "little") - MT_SDIO_TXD_SIZE
    off = 4 + MT_SDIO_TXD_SIZE
    if framelen < 24 or off + framelen > len(data):
        return None
    return data[off:off + framelen]


def _is_ctrl_tx(data: bytes) -> bool:
    """True for a short 802.11 control-frame TX (framelen 10..23). The framelen<24 window
    excludes FW-scatter chunks (length field in the thousands), so it matches only aireplay RTS."""
    off = 4 + MT_SDIO_TXD_SIZE
    if len(data) <= off:
        return False
    framelen = int.from_bytes(data[0:4], "little") - MT_SDIO_TXD_SIZE
    if not (10 <= framelen < 24):
        return False
    return ((data[off] >> 2) & 0x3) == 1                 # FC type == control


# Offline neutralization: the port's real bring-up awaits acks and sleeps for settle time.
# Offline the acks come from the wire and the sleeps collapse to nothing.

class _WireMcuQueue:
    """Stand-in for transport._mcu_rx_queue, serving MCU responses from the wire. Seq-aware:
    `get` returns the earliest unconsumed response carrying `expected_seq` (rxd seq at offset
    29), so the port always gets its own ack even when the mt76 core's acks are interleaved.
    Response content is input the port consumes, not output verified against it."""

    def __init__(self, responses: list[bytes]):
        self._all = [r for r in responses if mt_rx.classify(r) == "mcu"]
        self._used = [False] * len(self._all)
        self.expected_seq: int | None = None

    def _find(self) -> int:
        for i, (r, used) in enumerate(zip(self._all, self._used)):
            if used or len(r) <= 29:
                continue
            if self.expected_seq is None or r[29] == self.expected_seq:
                return i
        return -1

    def empty(self) -> bool:
        return self._find() < 0

    def qsize(self) -> int:
        return sum(1 for u in self._used if not u)

    def get_nowait(self):
        i = self._find()
        if i < 0:
            raise asyncio.QueueEmpty
        self._used[i] = True
        return self._all[i]

    async def get(self):
        # No matching-seq response left: raise TimeoutError, which send_mcu_command's wait_for
        # reads as a benign ack timeout (returns None) instead of hanging the walk.
        i = self._find()
        if i < 0:
            raise asyncio.TimeoutError
        self._used[i] = True
        return self._all[i]

    def put_nowait(self, x):
        pass


def _wire_next_mcu_seq(dev: E.ReplayDevice) -> int:
    """Serve the connac2 MCU seq from the wire (the next EP-0x08 frame's seq byte at offset
    43). It is not offline-reproducible: the mt76 core interleaves messages the port skips."""
    j = dev.i
    while j < len(dev.ops):
        op = dev.ops[j]
        if op.cls == "bulk" and op.ep == 0x08 and len(op.data) > 43:
            return op.data[43]
        j += 1
    return 1


def _make_transport(dev: E.ReplayDevice, queue: "_WireMcuQueue"):
    """A real MT7921AUTransport over the ReplayDevice, with the RX reader thread replaced by
    the shared wire-fed `queue` and the MCU seq served from the wire."""
    t = MT7921AUTransport(dev)
    t._mcu_rx_queue = queue
    t.start_rx = lambda: None

    def _seq() -> int:
        s = _wire_next_mcu_seq(dev)
        queue.expected_seq = s           # tell the queue which ack the port will await
        return s
    t._next_mcu_seq = _seq
    return t


# Phase drivers: real port code driven over the cursor.

async def _drive_firmware(walk: E.Walk, dev: E.ReplayDevice, state: dict):
    """Run the port's load_firmware over the cursor (chip-id, power-on, dma_init, ROM-patch
    and WM-RAM upload with FW_SCATTER, FW_START). Only the vendor-interface claim is faked."""
    t = _make_transport(dev, state["q"])
    loader = MT7921AUFirmwareLoader(t, ASSETS)
    loader._claim_vendor_interface = lambda *a, **k: 0     # no live USB to claim
    return await loader.load_firmware()


async def _drive_postboot(walk: E.Walk, dev: E.ReplayDevice, state: dict):
    """Run the port's post_boot_init over the cursor (firmware-run tail, hw init, regd and
    mac_start, monitor entry)."""
    t = _make_transport(dev, state["q"])
    return await mt_init.post_boot_init(t)


async def _send_op_mcu(dev: E.ReplayDevice, q: "_WireMcuQueue", disp):
    """Send one decoded operational MCU command through the real builder and transport, frame
    asserted at the cursor. wait_resp=False: only the output frame is checked, not the ack."""
    cmd, payload = disp
    await _make_transport(dev, q).send_mcu_command(cmd, payload, wait_resp=False)


def _drive_tx(dev: E.ReplayDevice, frame: bytes, band_5ghz: bool):
    """Rebuild a captured injection via tx.build_tx and assert the bulk-OUT bytes and endpoint
    at the cursor."""
    built, out_ep = mt_tx.build_tx(frame, band_5ghz=band_5ghz)
    dev.write(out_ep, built)


async def _drive_operational(walk: E.Walk, state: dict):
    """Dispatch each operational burst to the real routine that emits it (mac_work MIB cycle,
    monitor/hop MCU, or TX inject); an unrecognised opener stops the walk at the frontier."""
    q = state["q"]
    band5 = [False]

    def _drain_tx(dev: E.ReplayDevice):
        """Interleave hook: aireplay TX splices bulk-OUT frames into a mid-flight MIB cycle.
        Drain each through tx.build_tx so the MIB reads resume aligned."""
        while True:
            op = dev._next_matchable()
            if op is None or not (op.cls == "bulk" and op.ep in (EP_OUT_FW, _EP_TX_HCCA)):
                return
            frame = _tx_frame(op.data)
            if frame is None:
                return                                   # control/RTS TX: left to its waiver
            _drive_tx(dev, frame, band5[0])

    while not walk.done():
        op = walk.peek_matchable()
        if op is None:
            break
        if op.cls == "ctrl" and op.is_in and op.addr == _SURVEY_FIRST:
            walk.run(lambda dev: mt_mac.update_survey(_make_transport(dev, q)),
                     "mac.update_survey", async_interleave=_drain_tx)
        elif op.cls == "ctrl" and op.is_in and op.addr == _MIB_FIRST:
            walk.run(lambda dev: mt_mac.update_mib_stats(_make_transport(dev, q)),
                     "mac.update_mib_stats", async_interleave=_drain_tx)
        elif op.cls == "bulk" and op.ep == EP_OUT_MCU:
            disp = _decode_operational_mcu(op.data)
            if disp is None:
                break                                    # unknown MCU command: frontier
            ch = _sniffer_channel(op.data)
            if ch is not None:
                band5[0] = ch > 14
            await walk.run_async(lambda dev, d=disp: _send_op_mcu(dev, q, d),
                                 "operational.monitor/hop MCU")
        elif op.cls == "bulk" and op.ep in (EP_OUT_FW, _EP_TX_HCCA):
            frame = _tx_frame(op.data)
            if frame is None:
                break                                    # not a management/data TX: frontier
            walk.run(lambda dev, f=frame: _drive_tx(dev, f, band5[0]), "tx.build_tx (inject)")
        else:
            break                                        # unknown burst opener: frontier


async def _run_bringup(walk: E.Walk, state: dict):
    """Drive the bring-up phases in wire order over one cursor, sharing a single seq-aware
    ack queue across phases so each device ack is consumed once."""
    state["q"] = _WireMcuQueue(walk.cap.responses)
    await walk.run_async(lambda dev: _drive_firmware(walk, dev, state), "firmware.load_firmware")
    await walk.run_async(lambda dev: _drive_postboot(walk, dev, state), "init.post_boot_init")
    await _drive_operational(walk, state)


def run(cap: str | None = None, verbose: bool = False) -> int:
    # The port logs its bring-up at INFO; silence it so the report stands alone. --verbose
    # shows the driver's own narration alongside the report.
    if not verbose:
        logging.getLogger("wifit3").setLevel(logging.CRITICAL)
    # Offline replay needs no settle time; collapse every sleep to nothing.
    _real_sleep, time.sleep = time.sleep, lambda *a, **k: None
    _real_asleep = asyncio.sleep

    async def _fast_sleep(delay, *a, **k):
        return await _real_asleep(0)
    asyncio.sleep = _fast_sleep
    try:
        return _run(cap)
    finally:
        time.sleep = _real_sleep
        asyncio.sleep = _real_asleep


def _run(cap: str | None) -> int:
    path = cap or DEFAULT_CAP
    if not Path(path).exists():
        print(f"FAIL: no such capture {path}")
        return 1
    pkts = E.parse_pcapng(path)
    dev = E.busiest_vendor_devnum(pkts)
    if dev is None:
        print(f"FAIL: no vendor-control device found in {path}")
        return 1
    capture = E.extract(pkts, dev)
    walk = E.Walk(capture, waivers=waivers())    # waivers consumed inline as the cursor runs
    state: dict = {}

    title = f"mt76 verify (mt7921au) · {Path(path).name}"
    print(f"{title}: dev{dev}, {len(capture.ops)} host-to-device ops, "
          f"{len(capture.responses)} responses")

    try:
        asyncio.run(_run_bringup(walk, state))
    except E.Divergence:
        pass  # frontier recorded; fall through to the report
    except Exception as e:  # noqa: BLE001
        # A harness/port error, not a wire divergence. Still print what reproduced.
        print(f"\n[harness] bring-up raised {type(e).__name__}: {e}")

    return walk.report(title)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--verbose"]
    verbose = "--verbose" in sys.argv[1:]
    return run(args[0] if args else None, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main())
