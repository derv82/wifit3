"""RTL8814AU — the 5->2 RX-wedge repro on the LINUX (kernel) driver, N-trial rate.

The apples-to-apples twin of rx_scan_wedge.py: SAME methodology (repeat a 5 GHz hop then a
single 2.4 GHz cross-and-dwell, N times, report a wedge RATE), but driving the STOCK kernel
rtl8814au driver via airmon-ng + `iw set channel` + tcpdump instead of our userland port.

Purpose: settle gap-vs-silicon. If the kernel driver wedges at ~the same rate our port does
(~40%), the 5->2 wedge is a silicon/driver reality, not a port divergence. If the kernel driver
is clean (0/N), then our port diverges from Linux and THAT divergence is the thing to find.

One airmon session + one continuous tcpdump for the whole run (so the per-cycle airmon
degradation the earlier A/B hit can't confound the trials). Beacons in the first 3 s of each
dwell are counted from the pcap; <threshold = wedged. Read-only (monitor capture, no TX).

    sudo -v
    uv run python scripts/chips/rtl8814au_dkms/rx_scan_wedge_linux.py --trials 12
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3] / "src"))

sys.path.insert(0, str(_HERE.parents[2] / "baseline"))
import baseline_linux as _bl

from wifit3.dot11.parser import WlanFrameParser

_DEVNULL = subprocess.DEVNULL
_HOP5 = [157, 165, 149]      # non-DFS 5 GHz; end on 149 to match the userland repro's cross-from channel
_CROSS_FROM = 149


def _set_channel(mon: str, ch: int) -> bool:
    return subprocess.run(["sudo", "iw", "dev", mon, "set", "channel", str(ch)],
                          stdout=_DEVNULL, stderr=_DEVNULL).returncode == 0


def _beacon_bins(pcap: str, t0: float, nsec: int) -> list[int]:
    """Per-second all-beacon counts for the nsec seconds starting at t0 — so a wedged dwell's
    dead stretch AND whether/when it recovers are both visible."""
    bins = [0] * nsec
    for ts, frame, rssi in _bl.read_pcap(pcap):
        i = int(ts - t0)
        if not (0 <= i < nsec):
            continue
        try:
            p = WlanFrameParser.parse_80211_frame(frame, rssi if rssi is not None else 0)
        except Exception:  # noqa: BLE001
            continue
        if p and p.type == "beacon" and p.bssid:
            bins[i] += 1
    return bins


def run(args: argparse.Namespace) -> int:
    ref = args.ref.lower() if args.ref else None
    mon = args.iface if args.no_airmon else _bl.setup_monitor(args.iface)
    out = str(Path(tempfile.mkdtemp()) / "wedge.pcap")
    # continuous capture for the whole run: hop+dwell per trial + margins
    total = int(args.trials * (args.hop5_secs + args.dwell_secs + 1) + 8)
    crosses: list[float] = []          # capture-clock time of each 5->2 cross
    try:
        print(f"[*] tcpdump -> {out} for ~{total}s ; {args.trials} trials", file=sys.stderr)
        cap = subprocess.Popen(
            ["sudo", "timeout", str(total), "tcpdump", "-i", mon, "-w", out, "-U", "-Z", "root"],
            stdout=_DEVNULL, stderr=_DEVNULL)
        time.sleep(2.0)                # let tcpdump attach
        for trial in range(1, args.trials + 1):
            end = time.time() + args.hop5_secs
            i = 0
            while time.time() < end:
                _set_channel(mon, _HOP5[i % len(_HOP5)])
                time.sleep(0.5)
                i += 1
            _set_channel(mon, _CROSS_FROM)
            time.sleep(0.3)
            _set_channel(mon, args.dwell_ch)         # THE 5->2 cross
            crosses.append(time.time())
            print(f"  trial {trial:>2}: crossed to ch{args.dwell_ch}", file=sys.stderr)
            time.sleep(args.dwell_secs)
        cap.wait()
    finally:
        if not args.no_airmon:
            _bl.teardown_monitor(mon)

    # Offline: per-second beacons across each dwell. Wedged = first 3 s below threshold;
    # recovery = first later second that resumes (>=5 beacons/s), else "no recovery in <dwell>s".
    ndwell = int(args.dwell_secs)
    print(f"\n{'trial':>5}  {'first3s':>7}  verdict / recovery      per-second bins", file=sys.stderr)
    wedged = 0
    recovered = 0
    for n, tc in enumerate(crosses, 1):
        bins = _beacon_bins(out, tc + 0.3, ndwell)
        dead3 = sum(bins[:3])
        w = dead3 < args.wedge_th
        wedged += w
        if w:
            rsec = next((i for i in range(3, ndwell) if bins[i] >= 5), None)
            if rsec is not None:
                recovered += 1
                verdict = f"WEDGED, recovered ~{rsec}s"
            else:
                verdict = f"WEDGED, NO recovery in {ndwell}s"
        else:
            verdict = "ok"
        print(f"{n:>5}  {dead3:>7}  {verdict:<24} {bins}", file=sys.stderr)
    print(f"\n=== KERNEL-DRIVER: wedged {wedged}/{len(crosses)}; of those, "
          f"{recovered} recovered within {ndwell}s (dwell ch{args.dwell_ch}) ===", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="8814au 5->2 RX-wedge repro on the kernel driver (N-trial rate).")
    p.add_argument("--iface", default="wlan0")
    p.add_argument("--no-airmon", action="store_true", help="--iface is already a monitor iface.")
    p.add_argument("--dwell-ch", type=int, default=1)
    p.add_argument("--trials", type=int, default=12)
    p.add_argument("--hop5-secs", type=float, default=6.0)
    p.add_argument("--dwell-secs", type=float, default=4.0)
    p.add_argument("--wedge-th", type=int, default=10, help="beacons in first 3 dwell seconds below this = wedged.")
    p.add_argument("--ref", default=None, help="pin a 2.4 GHz reference BSSID (runtime only).")
    args = p.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
