"""Throwaway: decode the VENDOR's good-frame phy_status from the pcap (same parser).

phy_status_dump.py reads our CRC-failing frames' phy_status as pwdb/evm/snr=0,
channel=0, path-A gain=1 / path-B gain~50. To know which of those are real anomalies
vs just unpopulated/wrong-offset fields, decode the vendor's KNOWN-GOOD received
frames (pcap bulk-IN, crc_err=0) with the identical byte offsets and compare.
    uv run python scripts/rtl8821cu_dkms/vendor_phy.py
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc

DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"


def _s8(b):
    return b - 256 if b >= 128 else b


def _hex(s):
    return bytes.fromhex(s.replace(":", "").strip()) if s.strip() else b""


def _bulk_in(pcap, dev):
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y",
         f"usb.device_address=={dev} && usb.endpoint_address==0x84", "-T", "fields",
         "-e", "usb.capdata"], capture_output=True, text=True, check=True).stdout
    return [_hex(l) for l in out.splitlines() if l.strip()]


def _hist(vals, label):
    if not vals:
        print(f"  {label}: (none)")
        return
    vals = sorted(vals)
    n = len(vals)
    print(f"  {label}: n={n} min={vals[0]} p25={vals[n//4]} med={vals[n//2]} "
          f"p75={vals[3*n//4]} max={vals[-1]}")


def main() -> int:
    dev = rp.find_card_device(DEFAULT_CAP)
    print(f"{DEFAULT_CAP.name}: card=dev{dev}; decoding vendor GOOD-frame phy_status\n")
    gains_a, gains_b, pwdbs, cfoshos, cfotails, evms, snrs = [], [], [], [], [], [], []
    chl_hist, drvsz, samples, n = Counter(), Counter(), [], 0
    for buf in _bulk_in(DEFAULT_CAP, dev):
        off, nbuf = 0, len(buf)
        while off + 24 <= nbuf:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + po > nbuf:
                break
            if (not d.rpt_sel and not d.crc_err and not d.icv_err
                    and d.physt and d.drvinfo_sz >= 16):
                phy = buf[off + 24:off + 24 + d.drvinfo_sz]
                n += 1
                drvsz[d.drvinfo_sz] += 1
                gains_a.append(phy[0] & 0x7F)
                gains_b.append(phy[1] & 0x7F)
                pwdbs.append(phy[4])
                cfoshos.append(_s8(phy[5]))
                cfotails.append(_s8(phy[9]))
                evms.append(_s8(phy[12]))
                snrs.append(_s8(phy[14]))
                chl_hist[phy[2]] += 1
                if len(samples) < 8 and d.data_rate > 3:
                    samples.append((d.data_rate, phy[:20]))
            off += (po + 7) & ~7

    print(f"{n} good frames; drvinfo_sz: {dict(drvsz)}")
    print(f"  phy_status channel field: {dict(chl_hist.most_common(8))}")
    _hist(gains_a, "gain path-A[6:0]")
    _hist(gains_b, "gain path-B[6:0]")
    _hist(pwdbs, "pwdb_all (RSSI raw)")
    _hist(cfoshos, "cfo_short path-A")
    _hist(cfotails, "cfo_tail path-A")
    _hist(evms, "rxevm stream1")
    _hist(snrs, "rxsnr path-A")
    print("\n  sample OFDM phy_status (rate : first 20 bytes):")
    for rate, ph in samples:
        print(f"   r{rate:<2}: {ph.hex()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
