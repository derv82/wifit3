"""Throwaway: read the chip's own PHY-status measurements off received frames.

The jaguar phy_status_rpt_8812 (drvinfo) carries the demod's self-report: AGC gain,
pwdb (RSSI), short/long CFO, EVM, SNR, and the channel it thinks it's on. Reading it
on the CRC-failing frames tells us the fault flavor directly:
  - large |CFO|              => carrier frequency offset (crystal-cap / PLL mistune)
  - bad EVM/SNR, CFO ~0      => IQ imbalance / gain / filter
  - phy_status all zero      => PHY not measuring (APP_PHYST / AGC dead)
  - chl_num != tuned channel => phy_status offset/parse wrong (would also garble bytes)

[SRC] phydm_phystatus.h struct phy_status_rpt_8812. Passive.
    uv run python scripts/rtl8821cu_dkms/phy_status_dump.py [channel] [seconds]
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


def _s8(b: int) -> int:
    return b - 256 if b >= 128 else b


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


def _hist(vals, label):
    if not vals:
        print(f"  {label}: (none)")
        return
    vals = sorted(vals)
    n = len(vals)
    print(f"  {label}: n={n} min={vals[0]} p25={vals[n//4]} med={vals[n//2]} "
          f"p75={vals[3*n//4]} max={vals[-1]}")


def main() -> int:
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print(f"[*] cold bring-up, tune ch{ch}...")
        info = bringup.cold_bringup(t)
        chan.set_channel(t, info, ch)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)

        gains, pwdbs, cfoshos, cfotails, evms, snrs = [], [], [], [], [], []
        chl_hist, drvsz_hist, allzero, samples, n = Counter(), Counter(), 0, [], 0
        end = time.monotonic() + secs
        while time.monotonic() < end:
            buf = t.bulk_in()
            if not buf:
                continue
            off, nbuf = 0, len(buf)
            while off + 24 <= nbuf:
                d = query_rx_desc(buf[off:off + 24])
                if d.pkt_len <= 0:
                    break
                po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
                if off + po > nbuf:
                    break
                if not d.rpt_sel and d.physt and d.drvinfo_sz >= 16:
                    phy = buf[off + 24:off + 24 + d.drvinfo_sz]
                    n += 1
                    drvsz_hist[d.drvinfo_sz] += 1
                    if not any(phy):
                        allzero += 1
                    else:
                        gains.append(phy[0] & 0x7F)
                        pwdbs.append(phy[4])
                        cfoshos.append(_s8(phy[5]))
                        cfotails.append(_s8(phy[9]))
                        evms.append(_s8(phy[12]))
                        snrs.append(_s8(phy[14]))
                        chl_hist[phy[2]] += 1
                    if len(samples) < 8:
                        samples.append((d.data_rate, d.crc_err, phy[:20]))
                off += (po + 7) & ~7

        print(f"\n{n} frames; drvinfo_sz seen: {dict(drvsz_hist)}; all-zero phy_status: {allzero}")
        print(f"  phy_status channel field (should be {ch}): {dict(chl_hist.most_common(8))}")
        _hist(gains, "AGC gain[6:0] (high=more gain=weaker sig)")
        _hist(pwdbs, "pwdb_all (RSSI raw; (x>>1)-110 dBm)")
        _hist(cfoshos, "cfo_short path-A  s(8,7) raw")
        _hist(cfotails, "cfo_tail  path-A  s(8,7) raw  <-- freq offset")
        _hist(evms, "rxevm stream1 (signed)")
        _hist(snrs, "rxsnr path-A (signed)")
        print("\n  sample phy_status (rate/crc : first 20 drvinfo bytes):")
        for rate, crc, ph in samples:
            print(f"   r{rate:<2} crc{crc}: {ph.hex()}")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
