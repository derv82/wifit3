"""Long-run degradation probe.

Hops normally for M minutes, snapshots once per second. Bucketed into
``--bucket-sec`` windows; a monotonic decline in "active BSSIDs"
(last_seen within bucket) is the signal the user is chasing
(RTL8812AU full-RX death etc.).

Two known biases this probe defuses:

1. **Partial teardown bucket**: if the run stops mid-bucket (death
   detect or user Ctrl-C), the last bucket has fewer snapshots than
   the rest. The first-3-vs-last-3 trend ratio is sensitive to that
   small-sample variance, so we exclude buckets that didn't see at
   least ``bucket_sec / 2`` snapshots from the trend computation.
   The full bucket table is still rendered (with a marker), so the
   user can see what was happening at teardown.

2. **Trend on too-short runs**: the original code required at least
   6 buckets. We keep that gate but compute it on the *trend-eligible*
   subset (after partial-bucket exclusion).
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import defaultdict
from typing import Any

from .base import FrameCounter, Probe, snapshot_active


class LongRunProbe(Probe):
    name = "longrun"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--longrun-min", type=float, default=10.0,
            help="Long-run total duration in minutes.",
        )
        parser.add_argument(
            "--hop-interval", type=float, default=0.5,
            help="Long-run channel-hop interval (seconds).",
        )
        parser.add_argument(
            "--bucket-sec", type=int, default=60,
            help="Long-run bucket size for the report.",
        )
        parser.add_argument(
            "--death-timeout-sec", type=int, default=120,
            help="Cut the long-run short if no frames arrive for this many "
            "consecutive seconds (after that long of runtime). 0 = disabled.",
        )

    def is_enabled(self, args: argparse.Namespace) -> bool:
        return not args.skip_longrun

    def apply_multiplier(self, args: argparse.Namespace, mult: float) -> None:
        # Only the wall-clock duration scales; bucket / hop-interval /
        # death-timeout are about *resolution*, not duration.
        args.longrun_min = args.longrun_min * mult

    def attach(self, iface) -> None:
        pass

    async def run(self, iface, args: argparse.Namespace) -> Any:
        channels = args.channels_list
        total_sec = int(args.longrun_min * 60)
        hop_interval = args.hop_interval
        bucket_sec = args.bucket_sec
        death_timeout_sec = args.death_timeout_sec

        print(
            f"\n[*] Probe: long-run ({args.longrun_min:.1f} min)",
            file=sys.stderr,
        )

        counter = FrameCounter()
        iface.register_rx_callback(counter)
        await iface.start_hopping(channels=channels, interval=hop_interval)

        snapshots: list = []
        t0 = time.time()
        last_frame_count = 0
        last_frame_t = t0
        death_at: float | None = None
        interrupted = False
        death_msg = (
            f"  Death-detect: stop if no frames for {death_timeout_sec}s "
            "(after at least that long of runtime)."
        ) if death_timeout_sec > 0 else "  Death-detect: disabled."
        print(
            f"  Hopping {len(channels)} channels at {hop_interval}s interval "
            f"for {total_sec}s, snapshotting every 1s...",
            file=sys.stderr,
        )
        print(death_msg, file=sys.stderr)
        next_log = t0 + bucket_sec

        try:
            while time.time() - t0 < total_sec:
                try:
                    await asyncio.sleep(1.0)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    # Ctrl+C: fall through to teardown + render. Every
                    # snapshot already in the list is preserved.
                    interrupted = True
                    break
                now = time.time()
                active_since = now - bucket_sec
                a_total, a_24, a_5, _ = snapshot_active(args.array, active_since)
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
                    print(
                        f"  t={elapsed_min:5.1f}m  "
                        f"active={a_total:>3} (24G={a_24:>3} 5G={a_5:>3})  "
                        f"frames/window={frames_in_window}",
                        file=sys.stderr,
                    )
                    next_log = now + bucket_sec

                if (death_timeout_sec > 0
                        and (now - t0) >= death_timeout_sec
                        and (now - last_frame_t) >= death_timeout_sec):
                    death_at = now - t0
                    print(
                        f"\n  [!] DEATH DETECTED: no frames for "
                        f"{death_timeout_sec}s at t={death_at/60:.1f}m. "
                        "Cutting long-run short.",
                        file=sys.stderr,
                    )
                    break
        finally:
            # Teardown must never raise: we want the bucketize +
            # return below to run unconditionally. Common failure
            # modes: second Ctrl+C, USB pipe error if the device
            # already disconnected.
            try:
                await iface.stop_hopping()
            except (KeyboardInterrupt, asyncio.CancelledError):
                interrupted = True
            except Exception as e:
                print(
                    f"  [!] stop_hopping failed: {e}", file=sys.stderr,
                )

        if interrupted:
            args.interrupted = True

        buckets = _bucketize(snapshots, bucket_sec)
        return dict(
            snapshots=snapshots,
            buckets=buckets,
            death_at_sec=death_at,
            bucket_sec=bucket_sec,
            interrupted=interrupted,
        )

    def finalize(self) -> Any:
        return None

    def verdict_lines(self, result, args) -> list[str]:
        if result is None:
            return ["- Long-run probe skipped."]
        lines: list[str] = []
        if result.get("interrupted"):
            lines.append(
                "- INFO  Long-run cut short by Ctrl+C: trend reflects "
                "only the buckets that completed before the interrupt."
            )
        if result["death_at_sec"] is not None:
            d = result["death_at_sec"]
            lines.append(
                f"- WARN  DEATH DETECTED: RX stopped delivering frames at "
                f"t={d / 60:.1f}m. Long-run cut short."
            )
        degradation = _trend(result["buckets"], result["bucket_sec"])
        if degradation is not None:
            first, last, ratio = degradation
            flag = "WARN " if ratio < 0.5 else "OK   "
            lines.append(
                f"- {flag} Active-BSSID trend (median of first-3 vs last-3 "
                f"full buckets): {first:.0f} → {last:.0f} (ratio {ratio:.2f})"
            )
        elif result["death_at_sec"] is None and not result.get("interrupted"):
            lines.append("- Long-run too short for trend (need ≥6 full buckets).")
        return lines

    def report_lines(self, result, args) -> list[str]:
        if not result or not result["buckets"]:
            return []
        bucket_sec = result["bucket_sec"]
        full_threshold = max(1, bucket_sec // 2)
        lines: list[str] = [
            f"## Section 2 - Long-run degradation ({bucket_sec}s buckets)",
            "",
            "| # | Window (s) | Active total | 2.4 GHz | 5 GHz | Frames | Notes |",
            "|---:|---:|---:|---:|---:|---:|:---|",
        ]
        for i, b in enumerate(result["buckets"]):
            note = "" if b["snapshot_count"] >= full_threshold else "partial"
            lines.append(
                f"| {i + 1} | {b['start_sec']}–{b['end_sec']} | "
                f"{b['active_total']} | {b['active_24']} | "
                f"{b['active_5']} | {b['frames_total']} | {note} |"
            )
        lines.append("")
        return lines

    def csv_section(self, w, result) -> None:
        if not result:
            return
        w.writerow(["# longrun (per-second snapshots)"])
        w.writerow(["t_sec", "channel", "frames_delta",
                    "active_total", "active_24", "active_5"])
        for s in result["snapshots"]:
            w.writerow([f"{s['t']:.1f}", s["channel"], s["frames_delta"],
                        s["active_total"], s["active_24"], s["active_5"]])
        w.writerow([])


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
            snapshot_count=len(snaps),
        ))
    return out


def _trend(buckets, bucket_sec: int):
    """First-3 vs last-3 median of active_total, but only over buckets
    that saw at least ``bucket_sec / 2`` snapshots. Returns
    ``(first, last, ratio)`` or ``None`` if fewer than 6 full buckets."""
    full_threshold = max(1, bucket_sec // 2)
    eligible = [b for b in buckets if b["snapshot_count"] >= full_threshold]
    if len(eligible) < 6:
        return None
    first = statistics.median(b["active_total"] for b in eligible[:3])
    last = statistics.median(b["active_total"] for b in eligible[-3:])
    if first <= 0:
        return None
    return first, last, last / first
