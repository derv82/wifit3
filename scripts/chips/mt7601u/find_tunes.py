"""Find each MT7601U channel tune in a usbmon pcap and dump its op sequence.

A tune is delimited by the MT_TX_ALC_CFG_0 read that mt7601u_phy_set_txpower
opens with (phy.c), so content delimits the runs without depending on timestamps.

Usage: uv run python scratch/find_tunes.py <pcap> [--dump N]
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

OFF_TYPE, OFF_XFER, OFF_EP, OFF_DEV = 8, 9, 10, 11
OFF_LENCAP, OFF_SETUP, OFF_DATA = 36, 40, 64
SUBMIT = 0x53


def parse_pcapng(path: str) -> list[bytes]:
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def detect_card(pkts: list[bytes]) -> int:
    from collections import Counter
    counts: Counter = Counter()
    for p in pkts:
        if len(p) < 48 or p[OFF_XFER] != 2 or p[OFF_TYPE] != SUBMIT:
            continue
        bm, br = p[OFF_SETUP], p[OFF_SETUP + 1]
        if (bm & 0x60) == 0x40 and br in (0x07, 0x02, 0x42):
            counts[p[OFF_DEV]] += 1
    return counts.most_common(1)[0][0]


def ops_of(pkts: list[bytes], dev: int) -> list[tuple[str, int, int]]:
    """(kind, wIndex, wValue) for the host-issued ops of one device."""
    out = []
    for p in pkts:
        if len(p) < 48 or p[OFF_DEV] != dev:
            continue
        if p[OFF_XFER] == 2 and p[OFF_TYPE] == SUBMIT:
            bm, br, wv, wi, _wl = struct.unpack_from("<BBHHH", p, OFF_SETUP)
            if (bm & 0x60) != 0x40:
                continue
            out.append(({0x07: "read", 0x02: "write", 0x42: "fce"}[br], wi, wv))
        elif p[OFF_XFER] == 3 and p[OFF_EP] == 0x08 and p[OFF_TYPE] == SUBMIT:
            out.append(("bulk_out", struct.unpack_from("<I", p, OFF_LENCAP)[0], 0))
    return out


MT_TX_ALC_CFG_0 = 0x13B0


def main() -> None:
    pkts = parse_pcapng(sys.argv[1])
    dev = detect_card(pkts)
    ops = ops_of(pkts, dev)
    starts = [i for i, (k, wi, _v) in enumerate(ops) if k == "read" and wi == MT_TX_ALC_CFG_0]
    print(f"card dev={dev}: {len(ops)} ops, {len(starts)} MT_TX_ALC_CFG_0 reads")
    if len(starts) < 2:
        print("no tune runs found")
        return
    runs = list(zip(starts, starts[1:] + [len(ops)]))
    print(f"{len(runs)} candidate tune runs")
    for n, (a, b) in enumerate(runs):
        print(f"  run {n}: ops {a}..{b - 1} ({b - a} ops)")

    if "--dump" in sys.argv:
        which = int(sys.argv[sys.argv.index("--dump") + 1])
        a, b = runs[which]
        print(f"\n--- run {which}: {b - a} ops ---")
        for kind, a1, a2 in ops[a:b]:
            print(f"    {kind:8s} 0x{a1:06x}" + (f"=0x{a2:04x}" if kind != "bulk_out" else "B"))


if __name__ == "__main__":
    main()