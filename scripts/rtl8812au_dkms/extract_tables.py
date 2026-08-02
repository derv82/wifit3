"""Extract the RTL8812AU init tables + NIC firmware from the Lucid-Duck vendor C.

The 8812a is in the SAME vendor tree as the 8821a we already ported; its tables live
beside the 8821a ones. This dumps each into the chip package verbatim and prints a
sha256 so the transcription is golden-hash-checkable (no vendor 8812 pcap exists, so a
byte-exact source extraction is the static-data gate). Re-running must reproduce the
same hashes.

Tables (flat u32 stream incl. phy_cond IF/ELSE rows; resolved by phy_cond.apply_table):
  PHY_REG, AGC_TAB, RADIO_A, RADIO_B  (RADIO_B is new for the 2T2R 8812).
MAC table (flat (addr, byte) pairs, no opcode rows): MAC_REG.
Firmware (raw bytes): array_mp_8812a_fw_nic -> assets/rtl8812au_fw.bin.

Run: uv run python scripts/rtl8812au_dkms/extract_tables.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "driver_captures/captures_rtl8821au/driver-source"
PHYDM = SRC / "hal/phydm/rtl8812a"
OUTDIR = REPO / "src/wifit3/chips/rtl8812au_dkms"

# (src file, C array, out module, python var) — flat u32 stream tables resolved by the
# phy_cond walker. The MAC table is included here (unlike the 8821's flat pairs, the
# 8812 MAC table carries phy_cond IF/ELSE rows — e.g. an "if USB" branch on reg 0x11);
# its data rows are applied with write8 (odm_config_mac_8812a takes a u8), the others
# with write32 — that is a chip-module concern, not a table-format one.
U32_TABLES = [
    (PHYDM / "halhwimg8812a_mac.c", "array_mp_8812a_mac_reg", "mac_reg_tbl.py", "MAC_REG"),
    (PHYDM / "halhwimg8812a_bb.c", "array_mp_8812a_phy_reg", "bb_phy_reg_tbl.py", "BB_PHY_REG"),
    (PHYDM / "halhwimg8812a_bb.c", "array_mp_8812a_agc_tab", "bb_agc_tbl.py", "BB_AGC_TAB"),
    (PHYDM / "halhwimg8812a_rf.c", "array_mp_8812a_radioa", "rf_radioa_tbl.py", "RF_RADIOA"),
    (PHYDM / "halhwimg8812a_rf.c", "array_mp_8812a_radiob", "rf_radiob_tbl.py", "RF_RADIOB"),
]
FW = (SRC / "hal/rtl8812a/hal8812a_fw.c", "array_mp_8812a_fw_nic", "assets/rtl8812au_fw.bin")


def _array_body(path: Path, arrname: str) -> str:
    text = path.read_text(errors="replace")
    m = re.search(arrname + r"\[\]\s*=\s*\{(.*?)\}\s*;", text, re.S)
    if not m:
        raise SystemExit(f"FAIL: {arrname}[] not found in {path.name}")
    return m.group(1)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def extract_u32(path: Path, arrname: str, outname: str, varname: str) -> None:
    vals = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]+)", _array_body(path, arrname))]
    if len(vals) % 2:
        raise SystemExit(f"FAIL: {arrname} odd u32 count {len(vals)}")
    lines = [
        f"# Auto-extracted from vendor C array {arrname}[] ({path.name}).",
        "# Raw u32 stream (incl. phy_cond IF/ELSE rows); resolved by phy_cond.apply_table.",
        "# Regenerate with scripts/rtl8812au_dkms/extract_tables.py.",
        f"{varname} = (",
    ]
    for j in range(0, len(vals), 8):
        lines.append("    " + ", ".join(f"0x{v:08X}" for v in vals[j:j + 8]) + ",")
    lines += [")", ""]
    body = "\n".join(lines)
    (OUTDIR / outname).write_text(body)
    print(f"  {outname:22s} {len(vals):5d} u32 ({len(vals)//2} rows)  sha256={_sha(body.encode())[:16]}")


def extract_fw(path: Path, arrname: str, outname: str) -> None:
    nums = re.findall(r"0x([0-9A-Fa-f]{2})\b", _array_body(path, arrname))
    blob = bytes(int(b, 16) for b in nums)
    out = OUTDIR / outname
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    sig = int.from_bytes(blob[0:2], "little")
    print(f"  {outname:22s} {len(blob):5d} B (32 hdr + {len(blob)-32} body)  sha256={_sha(blob)[:16]}")
    print(f"    hdr signature=0x{sig:04x} (IS_FW_HEADER_EXIST_8812 wants 0x95x0), "
          f"first body byte=0x{blob[32]:02x} (expect 0x02 = 8051 LJMP)")


def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    print("RTL8812AU vendor-C extraction (golden hashes):")
    for path, arr, out, var in U32_TABLES:
        extract_u32(path, arr, out, var)
    extract_fw(*FW)
    return 0


if __name__ == "__main__":
    sys.exit(main())
