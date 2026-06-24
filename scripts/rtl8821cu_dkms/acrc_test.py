"""Throwaway: split "demod fails" from "nothing received" — accept CRC/ICV-error frames.

The monitor RCR (0x90000001) drops CRC/ICV-error frames, so the tally only ever sees
perfectly-demodulated MPDUs. Add ACRC32 (BIT8) + AICV (BIT9): the HW then forwards
frames that reached the demod but failed the checksum. If error-frames now arrive,
real signal IS being received and demodulation/calibration is the fault. If still
nothing, the front end receives nothing at all (antenna / RF / quiet environment).

Passive. uv run python scripts/rtl8821cu_dkms/acrc_test.py
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
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
_REG_RCR = 0x0608
_RCR_ACCEPT_ERR = 0x90000001 | (1 << 8) | (1 << 9)      # + ACRC32 + AICV


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


def _tally(t, dwell):
    good = crc = icv = c2h = total = 0
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        buf = t.bulk_in()
        if not buf:
            continue
        off, n = 0, len(buf)
        while off + 24 <= n:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + po > n:
                break
            total += 1
            if d.rpt_sel:
                c2h += 1
            elif d.crc_err:
                crc += 1
            elif d.icv_err:
                icv += 1
            else:
                good += 1
            off += (po + 7) & ~7
    return total, good, crc, icv, c2h


def main() -> int:
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up...")
        info = bringup.cold_bringup(t)
        for ch in (1, 6, 11):
            chan.set_channel(t, info, ch)
            t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
            rcr = t.read32(_REG_RCR)
            tot, good, crc, icv, c2h = _tally(t, 5.0)
            print(f"  ch{ch:<3} RCR=0x{rcr:08x}  pkts={tot:>4}  good={good:>4}  "
                  f"crc_err={crc:>4}  icv_err={icv:>3}  c2h={c2h:>3}")
        print("\n  crc_err>0 => real frames reach the demod (demod/calibration fault);")
        print("  all zero  => nothing is being received (antenna / RF front-end / quiet env).")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
