"""Extract the 8821a BB PHY_REG, BB AGC_TAB, and RF RadioA tables from vendor C.

Each is a flat u32 array (incl. phy_cond IF/ELSE rows); the runtime phy_cond
walker resolves them. This dumps the raw u32 stream verbatim into _tbl.py modules.

Run: uv run python scripts/chips/rtl8821au_dkms/extract_bb_rf_tables.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "driver_captures/captures_rtl8821au/driver-source/hal/phydm/rtl8821a"
OUTDIR = REPO / "src/wifit3/chips/rtl8821au_dkms"

TABLES = [
    ("halhwimg8821a_bb.c", "array_mp_8821a_phy_reg", "bb_phy_reg_tbl.py", "BB_PHY_REG"),
    ("halhwimg8821a_bb.c", "array_mp_8821a_agc_tab", "bb_agc_tbl.py", "BB_AGC_TAB"),
    ("halhwimg8821a_rf.c", "array_mp_8821a_radioa", "rf_radioa_tbl.py", "RF_RADIOA"),
]


def extract_one(fname, arrname, outname, varname) -> bool:
    text = (SRC / fname).read_text(errors="replace")
    m = re.search(arrname + r"\[\]\s*=\s*\{(.*?)\}\s*;", text, re.S)
    if not m:
        print(f"FAIL: {arrname}[] not found in {fname}")
        return False
    vals = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]+)", m.group(1))]
    if len(vals) % 2:
        print(f"FAIL: {arrname} odd u32 count {len(vals)}")
        return False
    lines = [
        f"# Auto-extracted from vendor C array {arrname}[] ({fname}).",
        "# Raw u32 stream (incl. phy_cond IF/ELSE rows); resolved by phy_cond.apply_table.",
        "# Regenerate with scripts/chips/rtl8821au_dkms/extract_bb_rf_tables.py.",
        f"{varname} = (",
    ]
    for j in range(0, len(vals), 8):
        lines.append("    " + ", ".join(f"0x{v:08X}" for v in vals[j:j + 8]) + ",")
    lines += [")", ""]
    (OUTDIR / outname).write_text("\n".join(lines))
    print(f"wrote {outname} ({len(vals)} u32, {len(vals)//2} rows)")
    return True


def main() -> int:
    ok = all(extract_one(*t) for t in TABLES)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
