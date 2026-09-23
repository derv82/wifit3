"""Acceptance check: decode rtl8188ftv_dkms bulk-IN RX against the vendor capture.

Extracts every 0x81 completion, walks the aggregation with the port's
``rx.iter_rx``, feeds NORMAL_RX frames to ``WlanFrameParser`` and
cross-checks the seen AP BSSIDs against the over-air reference. Slow
(full tshark pass); run on demand, not in the hot gate.

Run: uv run python scripts/chips/rtl8188ftv_dkms/verify_rx.py [capture-1|capture-2]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8188ftv_dkms import rx as rx_mod
from wifit3.dot11.parser import WlanFrameParser

CAP_DIR = REPO / "driver_captures" / "captures_8188fu"
KNOWN_APS = {"1c:63:49:79:c1:04", "a8:42:a1:99:85:1f", "18:0f:76:39:8a:20"}


def _resolve(capture: str | None) -> Path:
    if capture is None:
        return CAP_DIR / "capture-1.pcap"
    p = Path(capture)
    if p.exists():
        return p
    cand = CAP_DIR / f"{capture}.pcap"
    if cand.exists():
        return cand
    raise SystemExit(f"capture not found: {capture}")


def main() -> int:
    pcap = _resolve(sys.argv[1] if len(sys.argv) > 1 else None)
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-T", "fields", "-e", "frame.number",
         "-e", "usb.capdata",
         "-Y", "usb.endpoint_address==0x81 && usb.urb_type==67"],
        capture_output=True, text=True, check=True).stdout
    n_urb = n_pkt = n_c2h = n_exc = drops = 0
    bssids: set[str] = set()
    for line in out.splitlines():
        fno, _, capdata = line.partition("\t")
        capdata = capdata.strip()
        if not capdata:
            continue
        n_urb += 1
        try:
            buf = bytes.fromhex(capdata)
        except ValueError:
            print(f"  FAIL frame {fno}: bad hex")
            return 1
        for a, payload in rx_mod.iter_rx(buf):
            n_pkt += 1
            if a["c2h"]:
                n_c2h += 1
                continue
            if len(payload) < 2 or payload[0] & 0x03:
                print(f"  FAIL frame {fno}: bad FC {payload[:2].hex()}")
                return 1
            if a["pkt_len"] != len(payload):
                drops += 1
                continue
            try:
                pkt = WlanFrameParser.parse_80211_frame(payload, 0)
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL frame {fno}: parser raised {e!r}")
                n_exc += 1
                continue
            if pkt is not None and pkt.bssid:
                bssids.add(pkt.bssid.lower())
    print(f"rx: {n_urb} urbs, {n_pkt} packets ({n_c2h} c2h), "
          f"parser-exc={n_exc} len-drops={drops}")
    print(f"rx: {len(bssids)} ap bssids, known-found="
          f"{sorted(bssids & KNOWN_APS)}")
    if n_exc:
        return 1
    if not (bssids & KNOWN_APS):
        print("  FAIL: no known AP seen")
        return 1
    print("rx: green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
