"""Plot beacon_trend.py --json output.

OUT.png: one row per JSON file, one column per card; the target AP's beacons/s over the run
(per-second points, 30 s rolling mean per phase, scans shaded, each dwell's mean and % of 9.77/s).
OUT-first60.png: the first 60 s of every dwell, one row per arm (file stem minus a trailing -r<N>).

    uv run --with matplotlib python scripts/rx/plot_trend.py OUT.png master-r1.json master-r2.json pr165-r1.json
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

EXPECTED_PER_SEC = 1000 / 102.4
ROLLING_SECONDS = 30
FIRST_SECONDS = 60
FIRST_ROLLING_SECONDS = 5
SETTLE_SECONDS = 10
Y_MAX = 10.5
COLUMN_INCHES = 6
ROW_INCHES = 3.5

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
SCAN_SHADE = "#d9d8d3"
SERIES = "#2a78d6"
REPETITION_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


def rolling_mean(values: list[int], window: int) -> list[float]:
    """Centered rolling mean; the window shrinks at the ends."""
    half = window // 2
    means = []
    for index in range(len(values)):
        chunk = values[max(0, index - half):index + half + 1]
        means.append(sum(chunk) / len(chunk))
    return means


def arm_and_repetition(stem: str) -> tuple[str, str]:
    """('master', 'r2') for 'master-r2'; (stem, stem) when there is no -r<N> suffix."""
    match = re.fullmatch(r"(.+)-(r\d+)", stem)
    if match is None:
        return stem, stem
    return match.group(1), match.group(2)


def style_axes(axes, title: str) -> None:
    axes.axhline(EXPECTED_PER_SEC, color=TEXT_SECONDARY, linewidth=1, linestyle="--", zorder=1)
    axes.text(0.995, EXPECTED_PER_SEC + 0.08, f"expected {EXPECTED_PER_SEC:.2f}/s",
              transform=axes.get_yaxis_transform(), ha="right", va="bottom", fontsize=8,
              color=TEXT_SECONDARY)
    axes.set_ylim(0, Y_MAX)
    axes.set_title(title, fontsize=11, color=TEXT_PRIMARY, loc="left")
    axes.set_facecolor(SURFACE)
    axes.grid(axis="y", color=GRID, linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(GRID)
    axes.tick_params(colors=TEXT_SECONDARY, labelsize=9)


def plot_run_panel(axes, doc: dict, label: str, title: str) -> None:
    for phase in doc["phases"]:
        counts = phase["cards"][label]["target_beacons_per_sec"]
        minutes = [(phase["start_s"] + second) / 60 for second in range(len(counts))]
        start_minute = phase["start_s"] / 60
        end_minute = (phase["start_s"] + phase["seconds"]) / 60
        if phase["is_scan"]:
            axes.axvspan(start_minute, end_minute, color=SCAN_SHADE, alpha=0.5, linewidth=0, zorder=0)
            axes.text((start_minute + end_minute) / 2, Y_MAX - 0.25, "scan", ha="center", va="top",
                      fontsize=8, color=TEXT_SECONDARY)
        axes.scatter(minutes, counts, s=6, color=SERIES, alpha=0.18, linewidths=0, zorder=2)
        axes.plot(minutes, rolling_mean(counts, ROLLING_SECONDS), color=SERIES, linewidth=2,
                  solid_capstyle="round", solid_joinstyle="round", zorder=3)
        if not phase["is_scan"] and counts:
            mean = statistics.mean(counts)
            axes.text((start_minute + end_minute) / 2, 0.35,
                      f"mean {mean:.2f}/s  ({mean / EXPECTED_PER_SEC:.0%})",
                      ha="center", va="bottom", fontsize=9, color=TEXT_PRIMARY, zorder=4,
                      bbox={"boxstyle": "round,pad=0.25", "facecolor": SURFACE, "edgecolor": GRID})
    style_axes(axes, title)


def plot_first_panel(axes, runs: list[tuple[str, dict]], label: str, title: str,
                     repetition_colors: dict[str, str]) -> None:
    """runs: (repetition, doc) pairs of one arm; overlays the first 60 s of every dwell."""
    settle_counts = []
    steady_counts = []
    for repetition, doc in runs:
        color = repetition_colors[repetition]
        for phase in doc["phases"]:
            if phase["is_scan"]:
                continue
            counts = phase["cards"][label]["target_beacons_per_sec"][:FIRST_SECONDS]
            is_bringup = phase["cycle"] == 0
            if is_bringup:
                settle_counts.extend(counts[:SETTLE_SECONDS])
                steady_counts.extend(counts[SETTLE_SECONDS:])
            seconds = list(range(len(counts)))
            axes.scatter(seconds, counts, s=8, color=color, alpha=0.2, linewidths=0, zorder=2)
            axes.plot(seconds, rolling_mean(counts, FIRST_ROLLING_SECONDS), color=color,
                      linewidth=2 if is_bringup else 1.5, linestyle="-" if is_bringup else (0, (4, 2)),
                      solid_capstyle="round", solid_joinstyle="round", zorder=4 if is_bringup else 3)
    axes.axvline(SETTLE_SECONDS, color=GRID, linewidth=1, zorder=1)
    if settle_counts and steady_counts:
        settle_mean = statistics.mean(settle_counts)
        steady_mean = statistics.mean(steady_counts)
        axes.text(0.5, 0.35,
                  f"bring-up  0-{SETTLE_SECONDS} s: {settle_mean:.2f}/s ({settle_mean / EXPECTED_PER_SEC:.0%})"
                  f"   {SETTLE_SECONDS}-{FIRST_SECONDS} s: {steady_mean:.2f}/s "
                  f"({steady_mean / EXPECTED_PER_SEC:.0%})",
                  transform=axes.get_yaxis_transform(), ha="center", va="bottom", fontsize=9, color=TEXT_PRIMARY, zorder=5,
                  bbox={"boxstyle": "round,pad=0.25", "facecolor": SURFACE, "edgecolor": GRID})
    axes.set_xlim(0, FIRST_SECONDS)
    style_axes(axes, title)


def write_run_figure(out_path: Path, json_paths: list[Path], docs: list[dict], labels: list[str]) -> None:
    figure, axes_grid = plt.subplots(len(docs), len(labels), squeeze=False, sharex=True, sharey=True,
                                     figsize=(COLUMN_INCHES * len(labels), ROW_INCHES * len(docs)),
                                     facecolor=SURFACE)
    for row, (path, doc) in enumerate(zip(json_paths, docs)):
        for column, label in enumerate(labels):
            axes = axes_grid[row][column]
            plot_run_panel(axes, doc, label, f"{path.stem}: {label}")
            if column == 0:
                axes.set_ylabel("target beacons/s", fontsize=9, color=TEXT_SECONDARY)
            if row == len(docs) - 1:
                axes.set_xlabel("minutes from run start", fontsize=9, color=TEXT_SECONDARY)
    figure.tight_layout()
    figure.savefig(out_path, dpi=110, facecolor=SURFACE)
    plt.close(figure)


def write_first_figure(out_path: Path, json_paths: list[Path], docs: list[dict], labels: list[str]) -> None:
    runs_by_arm: dict[str, list[tuple[str, dict]]] = {}
    repetition_colors: dict[str, str] = {}
    for path, doc in zip(json_paths, docs):
        arm, repetition = arm_and_repetition(path.stem)
        runs_by_arm.setdefault(arm, []).append((repetition, doc))
        if repetition not in repetition_colors:
            repetition_colors[repetition] = REPETITION_COLORS[len(repetition_colors) % len(REPETITION_COLORS)]
    figure, axes_grid = plt.subplots(len(runs_by_arm), len(labels), squeeze=False, sharex=True, sharey=True,
                                     figsize=(COLUMN_INCHES * len(labels), ROW_INCHES * len(runs_by_arm)),
                                     facecolor=SURFACE)
    for row, (arm, runs) in enumerate(runs_by_arm.items()):
        for column, label in enumerate(labels):
            axes = axes_grid[row][column]
            plot_first_panel(axes, runs, label, f"{arm}: {label}", repetition_colors)
            if column == 0:
                axes.set_ylabel("target beacons/s", fontsize=9, color=TEXT_SECONDARY)
            if row == len(runs_by_arm) - 1:
                axes.set_xlabel("seconds since the dwell started", fontsize=9, color=TEXT_SECONDARY)
    handles = [Line2D([], [], color=color, linewidth=2, label=repetition)
               for repetition, color in repetition_colors.items()]
    handles.append(Line2D([], [], color=TEXT_SECONDARY, linewidth=2, label="bring-up dwell"))
    handles.append(Line2D([], [], color=TEXT_SECONDARY, linewidth=1.5, linestyle=(0, (4, 2)),
                          label="dwell after scan"))
    figure.legend(handles=handles, loc="upper center", ncol=len(handles), frameon=False, fontsize=9,
                  labelcolor=TEXT_PRIMARY, title=f"{FIRST_ROLLING_SECONDS} s rolling mean", title_fontsize=9)
    figure.tight_layout(rect=(0, 0, 1, 1 - 0.5 / (ROW_INCHES * len(runs_by_arm))))
    figure.savefig(out_path, dpi=110, facecolor=SURFACE)
    plt.close(figure)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    out_path = Path(sys.argv[1])
    json_paths = [Path(arg) for arg in sys.argv[2:]]
    docs = [json.loads(path.read_text()) for path in json_paths]
    labels = [card["label"] for card in docs[0]["cards"]]
    write_run_figure(out_path, json_paths, docs, labels)
    first_path = out_path.with_name(f"{out_path.stem}-first60{out_path.suffix}")
    write_first_figure(first_path, json_paths, docs, labels)
    print(out_path)
    print(first_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
