"""Throwaway: live 2.4 GHz environment monitor — watch noise + decode change over time.

Hops 2.4 GHz channels (+ a 5 GHz control) and prints timestamped per-channel counts:
total rx units (~noise false-alarm rate at full gain), beacon-headers, and good
CRC-passing frames. Used to watch the RF environment change in real time (e.g. a noise
source powering off, or a strong AP / phone moving next to the card).

Passive.  uv run python -u scripts/rtl8821cu_dkms/env_monitor.py [seconds]
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
_CHANNELS = [1, 6, 9, 11, 149]


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


def _count(t, dwell):
    good = full = total = 0
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
                start = off + 24 + d.drvinfo_sz + d.shift_sz
                full += _full_hdr(buf[start:start + d.pkt_len])
            off += (po + 7) & ~7
    return good, full, total


def main() -> int:
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up...", flush=True)
        info = bringup.cold_bringup(t)
        print(f"[*] monitoring {secs:g}s (total=noise@fullgain, hdr=beacon-headers, good=CRC-ok):",
              flush=True)
        t0 = time.monotonic()
        rnd = 0
        while time.monotonic() - t0 < secs:
            rnd += 1
            row = []
            for ch in _CHANNELS:
                chan.set_channel(t, info, ch)
                t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
                g, f, tot = _count(t, 1.1)
                row.append(f"ch{ch}:tot={tot:<4} hdr={f:<3} good={g:<3}")
            print(f"  [t={time.monotonic()-t0:5.0f}s] " + " | ".join(row), flush=True)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
