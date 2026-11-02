"""Replay the driver's bring-up against a capture of a warm (already initialized) chip.

    uv run python scripts/chips/rtl8188ftv_dkms/verify_warm.py <warm-capture.pcap>

Runs ``Rtl8188ftvDkmsDriver._bring_up`` against ``ReplayTransport`` from the card's first vendor
op, so the driver's own ``power.is_chip_warm`` picks the branch from the recorded reads. Checks
the bring-up only; the ops after it are counted, not walked (run verify_pcap.py for those).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "rtl8188ftv_dkms_verify_pcap", Path(__file__).resolve().parent / "verify_pcap.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)
rp = gate.rp


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the rtl8188ftv_dkms bring-up against a warm-chip capture.")
    parser.add_argument("pcap", type=Path, help="vendor-driver USB capture of a warm chip")
    args = parser.parse_args(argv)
    if not args.pcap.exists():
        print(f"FAIL: no such capture {args.pcap}")
        return 1
    time.sleep = lambda *a, **k: None
    dev = rp.find_card_device(args.pcap)
    ops = rp.extract_ops(args.pcap, dev)
    if not ops:
        print("FAIL: no vendor ops")
        return 1
    w = gate.Walk(ops)
    try:
        asyncio.run(w.run(lambda: w.driver._bring_up(gate.ignore_progress), "_bring_up"))
    except (rp.Divergence, gate.HandlerFailed) as e:
        first = gate.first_divergence(e)
        at = f" at {gate.fmt_op(ops[w.i])}" if w.i < len(ops) else ""
        print(f"FAIL after {w.i} matched ops{at}:\n  {type(first).__name__}: {first}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR (harness/port bug) after {w.i} ops: {type(e).__name__}: {e}")
        return 2
    print(f"warm: driver._bring_up reproduced {w.i} ops of {args.pcap.name} "
          f"({w.spliced_ticks} spliced watchdog ticks); {len(ops) - w.i} later ops not checked here")
    print("PASS: bring-up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
