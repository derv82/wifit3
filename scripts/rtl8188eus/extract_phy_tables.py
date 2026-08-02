"""Mechanically extract PHY init tables from the kernel rtl8xxxu driver.

Re-run after kernel updates:

    uv run python scripts/rtl8188eus/extract_phy_tables.py

Reads:
    driver_sources/rtl8xxxu-source-v6.18/8188e.c

Writes:
    src/wifit3/chips/rtl8188eus/phy_tables.py

Tables extracted (sentinels stripped):
    rtl8188eu_phy_init_table  (192 × {u16 addr, u32 val})  → PHY_INIT_TABLE_8188E
    rtl8188e_agc_table        (130 × {u16 addr, u32 val})  → AGC_TABLE_8188E
    rtl8188eu_radioa_init_table (95 × {u8 addr, u32 val})  → RADIO_A_INIT_TABLE_8188E
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path("driver_sources/rtl8xxxu-source-v6.18/8188e.c")
OUT = Path("src/wifit3/chips/rtl8188eus/phy_tables.py")


def extract(src: str, table_name: str, sentinel_addr: int) -> list[tuple[int, int]]:
    m = re.search(
        r"static const struct \w+ " + re.escape(table_name) + r"\[\] = \{(.*?)\n\};",
        src,
        re.DOTALL,
    )
    if not m:
        raise ValueError(f"table {table_name} not found in source")
    body = m.group(1)
    pairs = re.findall(r"\{(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}", body)
    out: list[tuple[int, int]] = []
    for r, v in pairs:
        ri = int(r, 16)
        vi = int(v, 16)
        if ri == sentinel_addr and (vi & 0xFF) == 0xFF:
            break
        out.append((ri, vi))
    return out


def emit(lines: list[str], name: str, table: list[tuple[int, int]], addr_width: int) -> None:
    fmt = f"0x{{0:0{addr_width}X}}"
    lines.append(f"{name}: tuple[tuple[int, int], ...] = (")
    for i in range(0, len(table), 4):
        chunk = table[i : i + 4]
        line = "    " + ", ".join(f"({fmt.format(r)}, 0x{v:08X})" for r, v in chunk) + ","
        lines.append(line)
    lines.append(")")
    lines.append("")


def main() -> None:
    src = SRC.read_text()

    phy = extract(src, "rtl8188eu_phy_init_table", 0xFFFF)
    agc = extract(src, "rtl8188e_agc_table", 0xFFFF)
    rfa = extract(src, "rtl8188eu_radioa_init_table", 0xFF)

    print(f"phy_init_table: {len(phy)} entries (expected 192)")
    print(f"agc_table:      {len(agc)} entries (expected 130)")
    print(f"radioa_init:    {len(rfa)} entries (expected 95)")
    assert len(phy) == 192
    assert len(agc) == 130
    assert len(rfa) == 95

    lines = [
        '"""PHY init tables for RTL8188EUS, mechanically extracted from',
        "`driver_sources/rtl8xxxu-source-v6.18/8188e.c` by",
        "`scripts/rtl8188eus/extract_phy_tables.py`. Do not edit by hand:",
        "re-run the extractor instead if the kernel source updates.",
        "",
        "* PHY_INIT_TABLE_8188E       = rtl8188eu_phy_init_table   (BB regs 0x800-0xfac)",
        "* AGC_TABLE_8188E            = rtl8188e_agc_table         (AGC reg 0xc78 + a few)",
        "* RADIO_A_INIT_TABLE_8188E   = rtl8188eu_radioa_init_table (RF path A regs)",
        '"""',
        "from __future__ import annotations",
        "",
    ]
    emit(lines, "PHY_INIT_TABLE_8188E", phy, 3)
    emit(lines, "AGC_TABLE_8188E", agc, 3)
    emit(lines, "RADIO_A_INIT_TABLE_8188E", rfa, 2)

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
