"""Throwaway: every place live silicon diverges from the recorded cold-boot writes.

The gate proves our control WRITES match the pcap byte-for-byte WHEN fed the recorded
reads. On real hardware the reads are live, so any write whose value is computed from
a read can diverge -- and that is the only way our chip can differ from the vendor's
(the gate covers everything else). This logs every register write our cold_bringup
emits on live silicon and aligns it against the pcap's recorded write stream; each
value mismatch is a live-read-dependent divergence, each (addr,width) desync is a
live-read-driven branch. The RX-breaking one is in this list.

The 0x4e0 ON-section mirror is excluded (it is emitted below writeN via ctrl_transfer
and is deterministic). Passive (cold init only, no TX).
    uv run python scripts/rtl8821cu_dkms/live_write_diff.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import libusb_package
import usb.core
import usb.util

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8821cu_dkms import bringup
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"
_MIRROR = 0x04E0


class LoggingTransport(Rtl8821cuTransport):
    """Captures every register write (addr, width, value) cold_bringup emits, in order.
    write8/16/32 funnel through writeN, so overriding writeN catches them all; the 0x4e0
    mirror is emitted in _mirror via ctrl_transfer (not writeN) so it is naturally excluded."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.writes: list[tuple[int, int, int]] = []

    def writeN(self, addr, data):
        data = bytes(data)
        self.writes.append((addr, len(data), int.from_bytes(data, "little")))
        super().writeN(addr, data)


_COLD_END_FRAME = 17019         # cold_bringup's last op (trace_cold.py); ignore operational writes


def _pcap_final_state():
    """Vendor's last-write-wins value per (addr,width) up to the cold_bringup boundary."""
    dev = rp.find_card_device(DEFAULT_CAP)
    ctrl = rp.extract_ctrl_ops(DEFAULT_CAP, dev)
    state: dict[int, tuple[int, int]] = {}      # addr -> (width, value)
    for o in ctrl:
        if (o["dir"] == "OUT" and o["wval"] != _MIRROR and o["data"]
                and o["frame"] <= _COLD_END_FRAME):
            state[o["wval"]] = (o["width"], int.from_bytes(o["data"], "little"))
    return state


def _classify(addr: int) -> str:
    if 0x0A00 <= addr <= 0x0AFF:
        return "CCK"
    if 0x0C00 <= addr <= 0x0CFF or 0x0E00 <= addr <= 0x0EFF or 0x0800 <= addr <= 0x08FF:
        return "BB/OFDM"
    if addr in (0x0040, 0x004C, 0x004E, 0x004F, 0x0064, 0x0065, 0x0066, 0x0067,
                0x0CB4, 0x0CB7, 0x0073, 0x1700, 0x1704):
        return "ANT/COEX"
    if addr in (0x0100, 0x0102, 0x0608, 0x06A0, 0x06A2, 0x06A4):
        return "MAC-RX"
    return ""


def _open():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    intf = next((i.bInterfaceNumber for i in dev.get_active_configuration()
                 if i.bInterfaceClass == _WIFI_INTF_CLASS), None)
    usb.util.claim_interface(dev, intf)
    return dev, intf


def main() -> int:
    pcap_state = _pcap_final_state()
    dev, intf = _open()
    t = LoggingTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up on live hardware (logging writes)...")
        bringup.cold_bringup(t)
        live_state: dict[int, tuple[int, int]] = {}
        for a, w, v in t.writes:
            live_state[a] = (w, v)
        print(f"[*] live final state: {len(live_state)} registers; "
              f"vendor cold final state: {len(pcap_state)} registers\n")

        addrs = sorted(set(live_state) | set(pcap_state))
        diffs = []
        for a in addrs:
            lv = live_state.get(a)
            pv = pcap_state.get(a)
            if lv != pv:
                diffs.append((a, lv, pv))
        print(f"{len(diffs)} registers differ in final cold state. RX-relevant first:\n")

        def _fmt(x):
            return f"0x{x[1]:0{x[0]*2}x}" if x else "(unwritten)"
        rx = [d for d in diffs if _classify(d[0])]
        other = [d for d in diffs if not _classify(d[0])]
        for a, lv, pv in rx:
            print(f"  [{_classify(a):8}] 0x{a:04x}: live={_fmt(lv):>12}  vendor={_fmt(pv):>12}")
        print(f"\n  ({len(other)} other non-RX-classified divergences: "
              f"{', '.join(f'0x{a:04x}' for a, _, _ in other[:40])}"
              f"{' ...' if len(other) > 40 else ''})")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
