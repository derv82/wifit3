"""usbmon pcap triage for the MT7601U port: device/op inventory + ordered stream.

Usage:
  uv run python scratch/usbmon.py <pcap> <devnum> [stream|addrs] [limit]
"""
from __future__ import annotations

import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

OFF_TYPE, OFF_XFER, OFF_EP, OFF_DEV = 8, 9, 10, 11
OFF_LENCAP, OFF_SETUP, OFF_DATA = 36, 40, 64
SUBMIT = 0x53
XNAME = {0: "ISO", 1: "INTR", 2: "CTRL", 3: "BULK"}


def parse_pcapng(path: str) -> list[tuple[float, bytes]]:
    """(epoch_seconds, usbmon_record) for every Enhanced Packet Block."""
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:
            _, ts_lo = struct.unpack_from("<II", data, off + 8 + 4)
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append((ts_lo / 1e6, data[off + 8 + 20: off + 8 + 20 + cap_len]))
        off += blen
    return pkts


def ctrl(p: bytes) -> tuple[int, int, int, int, int]:
    return struct.unpack_from("<BBHHH", p, OFF_SETUP)


def op_label(p: bytes) -> str:
    xfer, utype, ep = p[OFF_XFER], p[OFF_TYPE], p[OFF_EP]
    if xfer == 2 and utype == SUBMIT:
        bm, br, wv, wi, wl = ctrl(p)
        return f"ctrl bm{bm:02x}b{br:02x} wv{wv:04x} wi{wi:04x} wl{wl:04x}"
    lencap = struct.unpack_from("<I", p, OFF_LENCAP)[0]
    return f"{XNAME.get(xfer, xfer)}/{'S' if utype == SUBMIT else 'C'}/ep{ep:02x} len{lencap}"


def inventory(pkts: list[tuple[float, bytes]]) -> None:
    per_dev, kinds = Counter(), defaultdict(Counter)
    for _ts, p in pkts:
        if len(p) < 48:
            continue
        per_dev[p[OFF_DEV]] += 1
        kinds[p[OFF_DEV]][op_label(p)] += 1
    for dev, n in per_dev.most_common():
        print(f"\n=== dev {dev}: {n} records ===")
        for k, c in kinds[dev].most_common(20):
            print(f"  {c:5d}  {k}")


def stream(pkts: list[tuple[float, bytes]], dev: int, limit: int) -> None:
    t0 = pkts[0][0]
    rows = [(ts, p) for ts, p in pkts if len(p) >= 48 and p[OFF_DEV] == dev]
    print(f"dev {dev}: {len(rows)} records, t+{rows[0][0] - t0:.3f} .. t+{rows[-1][0] - t0:.3f}")
    for i, (ts, p) in enumerate(rows[:limit], 1):
        print(f"t+{ts - t0:7.3f} #{i:5d} {op_label(p)}")


def addrs(pkts: list[tuple[float, bytes]], dev: int) -> None:
    reads, writes, other = Counter(), Counter(), Counter()
    for _ts, p in pkts:
        if len(p) < 48 or p[OFF_XFER] != 2 or p[OFF_TYPE] != SUBMIT:
            continue
        if p[OFF_DEV] != dev:
            continue
        _bm, br, _wv, wi, _wl = ctrl(p)
        {7: reads, 2: writes}.get(br, other)[wi if br != 2 else wi] += 1
    print(f"READS br07: {sum(reads.values())} at {len(reads)} addrs")
    print("  " + "  ".join(f"{a:#06x}:{n}" for a, n in sorted(reads.items())))
    print(f"WRITES br02: {sum(writes.values())} at {len(writes)} addrs")
    print("  " + "  ".join(f"{a:#06x}:{n}" for a, n in sorted(writes.items())))
    print(f"OTHER: {dict(other)}")


def main() -> None:
    pkts = parse_pcapng(sys.argv[1])
    dev = int(sys.argv[2])
    mode = sys.argv[3] if len(sys.argv) > 3 else "stream"
    if mode == "addrs":
        addrs(pkts, dev)
    elif mode == "inventory":
        inventory(pkts)
    else:
        stream(pkts, dev, int(sys.argv[4]) if len(sys.argv) > 4 else 10_000)


if __name__ == "__main__":
    main()
