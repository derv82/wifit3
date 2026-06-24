"""Throwaway: characterize the CRC-failed RX frames — what flavor of demod corruption?

Frames reach the demod but ~99% fail CRC. This captures them (RCR accepts CRC/ICV
errors), and for each records the DESC rate (CCK<=3 vs OFDM), the PHY-status RSSI,
length, and the raw bytes. Then it asks: do they carry recognizable 802.11 structure
(sane FC subtype, broadcast addr1 on beacons, repeated BSSIDs) => header survives =>
CFO/SNR-like; or total garbage => spectral inversion / IQ / gross.

Strong RSSI + failing CRC = demod fault, not weak signal. Repeated BSSIDs across many
frames = real periodic beacons getting mangled.

Passive. uv run python scripts/rtl8821cu_dkms/frame_dump.py [channel] [seconds]
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
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc, decode_rssi
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
_REG_RCR = 0x0608
_RCR_ACCEPT_ERR = 0x90000001 | (1 << 8) | (1 << 9)
_SUBTYPE = {0x80: "beacon", 0x50: "proberesp", 0x40: "probereq", 0xB0: "auth",
            0xC0: "deauth", 0x08: "data", 0x88: "qosdata", 0x84: "blockack",
            0xD4: "ack", 0xB4: "rts", 0xC4: "cts"}


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


def _mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _walk(buf: bytes):
    """Yield (rate, crc, icv, rpt_sel, rssi, mpdu) for each rx unit, INCLUDING error frames."""
    off, n = 0, len(buf)
    while off + 24 <= n:
        d = query_rx_desc(buf[off:off + 24])
        if d.pkt_len <= 0:
            break
        po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if off + po > n:
            break
        start = off + 24 + d.drvinfo_sz + d.shift_sz
        mpdu = buf[start:start + d.pkt_len]
        phy = buf[off + 24:off + 24 + d.drvinfo_sz]
        rssi = decode_rssi(phy, d.data_rate) if d.physt else 0
        yield d.data_rate, d.crc_err, d.icv_err, d.rpt_sel, rssi, mpdu
        off += (po + 7) & ~7


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

        rate_hist, rssi_hist, sub_hist = Counter(), [], Counter()
        bssids, samples, n = Counter(), [], 0
        cck = ofdm = 0
        end = time.monotonic() + secs
        while time.monotonic() < end:
            buf = t.bulk_in()
            if not buf:
                continue
            for rate, crc, icv, rpt, rssi, mpdu in _walk(buf):
                if rpt or len(mpdu) < 10:
                    continue
                n += 1
                rate_hist[rate] += 1
                rssi_hist.append(rssi)
                cck += 1 if rate <= 3 else 0
                ofdm += 1 if rate > 3 else 0
                fc = mpdu[0]
                sub_hist[_SUBTYPE.get(fc & 0xFC, _SUBTYPE.get(fc, f"fc=0x{fc:02x}"))] += 1
                if len(mpdu) >= 22 and (fc & 0x0C) == 0:        # mgmt: addr3 = BSSID
                    bssids[_mac(mpdu[16:22])] += 1
                if len(samples) < 14:
                    samples.append((rate, crc, icv, rssi, mpdu[:40]))

        print(f"\n{n} frames in {secs:g}s  (CCK rate<=3: {cck}  OFDM: {ofdm})")
        print(f"  DESC rate histogram: {dict(sorted(rate_hist.items()))}")
        if rssi_hist:
            rssi_hist.sort()
            print(f"  RSSI dBm: min={rssi_hist[0]} median={rssi_hist[len(rssi_hist)//2]} "
                  f"max={rssi_hist[-1]}")
        print(f"  FC subtype histogram: {dict(sub_hist.most_common())}")
        print(f"  distinct mgmt BSSIDs (addr3): {len(bssids)}; top repeats: "
              f"{[c for _, c in bssids.most_common(6)]}")
        print(f"\n  sample frames (rate/crc/icv/rssi : first 40 bytes):")
        for rate, crc, icv, rssi, head in samples:
            print(f"   r{rate:<2} crc{crc:d} icv{icv:d} {rssi:>4}dBm: {head.hex()}")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
