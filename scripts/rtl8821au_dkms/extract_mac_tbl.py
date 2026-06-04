"""Extract the 8821a MAC register table from the vendor C array.

`array_mp_8821a_mac_reg[]` in `hal/phydm/rtl8821a/halhwimg8821a_mac.c` is a flat
list of u32 (addr, value) pairs applied as byte writes by ODM_ReadAndConfig. This
table contains no phy_cond opcode rows (every addr is a plain register <= 0x718),
so it reduces to a flat (addr, value8) write loop. Writes `mac_reg_tbl.py`.

Run: uv run python scripts/rtl8821au_dkms/extract_mac_tbl.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "usb_dumps_new/captures_rtl8821au/driver-source/hal/phydm/rtl8821a/halhwimg8821a_mac.c"
OUT = REPO / "src/wifit3/chips/rtl8821au_dkms/mac_reg_tbl.py"


def main() -> int:
    text = SRC.read_text(errors="replace")
    m = re.search(r"array_mp_8821a_mac_reg\[\]\s*=\s*\{(.*?)\}\s*;", text, re.S)
    if not m:
        print("FAIL: array_mp_8821a_mac_reg[] not found")
        return 1
    vals = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]+)", m.group(1))]
    if len(vals) % 2:
        print(f"FAIL: odd value count {len(vals)} (expected (addr,value) pairs)")
        return 1
    pairs = [(vals[i], vals[i + 1] & 0xFF) for i in range(0, len(vals), 2)]
    bad = [a for a, _ in pairs if a > 0x0FFF]
    if bad:
        print(f"FAIL: unexpected opcode/addr rows: {[hex(b) for b in bad]}")
        return 1

    lines = [
        "# Auto-extracted from halhwimg8821a_mac.c array_mp_8821a_mac_reg[] (8821a MAC_REG).",
        "# Flat (addr, byte-value) list applied in order via write8 by PHY_MACConfig8812.",
        "# Do not hand-edit; regenerate with scripts/rtl8821au_dkms/extract_mac_tbl.py.",
        "MAC_REG_TABLE = [",
    ]
    lines += [f"    (0x{a:04X}, 0x{v:02X})," for a, v in pairs]
    lines += ["]", ""]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT.relative_to(REPO)} ({len(pairs)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
