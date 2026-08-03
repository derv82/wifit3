"""Yield-per-channel baseline probe.

For each supported channel: tune, dwell N seconds, count raw RX frames
and the BSSIDs whose beacon advertises this channel. Silent channels
surface tune-time bugs (wrong centre-freq write, wrong RF table for
this band, RXEN gated off after retune, etc.).
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from typing import Any

from .base import FrameCounter, Probe, classify_band


class BaselineProbe(Probe):
    name = "baseline"

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dwell-sec", type=float, default=10.0,
            help="Seconds to dwell on each channel during baseline.",
        )

    def is_enabled(self, args: argparse.Namespace) -> bool:
        return not args.skip_baseline

    def apply_multiplier(self, args: argparse.Namespace, mult: float) -> None:
        args.dwell_sec = args.dwell_sec * mult

    def attach(self, iface) -> None:
        pass

    async def run(self, iface, args: argparse.Namespace) -> Any:
        channels = args.channels_list
        dwell_sec = args.dwell_sec

        counter = FrameCounter()
        iface.register_rx_callback(counter)
        results: list[dict] = []

        print(
            f"\n[*] Probe: yield baseline ({dwell_sec}s/ch on "
            f"{len(channels)} channels)",
            file=sys.stderr,
        )

        try:
            for ch in channels:
                ok = await iface.set_channel(ch)
                if not ok:
                    print(
                        f"  CH{ch:>3}: set_channel failed, skipping",
                        file=sys.stderr,
                    )
                    results.append(dict(
                        channel=ch, frames=0, bssids=0, new_bssids=0,
                        mean_rssi=None, tuned=False,
                    ))
                    continue

                await asyncio.sleep(0.25)  # AGC / pipe drain
                window_start = time.time()
                start_frames = counter.count
                pre_known = {
                    bssid for bssid, ap in list(args.array.access_points.items())
                    if ap.channel == ch
                }
                await asyncio.sleep(dwell_sec)
                frames = counter.count - start_frames

                active = [
                    ap for ap in list(args.array.access_points.values())
                    if ap.channel == ch and ap.last_seen >= window_start
                ]
                active_bssids = {ap.bssid for ap in active}
                mean_rssi = (
                    statistics.mean(ap.signal for ap in active) if active else None
                )
                results.append(dict(
                    channel=ch,
                    frames=frames,
                    bssids=len(active),
                    new_bssids=len(active_bssids - pre_known),
                    mean_rssi=mean_rssi,
                    tuned=True,
                ))
                rssi_str = "-" if mean_rssi is None else f"{mean_rssi:.0f}"
                print(
                    f"  CH{ch:>3} ({classify_band(ch)}): "
                    f"{frames:>6} frames · {len(active):>3} BSSIDs "
                    f"({len(active_bssids - pre_known)} new) · RSSI {rssi_str}",
                    file=sys.stderr,
                )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl+C in the middle of a dwell: the in-progress channel
            # was not appended to results, so it's silently dropped.
            # Every previously-completed channel is still in `results`.
            args.interrupted = True

        return results

    def finalize(self) -> Any:
        return None

    def verdict_lines(self, result, args) -> list[str]:
        if result is None:
            return ["- Baseline probe skipped."]
        silent = [r for r in result if r["tuned"] and r["frames"] == 0]
        untunable = [r for r in result if not r["tuned"]]
        lines: list[str] = []
        if silent:
            chans = ", ".join(str(r["channel"]) for r in silent)
            lines.append(f"- WARN  Silent channels (zero frames during dwell): {chans}")
        else:
            lines.append("- OK    Every tuned channel saw at least one frame.")
        if untunable:
            chans = ", ".join(str(r["channel"]) for r in untunable)
            lines.append(f"- WARN  Tune failures: {chans}")
        return lines

    def report_lines(self, result, args) -> list[str]:
        if not result:
            return []
        lines: list[str] = [
            "## Section 1 - Yield-per-channel baseline",
            "",
            "| Channel | Band | Frames | BSSIDs | New | Mean RSSI |",
            "|---:|:---:|---:|---:|---:|---:|",
        ]
        for r in result:
            rssi = "-" if r["mean_rssi"] is None else f"{r['mean_rssi']:.0f}"
            tag = "" if r["tuned"] else " (tune failed)"
            lines.append(
                f"| {r['channel']} | {classify_band(r['channel'])} "
                f"| {r['frames']} | {r['bssids']} | {r['new_bssids']} | {rssi} |{tag}"
            )
        lines.append("")
        return lines

    def csv_section(self, w, result) -> None:
        if result is None:
            return
        w.writerow(["# baseline"])
        w.writerow(["channel", "frames", "bssids", "new_bssids",
                    "mean_rssi", "tuned"])
        for r in result:
            rssi = "" if r["mean_rssi"] is None else f"{r['mean_rssi']:.1f}"
            w.writerow([r["channel"], r["frames"], r["bssids"],
                        r["new_bssids"], rssi, r["tuned"]])
        w.writerow([])
