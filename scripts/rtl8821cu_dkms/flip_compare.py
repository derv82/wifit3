"""Throwaway: does reaching 2.4 GHz via a 5 GHz->ch1 FLIP differ from cold-direct-ch1?

User question: 5 GHz works, 2.4 GHz fails -- would flipping 5 GHz -> ch1 (instead of
the cold init's first-ever direct ch1 tune) land the 2.4 GHz RF path in a different,
maybe-working state? This answers it deterministically (no 2.4 GHz signal needed):
snapshot the 2.4 GHz RX-path RF+BB registers reached cold-direct, then flip
ch1->5GHz->ch1 and snapshot again, and diff. Identical => the flip cannot help (same
2.4 GHz config); different => the differing registers are the 2.4 GHz lead.

Passive.  uv run python -u scripts/rtl8821cu_dkms/flip_compare.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, btc, chan
from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF

_RF_REGS = [0x00, 0x18, 0x08, 0x33, 0x3E, 0x55, 0x5F, 0x65, 0xB2, 0xCA, 0xCC, 0xDF, 0xEF]
_BB_REGS = [0x0808, 0x0A04, 0x0A07, 0x0860, 0x0838, 0x082C, 0x090C, 0x0C10, 0x0C14,
            0x0C1C, 0x0C50, 0x0CB4, 0x0CB7, 0x004E, 0x0E70, 0x0A24, 0x0A28, 0x0AAC]


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


def _snapshot(t) -> dict:
    s = {}
    for r in _RF_REGS:
        s[("RF", r)] = read_rf(t, r) & 0xFFFFF
    for r in _BB_REGS:
        s[("BB", r)] = t.read32(r)
    s[("LTE", 0x38)] = btc._read_indirect(t, 0x38)
    return s


def main() -> int:
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up (lands on ch1 = 2.4 GHz direct)...", flush=True)
        info = bringup.cold_bringup(t)
        chan.set_channel(t, info, 1)            # ensure clean ch1 tune
        a = _snapshot(t)
        print("[*] flip: ch1 -> ch149 (5 GHz) -> ch1 (2.4 GHz)...", flush=True)
        chan.set_channel(t, info, 149)
        chan.set_channel(t, info, 1)
        b = _snapshot(t)

        diffs = [(k, a[k], b[k]) for k in a if a[k] != b[k]]
        print(f"\n2.4 GHz RX-path state: cold-direct-ch1 vs 5GHz-flip-ch1")
        if not diffs:
            print("  IDENTICAL across all probed RF+BB+LTE registers.")
            print("  => the flip lands the 2.4 GHz path in the same state => it would NOT fix 2.4 GHz.")
        else:
            print(f"  {len(diffs)} registers differ:")
            for (kind, r), va, vb in diffs:
                w = 5 if kind == "RF" else 8
                print(f"   {kind} 0x{r:03x}: direct=0x{va:0{w}x}  flip=0x{vb:0{w}x}")
            print("  => the flip path configures these differently -- a 2.4 GHz lead.")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
