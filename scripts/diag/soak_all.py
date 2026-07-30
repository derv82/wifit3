"""Multi-card soak supervisor: run a long sweep on every connected card at once.

Discovers every supported card on the bus and runs one ``sweep.py`` subprocess per physical
card, each targeting its exact ``--instance BUS:ADDR`` (the only way to tell two identical
VID:PIDs apart) and running a ``--longrun-min`` degradation soak.

**Bring-ups are serialized; soaks run concurrently.** A cold-boot (FW upload + MAC/BB/RF
config) is the fragile part: two overlapping on the bus intermittently collide (an mt76 FW
upload times out, a Realtek card throws libusb ENOENT mid-config). So the supervisor brings up
ONE card at a time (it waits for sweep to touch a bring-up signal file before starting the
next) and only then lets the soaks overlap. A cold-boot that dies before the signal is retried
(``--retries``). The soak-time bus contention that follows is real and intended: that's the
multi-card load this tool exists to baseline.

One process per card means one sink per card, so each card's active-BSSID soak stays clean and
one card's RX-death or USB drop can't kill the others. Each subprocess writes its own report
under ``scripts/diag/reports/``; this supervisor tees each child's console to
``reports/soak_<slug>_<bus>-<addr>.log`` and prints each card's report path + exit code at the end.

Run::

    uv run python scripts/diag/soak_all.py                 # 30-min soak, every card
    uv run python scripts/diag/soak_all.py --minutes 60
    uv run python scripts/diag/soak_all.py --card 8812     # only cards matching a substring
    uv run python scripts/diag/soak_all.py --list          # show what would run, then exit

Any flag this supervisor does not recognise is forwarded verbatim to every ``sweep.py`` child
(e.g. ``--skip-baseline``, ``--bucket-sec 30``, ``--death-timeout-sec 0``).
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
_RETRY_SETTLE = 3.0   # seconds to let a card recover before re-attempting a failed bring-up


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "card"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Launch a long sweep soak on every connected card, serializing bring-ups.",
    )
    p.add_argument("--minutes", type=float, default=30.0,
                   help="Soak duration per card (forwarded as sweep's --longrun-min). Default: 30.")
    p.add_argument("--card", default="",
                   help="Only soak cards whose description contains this substring (default: all).")
    p.add_argument("--retries", type=int, default=2,
                   help="Extra bring-up attempts when a card's cold-boot fails before it soaks. "
                        "Default: 2.")
    p.add_argument("--bringup-timeout", type=float, default=120.0,
                   help="Seconds to wait for a card's bring-up before calling the attempt failed. "
                        "Default: 120.")
    p.add_argument("--settle", type=float, default=2.0,
                   help="Seconds to let the bus quiesce after each bring-up before the next card "
                        "cold-boots. Default: 2.")
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


def _paths(d) -> tuple[Path, Path]:
    """(console-log path, bring-up-signal path) for one card."""
    stem = f"{_slug(d.description)}_{d.bus}-{d.address}"
    return REPORTS_DIR / f"soak_{stem}.log", REPORTS_DIR / f".bringup_{stem}"


def _sweep_cmd(d, extra, minutes: float, signal_path: Path) -> list[str]:
    return [sys.executable, str(SWEEP), "--instance", f"{d.bus}:{d.address}",
            "--longrun-min", str(minutes), "--bringup-signal", str(signal_path), *extra]


def _kill(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _await_bringup(proc, signal_path: Path, timeout: float) -> bool:
    """Block until sweep touches ``signal_path`` (bring-up done -> True) or the process exits /
    times out (failed -> False)."""
    deadline = time.monotonic() + timeout
    while True:
        if signal_path.exists():
            return True
        if proc.poll() is not None:
            return signal_path.exists()   # touch + exit can race
        if time.monotonic() > deadline:
            return False
        time.sleep(0.3)


def _bring_up(d, extra, opts):
    """Serialized launch + retry. Starts d's sweep and waits for its bring-up signal, so the NEXT
    card cold-boots alone on the bus. A cold-boot that dies before the signal (the RTL8188EUS
    intermittent ENOENT, an mt76 FW-upload timeout) is relaunched up to ``opts.retries`` times.
    Returns the running (d, proc, log, log_path) job once soaking, or None if every attempt failed."""
    instance = f"{d.bus}:{d.address}"
    log_path, signal_path = _paths(d)
    for attempt in range(opts.retries + 1):
        print(f"[*] {instance} ({d.description}) bringing up "
              f"(attempt {attempt + 1}/{opts.retries + 1})...", file=sys.stderr)
        signal_path.unlink(missing_ok=True)   # clear any stale signal from a prior attempt
        log = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(_sweep_cmd(d, extra, opts.minutes, signal_path),
                                stdout=log, stderr=subprocess.STDOUT)
        if _await_bringup(proc, signal_path, opts.bringup_timeout):
            signal_path.unlink(missing_ok=True)
            print(f"[+] {instance} up, soaking -> {log_path.name} (pid {proc.pid})", file=sys.stderr)
            return (d, proc, log, log_path)
        _kill(proc)
        log.close()
        more = attempt < opts.retries
        print(f"[!] {instance} bring-up failed (rc={proc.returncode}); "
              f"{'retrying' if more else 'giving up'}", file=sys.stderr)
        if more:
            time.sleep(_RETRY_SETTLE)
    signal_path.unlink(missing_ok=True)
    return None


def main() -> int:
    known, extra = _build_parser().parse_known_args()

    devs = _select(known.card)
    if not devs:
        where = f" matching '{known.card}'" if known.card else ""
        print(f"[-] No supported cards{where} found on the bus.", file=sys.stderr)
        return 1

    print(f"[*] {len(devs)} card(s) to soak for {known.minutes:g} min each "
          f"(bring-ups serialized, {known.retries} retries):", file=sys.stderr)
    for d in devs:
        print(f"      {d.bus}:{d.address}  {d.description}", file=sys.stderr)
    if known.list:
        return 0
    if known.dry_run:
        for d in devs:
            _log, signal_path = _paths(d)
            print(f"[dry-run] {' '.join(_sweep_cmd(d, extra, known.minutes, signal_path))}",
                  file=sys.stderr)
        return 0

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []      # (d, proc, log, log_path) for cards that came up and are soaking
    failed = []    # d for cards that never came up
    try:
        for i, d in enumerate(devs):
            job = _bring_up(d, extra, known)
            if job is None:
                failed.append(d)
                continue
            jobs.append(job)
            if i < len(devs) - 1 and known.settle > 0:
                time.sleep(known.settle)   # let the bus quiesce before the next cold-boot

        if not jobs:
            print("[-] No card completed bring-up.", file=sys.stderr)
            return 1

        print(f"\n[*] {len(jobs)} soak(s) running concurrently. Waiting for all to finish "
              f"(Ctrl+C stops every card)...", file=sys.stderr)
        for _d, proc, _log, _lp in jobs:
            proc.wait()
    except KeyboardInterrupt:
        print("\n[!] Interrupt: stopping every soak...", file=sys.stderr)
        for _d, proc, _log, _lp in jobs:
            _kill(proc)

    print("\n[*] Results:", file=sys.stderr)
    worst = 0
    for d, proc, log, log_path in jobs:
        log.close()
        report = _find_report(log_path)
        rc = proc.returncode if proc.returncode is not None else -1
        worst = max(worst, abs(rc))
        print(f"      {d.bus}:{d.address} {d.description}: {'ok' if rc == 0 else f'rc={rc}'}",
              file=sys.stderr)
        print(f"        report: {report or f'(none; see {log_path.name})'}", file=sys.stderr)
    for d in failed:
        worst = max(worst, 1)
        print(f"      {d.bus}:{d.address} {d.description}: BRING-UP FAILED (all attempts)",
              file=sys.stderr)
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
