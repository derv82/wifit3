"""Diagnostic sweep: thin CLI over the probe registry.

Discovers a connected card, brings it up, runs every enabled probe in
``probes.ALL_PROBES`` order, and writes a Markdown report + sibling
CSV under ``scripts/diag/reports/``.

Run::

    uv run python scripts/diag/sweep.py
    uv run python scripts/diag/sweep.py --list-probes
    uv run python scripts/diag/sweep.py --skip-baseline --longrun-min 30
    uv run python scripts/diag/sweep.py --duration-multiplier 3

The probe registry (``probes/__init__.py``) is the source of truth for
what runs. Each probe owns its own CLI flags, verdict/report rendering,
and CSV section; sweep.py just orchestrates.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add src/ to sys.path so we can `import wifit3.*` without an editable
# install. AND make the parent of `probes/` importable so `from probes
# import ...` resolves; Python auto-adds the script's own dir to
# sys.path[0] when running ``python scripts/diag/sweep.py``, so this
# only matters for unusual invocations.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE))

from probes import ALL_PROBES  # noqa: E402
from report import write_csv, write_markdown  # noqa: E402

from wifit3.wlan.discovery import build_interfaces, close_interfaces  # noqa: E402

logger = logging.getLogger("diag.sweep")

REPORTS_DIR = _HERE / "reports"


def _chipset_slug(iface) -> str:
    cls_name = type(iface.driver).__name__
    return cls_name.lower().removesuffix("driver")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Wifit3 diagnostic sweep: runs every enabled probe in "
            "probes.ALL_PROBES against the first connected card and "
            "writes a Markdown report + CSV."
        ),
    )
    parser.add_argument(
        "--list-probes", action="store_true",
        help="Print the registered probes and exit.",
    )
    parser.add_argument(
        "--duration-multiplier", type=float, default=1.0,
        help="Scale every duration-style arg the probes own by this "
        "factor (e.g. 2 = run twice as long). Default: 1.0.",
    )
    parser.add_argument(
        "--channels", type=str, default=None,
        help="Comma-separated channels (override driver SUPPORTED_CHANNELS).",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG-level logging.",
    )
    # Every probe contributes:
    #  * a ``--skip-<probe.name>`` flag (skip_<sanitised_name>)
    #  * any probe-specific flags via probe.add_args
    for probe in ALL_PROBES:
        slug = probe.name.replace("-", "_")
        parser.add_argument(
            f"--skip-{probe.name}",
            dest=f"skip_{slug}",
            action="store_true",
            help=f"Skip the '{probe.name}' probe.",
        )
        probe.add_args(parser)
    return parser


async def _connect(iface) -> None:
    def _progress(pct: float, msg: str) -> None:
        print(f"  [{int(pct * 100):3d}%] {msg}", file=sys.stderr)

    print(
        f"[*] Bringing up {iface.name} ({iface.description})...",
        file=sys.stderr,
    )
    ok = await iface.connect(progress_cb=_progress)
    if not ok:
        raise RuntimeError("Driver bring-up returned False")


async def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_probes:
        for probe in ALL_PROBES:
            print(f"  {probe.name}")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Scale durations BEFORE probes are queried for their args, so the
    # report's metadata block reflects post-scale numbers.
    if args.duration_multiplier != 1.0:
        for probe in ALL_PROBES:
            probe.apply_multiplier(args, args.duration_multiplier)

    print("[*] Discovering interfaces...", file=sys.stderr)
    ifaces = build_interfaces()
    if not ifaces:
        print("[-] No supported devices found.", file=sys.stderr)
        return 1
    if len(ifaces) > 1:
        print(
            f"[!] Found {len(ifaces)} interfaces, using {ifaces[0].name}. "
            "Unplug others to test in isolation.",
            file=sys.stderr,
        )
    iface = ifaces[0]
    print(f"[+] Selected {iface.name}: {iface.description}", file=sys.stderr)

    try:
        await _connect(iface)
    except Exception as e:
        print(f"[-] Bring-up failed: {e}", file=sys.stderr)
        await close_interfaces(ifaces)
        return 1

    if args.channels:
        channels = [int(x.strip()) for x in args.channels.split(",") if x.strip()]
    else:
        channels = list(
            getattr(iface.driver, "SUPPORTED_CHANNELS", []) or [1, 6, 11]
        )
    args.channels_list = channels
    args.channels_used = ",".join(str(c) for c in channels)

    enabled = [p for p in ALL_PROBES if p.is_enabled(args)]

    # Attach phase: passive probes wire up their rx callbacks here
    # so they observe every frame the active probes generate.
    for probe in enabled:
        probe.attach(iface)

    # Run phase: active probes do their work; passive ones return
    # ``None`` and rely on finalize() to surface stats. We catch
    # everything (Ctrl+C, CancelledError, AND any exception) so the
    # report is still rendered with whatever partial state survived.
    # USB disconnects mid-run are the common non-Ctrl+C failure mode:
    # losing the report on a 10-min run that crashed at minute 8
    # is the exact pain point we're avoiding.
    probe_results: list[tuple] = []
    args.interrupted = False
    args.crashed_with: str | None = None
    try:
        for probe in enabled:
            try:
                result = await probe.run(iface, args)
            except (KeyboardInterrupt, asyncio.CancelledError):
                args.interrupted = True
                probe_results.append((probe, None))
                break
            except Exception as e:
                # USB disconnect, driver crash, etc. Preserve any
                # partial state the probe stashed before raising,
                # surface the cause in the report header.
                args.crashed_with = f"{probe.name}: {type(e).__name__}: {e}"
                print(
                    f"[!] {probe.name} crashed: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                probe_results.append((probe, None))
                break
            probe_results.append((probe, result))
    except (KeyboardInterrupt, asyncio.CancelledError):
        args.interrupted = True
    except Exception as e:
        args.crashed_with = f"orchestrator: {type(e).__name__}: {e}"
        print(
            f"[!] orchestrator crashed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )

    # close can itself raise on USB disconnect; isolate so a
    # teardown failure can't kill the render path.
    try:
        await close_interfaces(ifaces)
    except Exception as e:
        print(f"[!] close_interfaces() raised: {e}", file=sys.stderr)

    # Everything below this point runs unconditionally: the whole
    # point of the fix is that Ctrl+C / USB disconnect / driver
    # crash don't lose the report.

    # Finalize phase: passive probes' accumulated stats land here.
    # Active probes return ``None`` from finalize, so the run-phase
    # result is kept.
    finalized: list[tuple] = []
    for probe, run_result in probe_results:
        try:
            final = probe.finalize()
        except Exception as e:
            print(f"[!] {probe.name}.finalize() raised: {e}", file=sys.stderr)
            final = None
        finalized.append((probe, final if final is not None else run_result))
    probe_results = finalized

    # Disabled probes still contribute their "skipped" verdict line
    # so the report explicitly accounts for every probe in the registry.
    seen = {id(p) for p, _ in probe_results}
    for probe in ALL_PROBES:
        if id(probe) not in seen:
            probe_results.append((probe, None))

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = REPORTS_DIR / f"{_chipset_slug(iface)}_{ts}"
    report_path = base.with_suffix(".md")
    csv_path = base.with_suffix(".csv")
    try:
        write_csv(csv_path, probe_results=probe_results)
        write_markdown(
            report_path, iface=iface, args=args, probe_results=probe_results,
        )
    except Exception as e:
        print(f"[-] Render failed: {e}", file=sys.stderr)
        return 1

    if args.interrupted:
        print("\n[!] Interrupted: partial report below.", file=sys.stderr)
    elif args.crashed_with:
        print(
            f"\n[!] Crashed ({args.crashed_with}): partial report below.",
            file=sys.stderr,
        )
    print(f"[+] Report: {report_path}", file=sys.stderr)
    print(f"[+] CSV:    {csv_path}", file=sys.stderr)
    if args.interrupted:
        return 130
    if args.crashed_with:
        return 2
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        rc = 130
    sys.exit(rc)
