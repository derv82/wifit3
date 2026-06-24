"""Throwaway: signal-normalized crystal-cap sweep — is residual CFO/SFO the fault?

beacon_align showed the demod decodes beacon HEADERS perfectly but every long frame
fails CRC -> errors accumulate over length -> residual carrier/sampling freq offset,
set by the crystal. We apply xtal=0x2e (== capture card); this unit's crystal may want
a different cap.

The RF environment is variable, so a fixed-dwell sweep is unreliable (quiet windows
read 0 regardless of cap). This sweep is ADAPTIVE: pick the busiest channel, then for
each cap read until it has seen >=TARGET beacon-header frames (signal confirmed) or a
timeout, and report good/header. At the right cap good/header rises; flat => not the
crystal. Passive.
    uv run python scripts/rtl8821cu_dkms/xtal_sweep.py
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
from wifit3.chips.rtl8821cu_dkms.bb import set_crystal_cap
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
_REG_RCR = 0x0608
_RCR_ACCEPT_ERR = 0x90000001 | (1 << 8) | (1 << 9)
_TARGET_HDRS = 40              # signal-present threshold per cap
_CAP_TIMEOUT = 1.2


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


def _full_hdr(m):
    return (len(m) >= 24 and m[0] == 0x80 and m[4:10] == b"\xff\xff\xff\xff\xff\xff"
            and m[10:16] == m[16:22] and m[10:16] != b"\x00" * 6)


def _walk(buf):
    off, n = 0, len(buf)
    while off + 24 <= n:
        d = query_rx_desc(buf[off:off + 24])
        if d.pkt_len <= 0:
            break
        po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if off + po > n:
            break
        if not d.rpt_sel:
            start = off + 24 + d.drvinfo_sz + d.shift_sz
            yield d, buf[start:start + d.pkt_len]
        off += (po + 7) & ~7


def _busiest_channel(t, info):
    best, best_n = 1, -1
    for ch in (1, 6, 11, 3, 9):
        chan.set_channel(t, info, ch)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        hdrs = 0
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            buf = t.bulk_in()
            if buf:
                hdrs += sum(1 for _, m in _walk(buf) if _full_hdr(m))
        print(f"   ch{ch}: {hdrs} beacon-headers/1.5s")
        if hdrs > best_n:
            best, best_n = ch, hdrs
    return best, best_n


def _measure_cap(t):
    good = full = total = 0
    end = time.monotonic() + _CAP_TIMEOUT
    while time.monotonic() < end and full < _TARGET_HDRS:
        buf = t.bulk_in()
        if not buf:
            continue
        for d, m in _walk(buf):
            total += 1
            if not (d.crc_err or d.icv_err):
                good += 1
            if _full_hdr(m):
                full += 1
    return good, full, total


def main() -> int:
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up; finding busiest channel...")
        info = bringup.cold_bringup(t)
        ch, nh = _busiest_channel(t, info)
        print(f"[*] sweeping crystal cap on ch{ch} ({nh} hdrs/1.5s); 2 passes accumulated\n")
        chan.set_channel(t, info, ch)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        passes = int(sys.argv[1]) if len(sys.argv) > 1 else 2
        acc = {}
        for _p in range(passes):
            for cap in range(0x24, 0x39, 1):
                set_crystal_cap(t, cap)
                time.sleep(0.2)
                g, f, tot = _measure_cap(t)
                pg, pf, pt = acc.get(cap, (0, 0, 0))
                acc[cap] = (pg + g, pf + f, pt + tot)
        print("  cap   good  hdrs  total  good/hdr")
        best = (-1, -1.0)
        for cap in sorted(acc):
            g, f, tot = acc[cap]
            r = g / f if f else 0.0
            mark = " <-default" if cap == 0x2E else ""
            if g > best[1]:
                best = (cap, g)
            print(f"  0x{cap:02x}  {g:>4}  {f:>4}  {tot:>5}  {r:>6.3f}{mark}")
        bcap, bg = best
        print(f"\n  most good frames at cap 0x{bcap:02x} ({bg}). default 0x2e.")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
