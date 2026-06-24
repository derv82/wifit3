"""Throwaway: A/B the connect() 2.4 GHz band-switch prime fix on hardware.

Measures ch1 (2.4 GHz) decode BEFORE and AFTER the prime round-trip
(driver._prime_2g_rx: set_channel 36 -> set_channel 1). If 2.4 GHz signal is present,
[before] should be ~0 good and [after] should decode -- confirming the fix. If both
are ~0 with low total, there's no 2.4 GHz signal in-window (re-run with an AP up).

Passive.  uv run python -u scripts/rtl8821cu_dkms/prime_verify.py [2g_channel]
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


def _measure(t, dwell):
    good = total = 0
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        try:
            buf = t.bulk_in()
        except Exception:  # noqa: BLE001
            buf = None
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
            if not d.rpt_sel:
                total += 1
                good += not (d.crc_err or d.icv_err)
            off += (po + 7) & ~7
    return good, total


def main() -> int:
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print(f"[*] cold bring-up (lands ch1, 2.4 GHz)...", flush=True)
        info = bringup.cold_bringup(t)
        chan.set_channel(t, info, ch)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        g0, t0 = _measure(t, 4.0)
        print(f"  [BEFORE prime] ch{ch}: good={g0}  total={t0}", flush=True)

        chan.set_channel(t, info, 36)           # the prime: 2.4G -> 5G ...
        chan.set_channel(t, info, ch)           # ... -> back to 2.4G
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        g1, t1 = _measure(t, 4.0)
        print(f"  [AFTER  prime] ch{ch}: good={g1}  total={t1}", flush=True)

        print("\n  VERDICT:", flush=True)
        if g1 >= 20 and g0 == 0:
            print("   prime FIXES 2.4 GHz (0 -> decoding). The connect() band-switch prime works.",
                  flush=True)
        elif g1 >= 20:
            print("   both decoded -> 2.4 GHz already awake this run (no clean dead baseline).",
                  flush=True)
        elif t1 > 100:
            print("   high noise but 0 decode both sides -> interference, no clean AP on this channel"
                  " right now; re-run with a steady AP on this exact channel.", flush=True)
        else:
            print("   no 2.4 GHz signal in-window -> inconclusive; re-run with an AP up.", flush=True)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
