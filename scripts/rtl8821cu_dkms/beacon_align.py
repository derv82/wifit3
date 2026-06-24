"""Throwaway: is it a gross demod error (inversion) or length-dependent BER (marginal)?

Beacons have a self-verifying structure with no ground truth needed:
  byte0 == 0x80 (beacon), bytes4..9 == ff*6 (broadcast addr1), addr2 == addr3 (BSSID).
A frame matching that has its first 22 bytes demodulated correctly. If MANY frames
pass the header check but still fail CRC, the demod gets headers right and errors
accumulate over length => marginal EVM / length-dependent BER (analog / signal), and
the receiver fundamentally works. If almost NONE pass, errors start at symbol 0 =>
a gross systematic error (spectral inversion / IQ) still hiding.

Reports the longest correct beacon-header prefix distribution. Passive.
    uv run python scripts/rtl8821cu_dkms/beacon_align.py [channel] [seconds]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
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


def _beacon_hdr_score(m: bytes) -> int:
    """How many of the 6 beacon-header invariants hold (0..6): FC=0x80, FC[1]=0x00,
    addr1=broadcast, addr2==addr3 (BSSID), addr2 not broadcast, addr2 not zero."""
    if len(m) < 24:
        return 0
    s = 0
    s += m[0] == 0x80
    s += m[1] == 0x00
    s += m[4:10] == b"\xff\xff\xff\xff\xff\xff"
    s += m[10:16] == m[16:22]
    s += m[10:16] != b"\xff\xff\xff\xff\xff\xff"
    s += m[10:16] != b"\x00\x00\x00\x00\x00\x00"
    return s


def main() -> int:
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print(f"[*] cold bring-up, tune ch{ch}...")
        info = bringup.cold_bringup(t)
        chan.set_channel(t, info, ch)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)

        score_hist, n, full_hdr, fc80 = Counter(), 0, 0, 0
        len_of_full = []
        end = time.monotonic() + secs
        while time.monotonic() < end:
            buf = t.bulk_in()
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
                if not d.rpt_sel and d.pkt_len >= 24:
                    start = off + 24 + d.drvinfo_sz + d.shift_sz
                    m = buf[start:start + d.pkt_len]
                    n += 1
                    fc80 += m[0] == 0x80
                    sc = _beacon_hdr_score(m)
                    score_hist[sc] += 1
                    if sc == 6:
                        full_hdr += 1
                        len_of_full.append(d.pkt_len)
                off += (po + 7) & ~7

        print(f"\n{n} frames; {fc80} start with FC=0x80")
        print(f"  beacon-header invariants-passed histogram (6 = full 22-byte header OK):")
        for s in range(7):
            print(f"    {s}/6: {score_hist.get(s,0)}")
        print(f"\n  frames with FULL valid beacon header (first 22 B correct) but accepted as "
              f"crc-err: {full_hdr}")
        if len_of_full:
            len_of_full.sort()
            print(f"    their lengths: min={len_of_full[0]} med={len_of_full[len(len_of_full)//2]} "
                  f"max={len_of_full[-1]}")
        if full_hdr >= 5:
            print("  => headers demodulate correctly; errors accumulate over length "
                  "=> length-dependent BER (marginal EVM / analog), NOT a gross inversion.")
        else:
            print("  => headers almost never correct => errors from symbol 0 "
                  "=> gross systematic demod error (inversion / IQ).")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
