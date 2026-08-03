"""Report renderers (markdown + CSV) walk each probe asking for
its own verdict/report/csv lines, in registry order.

Each probe is responsible for its own section's structure. The renderer
only owns the file-level header (device metadata + timestamp + run
parameters) and the trailing "raw data" hint.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any


def _chipset_slug(iface) -> str:
    cls_name = type(iface.driver).__name__
    return cls_name.lower().removesuffix("driver")


def write_markdown(
    report_path: Path,
    *,
    iface,
    args,
    probe_results: list[tuple[Any, Any]],  # list of (probe, result)
) -> None:
    chipset = _chipset_slug(iface)
    lines: list[str] = []
    lines.append(f"# Diagnostic soak: {chipset}")
    lines.append("")
    lines.append(f"- **Device**: {iface.description}")
    lines.append(f"- **Driver**: {type(iface.driver).__name__}")
    lines.append(
        f"- **Timestamp**: {datetime.now().isoformat(timespec='seconds')}"
    )
    lines.append(f"- **Channels**: {args.channels_used}")
    if getattr(args, "duration_multiplier", 1.0) != 1.0:
        lines.append(
            f"- **Duration multiplier**: {args.duration_multiplier}×"
        )
    # Active-probe parameter summary: keep this stable for the
    # byte-for-byte compat target (baseline + longrun only, mult=1).
    baseline_part = (
        f"**Baseline**: {args.dwell_sec}s/ch"
        if hasattr(args, "dwell_sec") else None
    )
    longrun_part = (
        f"**Long-run**: {args.longrun_min}m, hop {args.hop_interval}s, "
        f"{args.bucket_sec}s buckets"
        if hasattr(args, "longrun_min") else None
    )
    pieces = [p for p in (baseline_part, longrun_part) if p]
    if pieces:
        lines.append("- " + "  ·  ".join(pieces))
    lines.append("")

    if getattr(args, "interrupted", False):
        lines.append(
            "> **NOTE (INTERRUPTED):** this run was cut short by Ctrl+C. "
            "Every section below reflects partial state; the long-run "
            "snapshot table is whatever survived up to the interrupt."
        )
        lines.append("")
    elif getattr(args, "crashed_with", None):
        lines.append(
            f"> **NOTE (CRASHED):** this run aborted before completing "
            f"every probe. Cause: `{args.crashed_with}`. The most common "
            f"trigger is a USB disconnect mid-run; the partial state "
            f"each probe collected before the crash is preserved below."
        )
        lines.append("")

    lines.append("## Quick verdict")
    lines.append("")
    for probe, result in probe_results:
        for ln in probe.verdict_lines(result, args):
            lines.append(ln)
    lines.append("")

    for probe, result in probe_results:
        lines.extend(probe.report_lines(result, args))

    lines.append("---")
    lines.append("")
    lines.append(
        "Raw per-second data is in the sibling CSV. Re-run with "
        "`--longrun-min 30` for a deeper degradation signal, or "
        "`--duration-multiplier 3` to scale every probe's duration "
        "without changing each flag individually."
    )
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(
    csv_path: Path,
    *,
    probe_results: list[tuple[Any, Any]],
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for probe, result in probe_results:
            probe.csv_section(w, result)
