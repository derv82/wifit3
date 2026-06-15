"""Byte-for-byte replay-diff of the rtl8822bu_dkms port vs the morrownr rtl88x2bu
cold-boot capture.

SCAFFOLD: capture parse + coverage audit are in place; the bring-up call sequence
is a TODO, filled milestone by milestone. Template: scripts/rtl8812au_dkms/verify_pcap.py.

PASS means only: for this captured boot, the port emits the same USB bytes the
vendor driver did — a faithfulness gate, not a correctness proof. Offline.

    uv run python scripts/verify_pcap.py rtl8822bu_dkms
    uv run python scripts/rtl8822bu_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402

CHANNEL = 1
DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_rtl88x2bu" / "capture-1.pcap"


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None              # replay needs no settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev_addr = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev_addr)
    ops = rp.extract_ops(pcap, dev_addr)
    n_w = sum(o["kind"] == "W" for o in ops)
    n_r = sum(o["kind"] == "R" for o in ops)
    n_b = sum(o["kind"] == "B" for o in ops)
    print(f"{pcap.name}: card=dev{dev_addr}, {len(ops)} vendor ops "
          f"({n_r} R, {n_w} W, {n_b} bulk)")
    if not ops:
        return 1
    print("  first 25 vendor ops:")
    for k, o in enumerate(ops[:25]):
        print(f"    [{k:3}] f{o['frame']:<7} {rp.ReplayTransport._fmt(o)}")

    # TODO: drive the bring-up against rp.ReplayTransport(ops), milestone by
    # milestone, catching rp.Divergence. Add a sibling verify_channels.py for the
    # per-hop tune. Run against capture-1/2/3. See RTL8822BU_DKMS.md and the
    # 8812au_dkms recipe.
    print("\nSCAFFOLD: bring-up not wired yet — capture parses; not a PASS.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
