"""Diagnostic sweep — yield baseline + long-run degradation.

Drives the driver-agnostic ``WlanInterface`` so a single binary works
across every supported chipset. Plug a card in, run it, hand the
resulting Markdown report (and sibling CSV) to the agent.

Two probes, both optional:

* **Baseline** — for each supported channel, tune, dwell N seconds,
  count raw RX frames + the BSSIDs whose beacon advertises this
  channel. Silent channels surface tune-time bugs.
* **Long-run** — hop normally for M minutes, snapshot once per second.
  Bucketed into ``--bucket-sec`` windows. A monotonic decline in
  "active BSSIDs" (last_seen within bucket) is the signal the user is
  chasing.

Run::

    uv run python scripts/diag/sweep.py
    uv run python scripts/diag/sweep.py --dwell-sec 5 --longrun-min 2
    uv run python scripts/diag/sweep.py --skip-baseline --longrun-min 30
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Add src/ to sys.path so we can `import wifit3.*` without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.wlan.interface import WlanInterface  # noqa: E402
from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402

logger = logging.getLogger("diag.sweep")

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _classify_band(channel: int) -> str:
    if 1 <= channel <= 14:
        return "2.4"
    if channel >= 36:
        return "5"
    return "?"


def _chipset_slug(iface: WlanInterface) -> str:
    cls_name = type(iface.driver).__name__
    return cls_name.lower().removesuffix("driver")


async def _connect(iface: WlanInterface) -> None:
    def _progress(pct: float, msg: str) -> None:
        print(f"  [{int(pct * 100):3d}%] {msg}", file=sys.stderr)

    print(f"[*] Bringing up {iface.name} ({iface.description})...", file=sys.stderr)
    ok = await iface.connect(progress_cb=_progress)
    if not ok:
        raise RuntimeError("Driver bring-up returned False")


class _FrameCounter:
    """Raw RX frame counter, hookable via ``iface.register_rx_callback``."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, raw: bytes, rssi: int, ts: float) -> None:
        self.count += 1


def _snapshot_active(iface: WlanInterface, since: float):
    """Return (active_total, active_24, active_5, channels_seen)."""
    # Snapshot the dict — defensive against any future threaded RX path.
    aps = [ap for ap in list(iface.access_points.values()) if ap.last_seen >= since]
    a24 = sum(1 for ap in aps if _classify_band(ap.channel) == "2.4")
    a5 = sum(1 for ap in aps if _classify_band(ap.channel) == "5")
    chans_seen = {ap.channel for ap in aps}
    return len(aps), a24, a5, chans_seen


async def _probe_baseline(iface: WlanInterface, channels, dwell_sec: float):
    """Tune to each channel; count frames + BSSIDs during the dwell window."""
    counter = _FrameCounter()
    iface.register_rx_callback(counter)
    results = []

    for ch in channels:
        ok = await iface.set_channel(ch)
        if not ok:
            print(f"  CH{ch:>3}: set_channel failed — skipping", file=sys.stderr)
            results.append(dict(channel=ch, frames=0, bssids=0, new_bssids=0,
                                mean_rssi=None, tuned=False))
            continue

        # AGC / pipe drain — discard first 250 ms.
        await asyncio.sleep(0.25)
        window_start = time.time()
        start_frames = counter.count
        pre_known = {
            bssid for bssid, ap in list(iface.access_points.items())
            if ap.channel == ch
        }
        await asyncio.sleep(dwell_sec)
        frames = counter.count - start_frames

        active = [
            ap for ap in list(iface.access_points.values())
            if ap.channel == ch and ap.last_seen >= window_start
        ]
        active_bssids = {ap.bssid for ap in active}
        mean_rssi = (statistics.mean(ap.signal for ap in active)
                     if active else None)
        results.append(dict(
            channel=ch,
            frames=frames,
            bssids=len(active),
            new_bssids=len(active_bssids - pre_known),
            mean_rssi=mean_rssi,
            tuned=True,
        ))
        rssi_str = "—" if mean_rssi is None else f"{mean_rssi:.0f}"
        print(f"  CH{ch:>3} ({_classify_band(ch)}): "
              f"{frames:>6} frames · {len(active):>3} BSSIDs "
              f"({len(active_bssids - pre_known)} new) · RSSI {rssi_str}",
              file=sys.stderr)
    return results


async def _probe_longrun(iface: WlanInterface, channels, total_sec: int,
                         hop_interval: float, bucket_sec: int,
                         death_timeout_sec: int):
    """Returns ``(snapshots, death_at_sec_or_None)``.

    ``death_at_sec`` is set if the loop cut short because no frames arrived
    for ``death_timeout_sec`` consecutive seconds (and we'd been running
    at least that long). Useful for catching RTL8812AU-style full-RX
    death without making the operator twiddle thumbs.
    """
    counter = _FrameCounter()
    iface.register_rx_callback(counter)
    await iface.start_hopping(channels=channels, interval=hop_interval)

    snapshots: list = []
    t0 = time.time()
    last_frame_count = 0
    last_frame_t = t0
    death_at: float | None = None
    death_msg = (
        f"  Death-detect: stop if no frames for {death_timeout_sec}s "
        "(after at least that long of runtime)."
    ) if death_timeout_sec > 0 else "  Death-detect: disabled."
    print(f"  Hopping {len(channels)} channels at {hop_interval}s interval "
          f"for {total_sec}s, snapshotting every 1s...", file=sys.stderr)
    print(death_msg, file=sys.stderr)
    next_log = t0 + bucket_sec

    try:
        while time.time() - t0 < total_sec:
            await asyncio.sleep(1.0)
            now = time.time()
            active_since = now - bucket_sec
            a_total, a_24, a_5, _ = _snapshot_active(iface, active_since)
            frames_now = counter.count
            frames_delta = frames_now - last_frame_count
            snapshots.append(dict(
                t=now - t0,
                channel=iface.current_channel,
                frames_delta=frames_delta,
                active_total=a_total,
                active_24=a_24,
                active_5=a_5,
            ))
            last_frame_count = frames_now
            if frames_delta > 0:
                last_frame_t = now

            if now >= next_log:
                elapsed_min = (now - t0) / 60.0
                window = snapshots[-bucket_sec:]
                frames_in_window = sum(s["frames_delta"] for s in window)
                print(f"  t={elapsed_min:5.1f}m  "
                      f"active={a_total:>3} (24G={a_24:>3} 5G={a_5:>3})  "
                      f"frames/window={frames_in_window}", file=sys.stderr)
                next_log = now + bucket_sec

            # Death detect — only after we've been running at least
            # death_timeout_sec, so we don't trip on a slow start.
            if (death_timeout_sec > 0
                    and (now - t0) >= death_timeout_sec
                    and (now - last_frame_t) >= death_timeout_sec):
                death_at = now - t0
                print(f"\n  [!] DEATH DETECTED: no frames for "
                      f"{death_timeout_sec}s at t={death_at/60:.1f}m. "
                      "Cutting long-run short.", file=sys.stderr)
                break
    finally:
        await iface.stop_hopping()

    return snapshots, death_at


def _bucketize(snapshots, bucket_sec: int):
    buckets_map: dict[int, list] = defaultdict(list)
    for s in snapshots:
        buckets_map[int(s["t"]) // bucket_sec].append(s)
    out = []
    for key in sorted(buckets_map):
        snaps = buckets_map[key]
        out.append(dict(
            start_sec=key * bucket_sec,
            end_sec=(key + 1) * bucket_sec,
            # Median smooths the 1-sample dip during a tune-in-progress.
            active_total=int(statistics.median(s["active_total"] for s in snaps)),
            active_24=int(statistics.median(s["active_24"] for s in snaps)),
            active_5=int(statistics.median(s["active_5"] for s in snaps)),
            frames_total=sum(s["frames_delta"] for s in snaps),
        ))
    return out


def _write_csv(csv_path: Path, baseline, snapshots) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["# baseline"])
        w.writerow(["channel", "frames", "bssids", "new_bssids",
                    "mean_rssi", "tuned"])
        for r in baseline:
            rssi = "" if r["mean_rssi"] is None else f"{r['mean_rssi']:.1f}"
            w.writerow([r["channel"], r["frames"], r["bssids"],
                        r["new_bssids"], rssi, r["tuned"]])
        w.writerow([])
        w.writerow(["# longrun (per-second snapshots)"])
        w.writerow(["t_sec", "channel", "frames_delta",
                    "active_total", "active_24", "active_5"])
        for s in snapshots:
            w.writerow([f"{s['t']:.1f}", s["channel"], s["frames_delta"],
                        s["active_total"], s["active_24"], s["active_5"]])


def _render_report(report_path: Path, *, iface: WlanInterface, baseline,
                   snapshots, buckets, args) -> None:
    silent = [r for r in baseline if r["tuned"] and r["frames"] == 0]
    untunable = [r for r in baseline if not r["tuned"]]

    degradation = None
    if len(buckets) >= 6:
        first = statistics.median(b["active_total"] for b in buckets[:3])
        last = statistics.median(b["active_total"] for b in buckets[-3:])
        if first > 0:
            degradation = (first, last, last / first)

    chipset = _chipset_slug(iface)
    lines = []
    lines.append(f"# Diagnostic sweep: {chipset}")
    lines.append("")
    lines.append(f"- **Device**: {iface.description}")
    lines.append(f"- **Driver**: {type(iface.driver).__name__}")
    lines.append(f"- **Timestamp**: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- **Channels**: {args.channels_used}")
    lines.append(
        f"- **Baseline**: {args.dwell_sec}s/ch  ·  "
        f"**Long-run**: {args.longrun_min}m, hop {args.hop_interval}s, "
        f"{args.bucket_sec}s buckets"
    )
    lines.append("")
    lines.append("## Quick verdict")
    lines.append("")
    if not args.do_baseline:
        lines.append("- Baseline probe skipped.")
    else:
        if silent:
            chans = ", ".join(str(r["channel"]) for r in silent)
            lines.append(f"- WARN  Silent channels (zero frames during dwell): {chans}")
        else:
            lines.append("- OK    Every tuned channel saw at least one frame.")
        if untunable:
            chans = ", ".join(str(r["channel"]) for r in untunable)
            lines.append(f"- WARN  Tune failures: {chans}")
    if not args.do_longrun:
        lines.append("- Long-run probe skipped.")
    else:
        if args.death_at_sec is not None:
            lines.append(f"- WARN  DEATH DETECTED: RX stopped delivering "
                         f"frames at t={args.death_at_sec / 60:.1f}m. "
                         f"Long-run cut short.")
        if degradation is not None:
            first, last, ratio = degradation
            flag = "WARN " if ratio < 0.5 else "OK   "
            lines.append(f"- {flag} Active-BSSID trend (median of first-3 vs "
                         f"last-3 buckets): {first:.0f} → {last:.0f} "
                         f"(ratio {ratio:.2f})")
        elif args.death_at_sec is None:
            lines.append("- Long-run too short for trend (need >=6 buckets).")
    lines.append("")

    if args.do_baseline and baseline:
        lines.append("## Section 1 - Yield-per-channel baseline")
        lines.append("")
        lines.append("| Channel | Band | Frames | BSSIDs | New | Mean RSSI |")
        lines.append("|---:|:---:|---:|---:|---:|---:|")
        for r in baseline:
            rssi = "—" if r["mean_rssi"] is None else f"{r['mean_rssi']:.0f}"
            tag = "" if r["tuned"] else " — tune failed"
            lines.append(
                f"| {r['channel']} | {_classify_band(r['channel'])} "
                f"| {r['frames']} | {r['bssids']} | {r['new_bssids']} | {rssi} |{tag}"
            )
        lines.append("")

    if args.do_longrun and buckets:
        lines.append(f"## Section 2 - Long-run degradation ({args.bucket_sec}s buckets)")
        lines.append("")
        lines.append("| # | Window (s) | Active total | 2.4 GHz | 5 GHz | Frames |")
        lines.append("|---:|---:|---:|---:|---:|---:|")
        for i, b in enumerate(buckets):
            lines.append(
                f"| {i + 1} | {b['start_sec']}–{b['end_sec']} | "
                f"{b['active_total']} | {b['active_24']} | "
                f"{b['active_5']} | {b['frames_total']} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Raw per-second data is in the sibling CSV. Re-run with "
                 "`--longrun-min 30` for a deeper degradation signal.")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wifit3 diagnostic sweep — yield baseline + long-run degradation."
    )
    parser.add_argument("--dwell-sec", type=float, default=10.0,
                        help="Seconds to dwell on each channel during baseline.")
    parser.add_argument("--longrun-min", type=float, default=10.0,
                        help="Long-run total duration in minutes.")
    parser.add_argument("--hop-interval", type=float, default=0.5,
                        help="Long-run channel-hop interval (seconds).")
    parser.add_argument("--bucket-sec", type=int, default=60,
                        help="Long-run bucket size for the report.")
    parser.add_argument("--channels", type=str, default=None,
                        help="Comma-separated channels (override driver SUPPORTED_CHANNELS).")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--skip-longrun", action="store_true")
    parser.add_argument("--death-timeout-sec", type=int, default=120,
                        help="Cut the long-run short if no frames arrive for "
                        "this many consecutive seconds (after that long of "
                        "runtime). 0 = disabled. Default: 120.")
    parser.add_argument("--debug", action="store_true",
                        help="Enable DEBUG-level logging.")
    args = parser.parse_args()
    args.do_baseline = not args.skip_baseline
    args.do_longrun = not args.skip_longrun
    args.death_at_sec = None

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("[*] Discovering interfaces...", file=sys.stderr)
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] No supported devices found.", file=sys.stderr)
        return 1
    if len(ifaces) > 1:
        print(f"[!] Found {len(ifaces)} interfaces — using {ifaces[0].name}. "
              "Unplug others to test in isolation.", file=sys.stderr)
    iface = ifaces[0]
    print(f"[+] Selected {iface.name}: {iface.description}", file=sys.stderr)

    try:
        await _connect(iface)
    except Exception as e:
        print(f"[-] Bring-up failed: {e}", file=sys.stderr)
        await mgr.close_all()
        return 1

    if args.channels:
        channels = [int(x.strip()) for x in args.channels.split(",") if x.strip()]
    else:
        channels = list(getattr(iface.driver, "SUPPORTED_CHANNELS", []) or [1, 6, 11])
    args.channels_used = ",".join(str(c) for c in channels)

    baseline: list = []
    snapshots: list = []
    try:
        if args.do_baseline:
            print(f"\n[*] Probe 1/2: yield baseline "
                  f"({args.dwell_sec}s/ch on {len(channels)} channels)",
                  file=sys.stderr)
            baseline = await _probe_baseline(iface, channels, args.dwell_sec)
        if args.do_longrun:
            total_sec = int(args.longrun_min * 60)
            print(f"\n[*] Probe 2/2: long-run ({args.longrun_min:.1f} min)",
                  file=sys.stderr)
            snapshots, args.death_at_sec = await _probe_longrun(
                iface, channels, total_sec,
                args.hop_interval, args.bucket_sec,
                args.death_timeout_sec,
            )
    finally:
        await mgr.close_all()

    buckets = _bucketize(snapshots, args.bucket_sec) if snapshots else []

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = REPORTS_DIR / f"{_chipset_slug(iface)}_{ts}"
    report_path = base.with_suffix(".md")
    csv_path = base.with_suffix(".csv")
    _write_csv(csv_path, baseline, snapshots)
    _render_report(report_path, iface=iface, baseline=baseline,
                   snapshots=snapshots, buckets=buckets, args=args)

    print(f"\n[+] Report: {report_path}", file=sys.stderr)
    print(f"[+] CSV:    {csv_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        rc = 130
    sys.exit(rc)
