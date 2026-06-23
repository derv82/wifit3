"""Throwaway wire analyzer for the rtl8821cu_dkms operational region.

Rebuilds the merged ctrl+bulk op stream (same as verify_pcap). Modes:
  (default)  print ops [start:end], 0x4e0 mirror filtered unless --all
  --led      tabulate every 0x004e read->write pair (the LED on/off sequence)
  --rf18     tabulate every RF 0x18 channel write (the hop sequence)

    uv run python scripts/rtl8821cu_dkms/dump_ops.py --led
    uv run python scripts/rtl8821cu_dkms/dump_ops.py 8149 8300
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402

CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"


def _fmt(op: dict) -> str:
    if op.get("dir") == "BULK":
        return f"BULK[{len(op['data'])}B]"
    d = op.get("data", b"")
    val = f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d else ""
    return f"{op['dir']} 0x{op['wval']:04x}/{op['width']}{val}"


def _ops():
    dev = rp.find_card_device(CAP)
    return rp.merge_ops_by_frame(rp.extract_ctrl_ops(CAP, dev),
                                 rp.extract_bulk_out_ops(CAP, dev))


def main() -> int:
    ops = _ops()
    if "--led" in sys.argv:
        prev_w = None
        for k, o in enumerate(ops):
            if o.get("wval") == 0x004E and o["width"] == 1:
                v = int.from_bytes(o.get("data", b"\x00"), "little")
                d = "IN " if o["dir"] == "IN" else "OUT"
                note = ""
                if o["dir"] == "OUT":
                    state = "OFF" if (v & 0x8) else "ON "
                    note = f"  -> LED {state}" + ("  (no-change)" if v == prev_w else "")
                    prev_w = v
                print(f"[{k:5}] f{o['frame']:<7} {d} 0x004e=0x{v:02x}{note}")
        return 0
    if "--rf18" in sys.argv:
        for k, o in enumerate(ops):
            if o["dir"] == "OUT" and o.get("wval") == 0x0C90 and o["width"] == 4:
                v = int.from_bytes(o["data"], "little")
                if (v >> 20) == 0x18:               # LSSI addr field == RF 0x18
                    print(f"[{k:5}] f{o['frame']:<7} write_rf 0x18 = 0x{v & 0xFFFFF:05x} "
                          f"(ch {v & 0xFF})")
        return 0
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 8149
    end = int(sys.argv[2]) if len(sys.argv) > 2 else start + 160
    for k in range(start, min(end, len(ops))):
        o = ops[k]
        if "--all" not in sys.argv and o.get("wval") == 0x04E0:
            continue
        tag = " *" if o.get("wval") == 0x04E0 else ""
        print(f"[{k:5}] f{o['frame']:<7} {_fmt(o)}{tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
