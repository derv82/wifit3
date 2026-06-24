"""Work BACKWARDS from the first RX frame in the airmon stage to the ops that turn RX on.

pcap_slicer puts 'sudo airmon-ng start wlan1' at frames 7673-17766 — that's where the kernel
capture first delivers 802.11 frames (beacons flow there, NOT in the fixed-ch1 window
beacon_watch_usbcap clips to). This finds the FIRST beacon in that window, then prints the
control/bulk ops immediately preceding it WITH per-op wall-clock gaps, and separately lists the
largest inter-op time gaps across init+airmon — the explicit mdelay/udelay waits the byte-gate is
blind to (a wait emits no op). The timing hypothesis: we fire writes faster than the chip settles.

Read-only on the pcap.  uv run python scripts/rtl8821cu_dkms/airmon_rx_onset.py [gap_ms] [n_before]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp

CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"
AIRMON = (7673, 17766)
_SIG = re.compile("8000[0-9a-f]{4}ffffffffffff[0-9a-f]{12}([0-9a-f]{12})")


def first_beacon(dev: int) -> tuple[int, float, str]:
    out = subprocess.run(
        ["tshark", "-r", str(CAP), "-Y",
         f"usb.device_address=={dev} && usb.transfer_type==0x03 "
         f"&& usb.endpoint_address.direction==1 "
         f"&& frame.number>={AIRMON[0]} && frame.number<={AIRMON[1]}",
         "-T", "fields", "-e", "frame.number", "-e", "frame.time_epoch",
         "-e", "usb.capdata"],
        capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) < 3 or not p[2]:
            continue
        m = _SIG.search(p[2].lower())
        if m:
            return int(p[0]), float(p[1]), m.group(1)
    raise SystemExit("no beacon signature found in the airmon window")


def _fmt(op: dict) -> str:
    k = op["kind"]
    if k == "B":
        return f"BULK[{len(op['data'])}B]"
    return f"{k} 0x{op['addr']:04x}/{op['width']}=0x{op['value']:0{op['width'] * 2}x}"


def main() -> int:
    gap_ms = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    n_before = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    dev = rp.find_card_device(CAP)
    fb_frame, fb_epoch, bssid = first_beacon(dev)
    nums, eps = rp.frame_epochs(CAP)
    f2e = dict(zip(nums, eps))
    t0 = f2e[AIRMON[0]]
    print(f"[*] dev={dev}  airmon window frames {AIRMON[0]}-{AIRMON[1]}")
    print(f"[*] FIRST beacon: frame {fb_frame}  bssid {':'.join(bssid[i:i+2] for i in range(0,12,2))}"
          f"  at +{fb_epoch - t0:.3f}s into airmon\n")

    ops = rp.extract_ops(CAP, dev, window=(1, fb_frame))
    # attach epoch + gap-to-next
    for o in ops:
        o["epoch"] = f2e.get(o["frame"])
    print(f"=== last {n_before} ops before the first beacon (frame {fb_frame}) ===")
    print(f"{'op#':>5} {'frame':>7} {'+airmon_s':>10} {'gap_ms':>8}  op")
    tail = ops[-n_before:]
    for idx, o in enumerate(tail):
        gi = len(ops) - n_before + idx
        nxt = ops[gi + 1]["epoch"] if gi + 1 < len(ops) else fb_epoch
        gap = (nxt - o["epoch"]) * 1e3 if o["epoch"] and nxt else 0.0
        rel = (o["epoch"] - t0) if o["epoch"] else 0.0
        mark = "  <<< GAP" if gap >= gap_ms else ""
        print(f"{gi:>5} {o['frame']:>7} {rel:>10.3f} {gap:>8.2f}  {_fmt(o)}{mark}")
    last = ops[-1]
    print(f"\n  [first beacon arrives {(fb_epoch - last['epoch']) * 1e3:.2f} ms after the last op above]")

    # whole init+airmon: largest inter-op gaps (explicit waits)
    print(f"\n=== largest inter-op gaps in frames 1-{fb_frame} (>= {gap_ms} ms) ===")
    rows = []
    for i in range(len(ops) - 1):
        a, b = ops[i], ops[i + 1]
        if a["epoch"] and b["epoch"]:
            g = (b["epoch"] - a["epoch"]) * 1e3
            if g >= gap_ms:
                rows.append((g, i, a, b))
    rows.sort(reverse=True)
    print(f"{'gap_ms':>8} {'+airmon_s':>10} {'after op':>28}  -> next op")
    for g, i, a, b in rows[:30]:
        rel = a["epoch"] - t0
        print(f"{g:>8.2f} {rel:>10.3f}  {_fmt(a):>28}  -> {_fmt(b)}")
    print(f"\n  ({len(rows)} gaps >= {gap_ms} ms total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
