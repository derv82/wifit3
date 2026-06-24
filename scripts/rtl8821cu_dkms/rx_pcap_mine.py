"""Throwaway: mine the chip->host RX direction the gate ignores.

verify_pcap reproduces only the host->chip ctrl + bulk-OUT stream. The pcap ALSO
records the chip->host bulk-IN (ep 0x84 RX) + interrupt-IN (ep 0x81 C2H) it never
looks at. The vendor driver ran airmon + airodump + aireplay --test in this capture,
so if it received any 802.11 frames they are sitting in the bulk-IN completions.

This pulls every bulk-IN completion's data out of the pcap and runs it through OUR
OWN rx.py decoder (query_rx_desc / iter_frames), classifying C2H vs good MPDU vs
crc/icv-error, and parses MPDUs with WlanFrameParser to surface beacons. So it
answers two unverified claims at once: (1) did the vendor config actually receive
802.11 frames, and (2) does our RX decoder correctly parse real vendor RX buffers.

    uv run python scripts/rtl8821cu_dkms/rx_pcap_mine.py [capture-1.pcap]
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc, iter_frames  # noqa: E402
from wifit3.wlan.packet import WlanFrameParser  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"


def _hex(s: str) -> bytes:
    s = s.replace(":", "").strip()
    return bytes.fromhex(s) if s else b""


def extract_in(pcap: Path, dev: int, ep_dir_hi: int):
    """Ordered (frame, data) for chip->host completions on bulk-IN (ep & 0x80).
    ep_dir_hi selects transfer_type: 0x03 = bulk-IN, 0x01 = interrupt-IN."""
    fields = ["frame.number", "usb.endpoint_address", "usb.capdata", "usb.urb_type"]
    flt = (f"usb.device_address=={dev} && usb.transfer_type==0x{ep_dir_hi:02x} "
           f"&& usb.endpoint_address.direction==1")
    cmd = ["tshark", "-r", str(pcap), "-Y", flt, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        c = line.split("\t")
        c += [""] * (len(fields) - len(c))
        frame, ep, cap, utype = c[:4]
        data = _hex(cap)
        if data:
            rows.append((int(frame), int(ep, 16), data, utype.strip("'")))
    return rows


def main() -> int:
    pcap = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAP
    if not pcap.exists():
        print(f"no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    print(f"{pcap.name}: card=dev{dev}\n")

    bulk_in = extract_in(pcap, dev, 0x03)
    intr_in = extract_in(pcap, dev, 0x01)
    eps = Counter(ep for _, ep, _, _ in bulk_in)
    print(f"bulk-IN completions with data: {len(bulk_in)}  (by ep: "
          f"{', '.join(f'0x{e:02x}={n}' for e, n in sorted(eps.items()))})")
    print(f"interrupt-IN completions with data: {len(intr_in)}\n")

    total_pkts = c2h = good = crc = icv = beacons = 0
    rate_hist: Counter = Counter()
    first_mpdu_frame = None
    ssid_seen: Counter = Counter()
    subtype_hist: Counter = Counter()

    for frame, ep, buf, _ in bulk_in:
        off, n = 0, len(buf)
        while off + 24 <= n:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            pkt_off = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + pkt_off > n:
                break
            total_pkts += 1
            rate_hist[d.data_rate] += 1
            if d.rpt_sel:
                c2h += 1
            elif d.crc_err:
                crc += 1
            elif d.icv_err:
                icv += 1
            else:
                good += 1
                if first_mpdu_frame is None:
                    first_mpdu_frame = frame
            off += (pkt_off + 7) & ~7

    # Decode good MPDUs via the real driver path (iter_frames -> parser).
    for frame, ep, buf, _ in bulk_in:
        for mpdu, rssi in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(mpdu, rssi)
            if parsed is None:
                continue
            fc = mpdu[0] if mpdu else 0
            subtype_hist[(fc >> 4) & 0xF] += 1
            if (fc & 0x0C) == 0x00 and ((fc >> 4) & 0xF) == 0x08:
                beacons += 1
            ssid = parsed.get("ssid")
            if ssid:
                ssid_seen[ssid] += 1

    print(f"rx_pkt_desc units across all bulk-IN buffers: {total_pkts}")
    print(f"  c2h(rpt_sel)={c2h}  good_80211={good}  crc_err={crc}  icv_err={icv}")
    print(f"  first good-MPDU at pcap frame: {first_mpdu_frame}")
    print(f"  DESC rate-idx histogram: {dict(sorted(rate_hist.items()))}  (<=3 = CCK)")
    print(f"\niter_frames -> parser: {sum(subtype_hist.values())} MPDUs parsed, "
          f"{beacons} beacons")
    print(f"  mgmt/data subtype histogram: {dict(sorted(subtype_hist.items()))}")
    if ssid_seen:
        print(f"  distinct SSIDs seen: {len(ssid_seen)} "
              f"(top counts: {ssid_seen.most_common(5) and '[redacted]'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
