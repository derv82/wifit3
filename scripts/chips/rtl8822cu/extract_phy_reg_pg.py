"""Mechanical transcription: parse array_mp_8822c_phy_reg_pg out of the vendor C and emit
the Python literal, plus counts and a checksum for verification."""
import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "driver_captures/captures_rtl88x2cu/driver-source/hal/phydm/rtl8822c/halhwimg8822c_bb.c"

text = SRC.read_text(errors="replace")
m = re.search(r"const u32 array_mp_8822c_phy_reg_pg\[\] = \{(.*?)\n\};", text, re.S)
body = m.group(1)
start_line = text[:m.start()].count("\n") + 1
end_line = text[:m.end()].count("\n") + 1
vals = [int(t, 0) for t in re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", body)]

print(f"source lines {start_line}..{end_line}")
print("u32 count:", len(vals), "rows:", len(vals) / 6)
print("sum:", hex(sum(vals)))
print("sha256:", hashlib.sha256(",".join(f"{v:#010x}" for v in vals).encode()).hexdigest())

if "--emit" in sys.argv:
    out = []
    for i in range(0, len(vals), 6):
        b, p, t, a, msk, d = vals[i:i + 6]
        out.append(f"    {b}, {p}, {t}, {a:#010x}, {msk:#010x}, {d:#010x},")
    print("\n".join(out))
