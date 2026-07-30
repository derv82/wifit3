"""Multi-card soak supervisor: run a long sweep on every connected card at once.

Discovers every supported card on the bus and launches one ``sweep.py`` subprocess per
physical card, each targeting its exact ``--instance BUS:ADDR`` (the only way to tell two
identical VID:PIDs apart) and running a ``--longrun-min`` degradation soak. Launches are
staggered (``--stagger``) so two USB cold-boots never overlap: two heavy bring-ups at once
collide on a shared hub (an mt76 FW upload times out, a Realtek card can lose its handle).

One process per card means one sink per card, so each card's active-BSSID soak stays clean
and comparable to its single-card baseline, and one card's RX-death or USB drop can't kill
the others. Physical USB contention and power draw across all cards is still present and real,
which is the point: this baselines the way people actually run wifit3, with several cards in.

Each subprocess writes its own report under ``scripts/diag/reports/`` (per sweep.py). This
supervisor tees each child's console output to ``reports/soak_<slug>_<bus>-<addr>.log`` and,
when everything finishes, prints each card's report path and exit code.

Run::

    uv run python scripts/diag/soak_all.py                 # 30-min soak, every card
    uv run python scripts/diag/soak_all.py --minutes 60
    uv run python scripts/diag/soak_all.py --card 8812     # only cards matching a substring
    uv run python scripts/diag/soak_all.py --list          # show what would run, then exit

Any flag this supervisor does not recognise is forwarded verbatim to every ``sweep.py``
child (e.g. ``--skip-baseline``, ``--bucket-sec 30``, ``--death-timeout-sec 0``).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))

from wifit3.wlan.discovery import find_devices  # noqa: E402

SWEEP = _HERE / "sweep.py"
REPORTS_DIR = _HERE / "reports"
_REPORT_LINE = re.compile(r"\[\+\] Report:\s*(.+)$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "card"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Launch a long sweep soak on every connected card at once, one subprocess each.",
    )
    p.add_argument("--minutes", type=float, default=30.0,
                   help="Soak duration per card (forwarded as sweep's --longrun-min). Default: 30.")
    p.add_argument("--card", default="",
                   help="Only soak cards whose description contains this substring (default: all).")
    p.add_argument("--stagger", type=float, default=8.0,
                   help="Seconds between subprocess launches, so cold-boots don't overlap. Default: 8.")
    p.add_argument("--list", action="store_true",
                   help="Print the cards that would be soaked, then exit.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print each sweep command without launching it.")
    return p


def _select(card: str):
    devs = find_devices()
    if card:
        devs = [d for d in devs if card.lower() in d.description.lower()]
    return devs


def main() -> int:
    known, extra = _build_parser().parse_known_args()

    devs = _select(known.card)
    if not devs:
        where = f" matching '{known.card}'" if known.card else ""
        print(f"[-] No supported cards{where} found on the bus.", file=sys.stderr)
        return 1

    print(f"[*] {len(devs)} card(s) to soak for {known.minutes:g} min each "
          f"(stagger {known.stagger:g}s):", file=sys.stderr)
    for d in devs:
        print(f"      {d.bus}:{d.address}  {d.description}", file=sys.stderr)
    if known.list:
        return 0

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []   # (dev, proc, log_handle, log_path)
    for i, d in enumerate(devs):
        instance = f"{d.bus}:{d.address}"
        cmd = [sys.executable, str(SWEEP), "--instance", instance,
               "--longrun-min", str(known.minutes), *extra]
        if known.dry_run:
            print(f"[dry-run] {' '.join(cmd)}", file=sys.stderr)
            continue
        log_path = REPORTS_DIR / f"soak_{_slug(d.description)}_{d.bus}-{d.address}.log"
        log = log_path.open("w", encoding="utf-8")
        # Tee each child's console to its own file: 4 concurrent sweeps would otherwise
        # interleave into unreadable garble on the shared terminal.
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        jobs.append((d, proc, log, log_path))
        print(f"[+] launched {instance} ({d.description}) -> {log_path.name} (pid {proc.pid})",
              file=sys.stderr)
        if i < len(devs) - 1:
            time.sleep(known.stagger)

    if known.dry_run or not jobs:
        return 0

    print(f"\n[*] {len(jobs)} soak(s) running. Waiting for all to finish "
          f"(Ctrl+C stops every card)...", file=sys.stderr)
    try:
        for _d, proc, _log, _lp in jobs:
            proc.wait()
    except KeyboardInterrupt:
        print("\n[!] Interrupt: stopping every soak...", file=sys.stderr)
        for _d, proc, _log, _lp in jobs:
            if proc.poll() is None:
                proc.terminate()
        for _d, proc, _log, _lp in jobs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("\n[*] Results:", file=sys.stderr)
    worst = 0
    for d, proc, log, log_path in jobs:
        log.close()
        report = _find_report(log_path)
        rc = proc.returncode if proc.returncode is not None else -1
        worst = max(worst, abs(rc))
        tag = "ok" if rc == 0 else f"rc={rc}"
        print(f"      {d.bus}:{d.address} {d.description}: {tag}", file=sys.stderr)
        print(f"        report: {report or f'(none; see {log_path.name})'}", file=sys.stderr)
    return 0 if worst == 0 else 2


def _find_report(log_path: Path) -> str | None:
    """The report path a sweep child printed (``[+] Report: ...``), read back from its log."""
    try:
        for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            m = _REPORT_LINE.search(line)
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        sys.exit(130)
