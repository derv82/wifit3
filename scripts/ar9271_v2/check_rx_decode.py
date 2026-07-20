"""Throwaway: validate rx_decode.iter_frames against a capture's REAL bulk-IN RX frames.

The pcap gate verifies host->device ops only; the device->host RX *frame* decode has no gate.
This decodes the capture's recorded EP 0x82 (WLAN_RX) transfers through the real iter_frames and
reports frame-type / SSID / RSSI tallies, so the HIF-stream framing + rx_status offsets + the FCS
strip are confirmed off real wire bytes before hardware.

    uv run python scripts/ar9271_v2/check_rx_decode.py [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import binascii
import struct
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "ar9271_v2"))

import ar9271_pcap_replay as rp  # noqa: E402
from wifit3.chips.ar9271_v2 import rx_decode  # noqa: E402
from wifit3.dot11.parser import WlanFrameParser  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new2" / "captures_ath9k_htc_newddevice"


_H = rx_decode.HTC_FRAME_HDR_LEN + rx_decode.RX_STATUS_LEN   # htc hdr + rx_status = 48


def _full_frames(buf: bytes):
    """Walk the HIF stream like iter_frames — same drop filters — but yield each delivered
    802.11 frame WITH its trailing FCS, so the FCS can be CRC-checked (the production decoder
    strips it). Applying the same filters means the denominator is the frames actually
    delivered, not the hardware-flagged bad-CRC frames iter_frames drops."""
    i, n = 0, len(buf)
    while i + 4 <= n:
        pkt_len, tag = struct.unpack_from("<HH", buf, i)
        if tag != rx_decode.HIF_RX_STREAM_TAG:
            return
        start, end = i + 4, i + 4 + pkt_len
        if end > n:
            return
        body = buf[start:end]
        i = end + ((4 - (pkt_len & 3)) & 3)
        if pkt_len < _H:
            continue
        rxs = body[rx_decode.HTC_FRAME_HDR_LEN:_H]
        dot11 = body[_H:]
        rs_datalen = struct.unpack_from(">H", rxs, rx_decode._OFF_DATALEN)[0]
        if rxs[rx_decode._OFF_STATUS] & rx_decode._DROP_STATUS:
            continue
        if rs_datalen != len(dot11) or rs_datalen < 10:
            continue
        yield dot11                                          # rs_datalen bytes incl. FCS


def run(name: str) -> int:
    pkts = rp.parse_pcapng(str(CAP_DIR / f"{name}.pcap"))
    dev = rp.detect_card(pkts)
    ex = rp.extract(pkts, dev)
    rx_bufs = [bytes(r["data"]) for r in ex["responses"] if r["ep"] == rp.EP_WLAN_RX]

    # 1) FCS proof: for every full frame >= 8 B, CRC-32 the body and compare to its last 4 bytes.
    fcs_ok = fcs_checked = 0
    for buf in rx_bufs:
        for f in _full_frames(buf):
            if len(f) < 8:
                continue
            fcs_checked += 1
            if binascii.crc32(f[:-4]) == int.from_bytes(f[-4:], "little"):
                fcs_ok += 1

    # 2) Production path: type / SSID / RSSI tallies via the real iter_frames.
    types: Counter = Counter()
    ssids: Counter = Counter()
    frames = none_parsed = rssi_sum = 0
    rssi_lo, rssi_hi = 999, -999
    for buf in rx_bufs:
        for mpdu, rssi in rx_decode.iter_frames(buf):
            frames += 1
            rssi_sum += rssi
            rssi_lo, rssi_hi = min(rssi_lo, rssi), max(rssi_hi, rssi)
            parsed = WlanFrameParser.parse_80211_frame(mpdu, rssi)
            if parsed is None:
                none_parsed += 1                             # control frames + anything unparsed
                continue
            types[parsed.type] += 1
            if parsed.ssid:
                ssids[parsed.ssid] += 1

    print(f"{name}: {len(rx_bufs)} bulk-IN transfers -> {frames} frames decoded")
    print(f"  FCS CRC-32 valid: {fcs_ok}/{fcs_checked} "
          f"({100 * fcs_ok / max(fcs_checked, 1):.1f}%)  <- proves the FCS strip is correct")
    print(f"  parsed types: {dict(types.most_common())}")
    print(f"  parser returned None (control frames etc.): {none_parsed}")
    print(f"  distinct SSIDs: {len(ssids)}  (top: {[s for s, _ in ssids.most_common(5)]})")
    if frames:
        print(f"  RSSI dBm: mean {rssi_sum / frames:.0f}, range [{rssi_lo}, {rssi_hi}]")
    # Healthy: FCS overwhelmingly valid (frame boundaries + length are exact) and real SSIDs seen.
    return 0 if fcs_checked and fcs_ok > fcs_checked * 0.9 and ssids else 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else "capture-1"))
