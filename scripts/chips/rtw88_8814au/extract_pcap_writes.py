"""Dump the kernel's ordered register-write sequence from a cold-boot pcap.

Every rtw88 register access is a vendor control transfer (bmRequestType=0x40
write / 0xC0 read, bRequest=0x05, wValue=addr). This walks a usbmon pcap and
prints the WRITES in order as (frame, addr, len, value), collapsing long runs
of BB/RF/AGC *table* writes into one summary line so the control-plane init
(power-seq, MAC, RX/agg setup) is readable.

Goal: diff against our driver's init to find missing/wrong steps.

Usage:
    uv run scripts/chips/rtw88_8814au/extract_pcap_writes.py \
        driver_captures/captures_rtw88_8814au/capture-1.pcap [--all] [--max-frame N]
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "rtl8822bu"))
from extract_rtl8822bu_fw import _iter_urbs  # noqa: E402

# Address ranges that are bulk init *tables* (BB/AGC/RF/EFUSE), collapsed in
# the default view so the discrete control writes stand out.
def _is_table_addr(a: int) -> bool:
    return (0x0800 <= a <= 0x1FFF) or a == 0x0030  # BB/RF/AGC space + EFUSE_CTRL


def iter_writes(pcap: Path, max_frame: int | None, min_frame: int | None = None):
    for fn, data in _iter_urbs(pcap):
        if max_frame is not None and fn > max_frame:
            return
        if min_frame is not None and fn < min_frame:
            continue
        if len(data) < 48 or data[9] != 2 or data[8] != ord("S"):
            continue
        if data[40] != 0x40 or data[41] != 0x05:   # vendor write submit
            continue
        addr = struct.unpack_from("<H", data, 42)[0]
        wlen = struct.unpack_from("<H", data, 46)[0]
        payload = data[64:64 + wlen]
        val = int.from_bytes(payload, "little") if payload else 0
        yield fn, addr, wlen, val


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pcap", type=Path)
    p.add_argument("--all", action="store_true", help="don't collapse table runs")
    p.add_argument("--max-frame", type=int, default=None)
    p.add_argument("--min-frame", type=int, default=None)
    args = p.parse_args()

    run_count = run_lo = run_hi = 0
    total = 0

    def flush_run():
        nonlocal run_count
        if run_count:
            print(f"    [... {run_count} table writes, addr 0x{run_lo:04x}..0x{run_hi:04x}]")
            run_count = 0

    for fn, addr, wlen, val in iter_writes(args.pcap, args.max_frame, args.min_frame):
        total += 1
        if not args.all and _is_table_addr(addr):
            if run_count == 0:
                run_lo = run_hi = addr
            run_lo, run_hi = min(run_lo, addr), max(run_hi, addr)
            run_count += 1
            continue
        flush_run()
        print(f"  f{fn:<5d} W{wlen} 0x{addr:04x} = 0x{val:0{wlen * 2}x}")
    flush_run()
    print(f"\n  total writes: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
