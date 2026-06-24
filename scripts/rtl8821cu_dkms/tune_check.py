"""Throwaway: dump the per-channel 2.4 GHz tune state — why do ch9-11 go silent?

In every scan, 2.4 GHz ch1-8 produce ~450 noise units at full gain but ch9/10/11
produce ~1 (before AND after plugging in an AP) -- a channel-tune artifact, not signal.
This reads the tune-critical registers across 2.4 GHz channels so a mistune at ch>=9
(RF synth, central freq, AGC index, cached CCK filter) shows up as a discontinuity.

Passive.  uv run python -u scripts/rtl8821cu_dkms/tune_check.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, chan
from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
_REG_RCR = 0x0608
_RCR_ACCEPT_ERR = 0x90000001 | (1 << 8) | (1 << 9)


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


def _count(t, dwell):
    n = 0
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        try:
            buf = t.bulk_in()
        except Exception:  # noqa: BLE001
            buf = None
        if not buf:
            continue
        off, nb = 0, len(buf)
        while off + 24 <= nb:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + po > nb:
                break
            n += 1
            off += (po + 7) & ~7
    return n


def main() -> int:
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up...", flush=True)
        info = bringup.cold_bringup(t)
        print("  ch  RF0x18  RF0x00  cen_fc  AGCidx  IGI  0xa24    0xa28    0xaac    808       frames",
              flush=True)
        for ch in (1, 4, 6, 7, 8, 9, 10, 11, 13):
            chan.set_channel(t, info, ch)
            t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
            rf18 = read_rf(t, 0x18) & 0xFFFFF
            rf00 = read_rf(t, 0x00) & 0xFFFFF
            fc = (t.read32(0x0860) >> 17) & 0xFFF
            agc = (t.read32(0x0C1C) >> 8) & 0xF
            igi = t.read32(0x0C50) & 0x7F
            a24, a28, aac = t.read32(0x0A24), t.read32(0x0A28), t.read32(0x0AAC)
            r808 = t.read32(0x0808)
            n = _count(t, 1.0)
            print(f"  {ch:<3} {rf18:05x}   {rf00:05x}   0x{fc:03x}   {agc:<5}  "
                  f"0x{igi:02x} {a24:08x} {a28:08x} {aac:08x} {r808:08x} {n:>6}", flush=True)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
