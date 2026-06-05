"""Diff our port's real bring-up register writes against the morrownr vendor oracle.

  ours   : ref/ourport_bringup.txt          (from trace_bringup.py, live HW)
  oracle : ref/morrownr_capture2_bringup.txt (from pcap_regtrace.py, capture-2)

Compares the FINAL value written to each config register (last-write-wins), which
cancels out ordering / RMW / EDCCA-sweep noise. RF writes (BB 0x0C90 path A /
0x0E90 path B) are decoded to per-(path, rf_addr) granularity so each radio register
is compared on its own, not collapsed into the last RF write.

The EFUSE-read loop and FW-download dominate both captures with non-comparable
traffic, so the oracle is restricted to frame >= --oracle-min-frame (MAC init onward,
default 7187) and our trace is read from the ``mac_init`` marker onward.

Offline; no hardware.

    uv run python scripts/rtl8812au_dkms/diff_trace.py
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

REF = Path(__file__).parent / "ref"
_REG = re.compile(r"0x([0-9A-Fa-f]{3,4})\s*=\s*0x([0-9A-Fa-f]+)")
_FRAME = re.compile(r"frame=(\d+)")


def _key(addr: int, val: int) -> tuple[str, int]:
    """Normalize a raw (BB addr, dword) write to a comparison key + value."""
    if addr in (0x0C90, 0x0E90):
        p = "A" if addr == 0x0C90 else "B"
        return f"RF[{p}] 0x{(val >> 20) & 0xFF:02X}", val & 0xFFFFF
    return f"0x{addr:04X}", val


def parse(path: Path, min_frame: int = 0, start_marker: str | None = None):
    final: dict[str, int] = {}
    order: dict[str, int] = {}
    seq = 0
    started = start_marker is None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if start_marker and start_marker in line:
                started = True
            continue
        fm = _FRAME.search(line)
        if fm and int(fm.group(1)) < min_frame:
            continue
        if not started:
            continue
        m = _REG.search(line)   # first 0x..=0x.. on the line is the BB write
        if not m:
            continue
        key, val = _key(int(m.group(1), 16), int(m.group(2), 16))
        if key not in order:
            order[key] = seq
            seq += 1
        final[key] = val
    return final, order


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", type=Path, default=REF / "morrownr_capture2_bringup.txt")
    ap.add_argument("--ours", type=Path, default=REF / "ourport_bringup.txt")
    ap.add_argument("--oracle-min-frame", type=int, default=7187)
    args = ap.parse_args()

    oracle, o_ord = parse(args.oracle, min_frame=args.oracle_min_frame)
    ours, r_ord = parse(args.ours, start_marker="mac_init")

    ok, rk = set(oracle), set(ours)
    diffval = sorted((ok & rk) - {k for k in ok & rk if oracle[k] == ours[k]}, key=lambda k: o_ord[k])
    missing = sorted(ok - rk, key=lambda k: o_ord[k])   # vendor writes, we never do
    extra = sorted(rk - ok, key=lambda k: r_ord[k])     # we write, vendor never does

    def is_rf(k: str) -> bool:
        return k.startswith("RF[")

    print("=== config-register diff: ours vs morrownr oracle ===")
    print(f"  comparable registers: ours={len(ours)}  oracle={len(oracle)}")
    print(f"  differing final value : {len(diffval)}   ({sum(is_rf(k) for k in diffval)} RF)")
    print(f"  vendor writes we omit : {len(missing)}   ({sum(is_rf(k) for k in missing)} RF)")
    print(f"  we write, vendor n't  : {len(extra)}   ({sum(is_rf(k) for k in extra)} RF)")

    print("\n-- DIFFERING FINAL VALUES (RF first — the prime suspects) --")
    for k in sorted(diffval, key=lambda k: (not is_rf(k), o_ord[k])):
        print(f"  {k:13}  vendor=0x{oracle[k]:X}   ours=0x{ours[k]:X}")
    print("\n-- VENDOR WRITES WE OMIT --")
    for k in missing:
        print(f"  {k:13}  vendor=0x{oracle[k]:X}")
    print("\n-- WE WRITE, VENDOR DOESN'T --")
    for k in extra:
        print(f"  {k:13}  ours=0x{ours[k]:X}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
