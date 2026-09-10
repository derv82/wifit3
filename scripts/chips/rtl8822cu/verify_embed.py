"""Diff the embedded ARRAY_MP_8822C_PHY_REG_PG against a fresh parse of the vendor C."""
import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from wifit3.chips.rtl8822cu.txpwr_tables import ARRAY_MP_8822C_PHY_REG_PG  # noqa: E402

SRC = REPO / "driver_captures/captures_rtl88x2cu/driver-source/hal/phydm/rtl8822c/halhwimg8822c_bb.c"
text = SRC.read_text(errors="replace")
m = re.search(r"const u32 array_mp_8822c_phy_reg_pg\[\] = \{(.*?)\n\};", text, re.S)
c_vals = tuple(int(t, 0) for t in re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", m.group(1)))

print("C entries:", len(c_vals), " embedded entries:", len(ARRAY_MP_8822C_PHY_REG_PG))
print("identical:", c_vals == ARRAY_MP_8822C_PHY_REG_PG)
print("sum:", hex(sum(ARRAY_MP_8822C_PHY_REG_PG)))
print("sha256:", hashlib.sha256(
    ",".join(f"{v:#010x}" for v in ARRAY_MP_8822C_PHY_REG_PG).encode()).hexdigest())
for i, (a, b) in enumerate(zip(c_vals, ARRAY_MP_8822C_PHY_REG_PG)):
    if a != b:
        print(f"  index {i}: C {a:#x} != embedded {b:#x}")
