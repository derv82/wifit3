"""RTL8814AU — 2.4 GHz RX-death repro on the LINUX (kernel DKMS) driver.

The userland twin is `rx_death_repro.py`, which proves the hop→dwell 2.4 GHz wedge on our
port and (with --no-dig) that the DIG watchdog is only masking it. We can't toggle DIG on the
kernel driver, so this reproduces the same trigger with stock tools: `airmon-ng` for monitor
mode, `iw ... set channel` to hop 2.4↔5 GHz then land on a 2.4 GHz channel, and `tcpdump` to
record the dwell. It then bins beacons/s (all + a pinned --ref AP) by seconds-into-the-dwell,
so a dead-then-recover window shows up exactly as it does on our port.

Compare the two runs:
    sudo -v   # cache credentials; the script shells out to sudo iw/tcpdump/airmon-ng
    uv run python scripts/rtl8814au_dkms/rx_death_repro_linux.py --ref <BSSID>              # hop→sit
    uv run python scripts/rtl8814au_dkms/rx_death_repro_linux.py --ref <BSSID> --skip-hop   # cold-sit control

A healthy cold-sit vs a dead/degraded hop→sit start = the kernel driver has the same bug.
--ref is a runtime arg (no BSSID committed); on-screen output stays on your terminal.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "src"))

# Reuse the baseline-linux monitor + radiotap-pcap helpers (hyphenated filename -> importlib).
_spec = importlib.util.spec_from_file_location(
    "baseline_linux", _HERE.parents[1] / "diag" / "baseline-linux.py")
_bl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bl)

from wifit3.wlan.packet import WlanFrameParser  # noqa: E402

_DEVNULL = subprocess.DEVNULL


def _set_channel(mon: str, ch: int) -> None:
    subprocess.run(["sudo", "iw", "dev", mon, "set", "channel", str(ch)],
                   stdout=_DEVNULL, stderr=_DEVNULL)


def _hop(mon: str, channels: list[int], secs: float, dwell: float) -> None:
    end = time.time() + secs
    i = 0
    while time.time() < end:
        _set_channel(mon, channels[i % len(channels)])
        time.sleep(dwell)
        i += 1


def _timeline(pcap: str, ref: str | None, sit_start: float, sample: float) -> None:
    """Bin beacons by seconds relative to the dwell start; print ref/s + total/s + nBSSID."""
    rows: dict[int, dict] = {}
    for ts, frame, rssi in _bl.read_pcap(pcap):
        try:
            p = WlanFrameParser.parse_80211_frame(frame, rssi if rssi is not None else 0)
        except Exception:  # noqa: BLE001
            continue
        if not p or p.type != "beacon" or not p.bssid:
            continue
        b = int((ts - sit_start) // sample)
        r = rows.setdefault(b, {"ref": 0, "tot": 0, "bssids": set()})
        r["tot"] += 1
        r["bssids"].add(p.bssid.lower())
        if ref and p.bssid.lower() == ref:
            r["ref"] += 1
    print(f"\n{'t(s)':>6} {'phase':<5} {'ref/s':>6} {'all/s':>6} {'nBSSID':>6}", file=sys.stderr)
    for b in sorted(rows):
        r = rows[b]
        t = b * sample
        phase = "sit" if t >= 0 else "hop"
        print(f"{t:6.0f} {phase:<5} {r['ref'] / sample:6.1f} {r['tot'] / sample:6.1f} "
              f"{len(r['bssids']):>6}", file=sys.stderr)


def _phase_timeline(pcap: str, ref: str | None, t0: float, window: float,
                    sample: float, label: str) -> int:
    """Print a beacons/s timeline for one phase window [t0, t0+window). Returns total beacons."""
    rows: dict[int, dict] = {}
    total = 0
    for ts, frame, rssi in _bl.read_pcap(pcap):
        if ts < t0 or ts >= t0 + window:
            continue
        try:
            p = WlanFrameParser.parse_80211_frame(frame, rssi if rssi is not None else 0)
        except Exception:  # noqa: BLE001
            continue
        if not p or p.type != "beacon" or not p.bssid:
            continue
        total += 1
        b = int((ts - t0) // sample)
        r = rows.setdefault(b, {"ref": 0, "tot": 0, "bssids": set()})
        r["tot"] += 1
        r["bssids"].add(p.bssid.lower())
        if ref and p.bssid.lower() == ref:
            r["ref"] += 1
    print(f"\n[{label}]  ({total} beacons total)  {'t(s)':>4} {'ref/s':>6} {'all/s':>6} {'nBSSID':>6}",
          file=sys.stderr)
    for b in sorted(rows):
        r = rows[b]
        print(f"{'':>{len(label) + 22}}{b * sample:4.0f} {r['ref'] / sample:6.1f} "
              f"{r['tot'] / sample:6.1f} {len(r['bssids']):>6}", file=sys.stderr)
    if not rows:
        print(f"{'':>{len(label) + 22}}(dead — 0 beacons in {window:.0f}s)", file=sys.stderr)
    return total


def run_ab(args: argparse.Namespace) -> int:
    """Cold-sit vs hop→sit in ONE monitor session (single airmon cycle), so the per-run
    airmon-cycle degradation can't confound the A/B. A healthy cold-sit + a dead hop→sit in the
    same session = the kernel driver has the bug; a dead cold-sit = the card came in degraded."""
    hop = [int(c) for c in args.hop_channels.split(",") if c.strip()]
    ref = args.ref.lower() if args.ref else None
    mon = args.iface if args.no_airmon else _bl.setup_monitor(args.iface)
    out = str(Path(tempfile.mkdtemp()) / "ab.pcap")
    total = 2 * args.sit_secs + args.hop_secs + 6
    try:
        cap = subprocess.Popen(
            ["sudo", "timeout", str(int(total)), "tcpdump", "-i", mon, "-w", out, "-U", "-Z", "root"],
            stdout=_DEVNULL, stderr=_DEVNULL)
        time.sleep(1.0)
        print(f"[*] COLD-SIT ch{args.sit_channel} for {args.sit_secs}s", file=sys.stderr)
        _set_channel(mon, args.sit_channel)
        cold_t0 = time.time()
        time.sleep(args.sit_secs)
        print(f"[*] HOP {hop} @ {args.hop_dwell}s for {args.hop_secs}s", file=sys.stderr)
        _hop(mon, hop, args.hop_secs, args.hop_dwell)
        print(f"[*] HOP-SIT ch{args.sit_channel} for {args.sit_secs}s", file=sys.stderr)
        _set_channel(mon, args.sit_channel)
        hop_t0 = time.time()
        time.sleep(args.sit_secs)
        cap.wait()
        _phase_timeline(out, ref, cold_t0, args.sit_secs, args.sample_secs, "COLD-SIT")
        _phase_timeline(out, ref, hop_t0, args.sit_secs, args.sample_secs, "HOP-SIT")
    finally:
        if not args.no_airmon:
            _bl.teardown_monitor(mon)
    return 0


def run(args: argparse.Namespace) -> int:
    hop = [int(c) for c in args.hop_channels.split(",") if c.strip()]
    ref = args.ref.lower() if args.ref else None
    mon = args.iface if args.no_airmon else _bl.setup_monitor(args.iface)
    total = (0 if args.skip_hop else args.hop_secs) + args.sit_secs + 3
    out = str(Path(tempfile.mkdtemp()) / "repro.pcap")
    try:
        print(f"[*] tcpdump -> {out} ({'sit-only' if args.skip_hop else 'hop then sit'})",
              file=sys.stderr)
        cap = subprocess.Popen(
            ["sudo", "timeout", str(int(total)), "tcpdump", "-i", mon, "-w", out, "-U", "-Z", "root"],
            stdout=_DEVNULL, stderr=_DEVNULL)
        time.sleep(1.0)                                   # let tcpdump attach before we drive
        if not args.skip_hop:
            print(f"[*] HOP {hop} @ {args.hop_dwell}s for {args.hop_secs}s", file=sys.stderr)
            _hop(mon, hop, args.hop_secs, args.hop_dwell)
        print(f"[*] SIT ch{args.sit_channel} for {args.sit_secs}s", file=sys.stderr)
        sit_start = time.time()
        _set_channel(mon, args.sit_channel)
        time.sleep(args.sit_secs)
        cap.wait()
        _timeline(out, ref, sit_start, args.sample_secs)
    finally:
        if not args.no_airmon:
            _bl.teardown_monitor(mon)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="RTL8814AU 2.4 GHz RX-death repro on the kernel driver.")
    p.add_argument("--iface", default="wlan0", help="base interface (airmon brings it to monitor).")
    p.add_argument("--no-airmon", action="store_true", help="--iface is already a monitor iface.")
    p.add_argument("--hop-channels", default="1,149", help="alternated in the HOP phase (2.4,5).")
    p.add_argument("--hop-secs", type=float, default=60.0)
    p.add_argument("--hop-dwell", type=float, default=0.5)
    p.add_argument("--sit-channel", type=int, default=1)
    p.add_argument("--sit-secs", type=float, default=120.0)
    p.add_argument("--sample-secs", type=float, default=3.0)
    p.add_argument("--ref", default=None, help="pin a 2.4 GHz reference BSSID (runtime only).")
    p.add_argument("--skip-hop", action="store_true", help="cold-sit control (no HOP phase).")
    p.add_argument("--ab", action="store_true",
                   help="cold-sit then hop→sit in ONE monitor session (no cross-run degradation).")
    args = p.parse_args()
    try:
        return run_ab(args) if args.ab else run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
