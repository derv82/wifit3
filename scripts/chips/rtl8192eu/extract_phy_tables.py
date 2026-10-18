"""Mechanically extract the RTL8192EU init tables from the kernel rtl8xxxu driver.

Re-run after kernel updates:

    uv run python scripts/chips/rtl8192eu/extract_phy_tables.py

Reads:
    driver_sources/rtl8xxxu-source-v6.19/8192e.c

Writes:
    src/wifit3/chips/rtl8192eu/phy_tables.py

`#ifdef EXT_PA_8192EU` blocks are resolved to their `#else` branch (the
part we ship ships an internal PA; EXT_PA is never defined).

Tables extracted (sentinels stripped):
    rtl8192e_mac_init_table          → MAC_INIT_TABLE_8192E     (u16 reg, u8 val)
    rtl8192eu_phy_init_table         → PHY_INIT_TABLE_8192E     (u16 reg, u32 val)
    rtl8xxx_agc_8192eu_std_table     → AGC_STD_TABLE_8192E      (u16 reg, u32 val)
    rtl8xxx_agc_8192eu_highpa_table  → AGC_HIGHP_A_TABLE_8192E  (u16 reg, u32 val)
    rtl8192eu_radioa_init_table      → RADIO_A_INIT_TABLE_8192E (u8  reg, u32 val)
    rtl8192eu_radiob_init_table      → RADIO_B_INIT_TABLE_8192E (u8  reg, u32 val)
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path("driver_sources/rtl8xxxu-source-v6.19/8192e.c")
OUT = Path("src/wifit3/chips/rtl8192eu/phy_tables.py")


def resolve_ext_pa(src: str) -> str:
    """Take the `#else` branch of every `#ifdef EXT_PA_8192EU` block."""
    pattern = re.compile(
        r"#ifdef EXT_PA_8192EU\n(.*?)#else\n(.*?)#endif\n", re.DOTALL
    )
    while pattern.search(src):
        src = pattern.sub(lambda m: m.group(2), src, count=1)
    return src


def extract(src: str, table_name: str, sentinel_addr: int) -> list[tuple[int, int]]:
    m = re.search(
        r"static const struct \w+ " + re.escape(table_name) + r"\[\] = \{(.*?)\n\};",
        src,
        re.DOTALL,
    )
    if not m:
        raise ValueError(f"table {table_name} not found in source")
    pairs = re.findall(
        r"\{(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}", m.group(1)
    )
    out: list[tuple[int, int]] = []
    for r, v in pairs:
        ri, vi = int(r, 16), int(v, 16)
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
    src = resolve_ext_pa(SRC.read_text())

    tables = {
        "MAC_INIT_TABLE_8192E": (extract(src, "rtl8192e_mac_init_table", 0xFFFF), 3),
        "PHY_INIT_TABLE_8192E": (extract(src, "rtl8192eu_phy_init_table", 0xFFFF), 3),
        "AGC_STD_TABLE_8192E": (extract(src, "rtl8xxx_agc_8192eu_std_table", 0xFFFF), 3),
        "AGC_HIGHP_A_TABLE_8192E": (
            extract(src, "rtl8xxx_agc_8192eu_highpa_table", 0xFFFF),
            3,
        ),
        "RADIO_A_INIT_TABLE_8192E": (
            extract(src, "rtl8192eu_radioa_init_table", 0xFF),
            2,
        ),
        "RADIO_B_INIT_TABLE_8192E": (
            extract(src, "rtl8192eu_radiob_init_table", 0xFF),
            2,
        ),
    }
    for name, (table, _) in tables.items():
        print(f"{name}: {len(table)} entries")

    lines = [
        '"""PHY init tables for RTL8192EU, mechanically extracted from',
        "`driver_sources/rtl8xxxu-source-v6.19/8192e.c` by",
        "`scripts/chips/rtl8192eu/extract_phy_tables.py`. Do not edit by hand:",
        "re-run the extractor instead if the kernel source updates.",
        "",
        "* MAC_INIT_TABLE_8192E       = rtl8192e_mac_init_table",
        "* PHY_INIT_TABLE_8192E       = rtl8192eu_phy_init_table   (BB regs)",
        "* AGC_STD_TABLE_8192E        = rtl8xxx_agc_8192eu_std_table",
        "* AGC_HIGHP_A_TABLE_8192E    = rtl8xxx_agc_8192eu_highpa_table",
        "* RADIO_A_INIT_TABLE_8192E   = rtl8192eu_radioa_init_table (RF path A)",
        "* RADIO_B_INIT_TABLE_8192E   = rtl8192eu_radiob_init_table (RF path B)",
        '"""',
        "from __future__ import annotations",
        "",
    ]
    for name, (table, width) in tables.items():
        emit(lines, name, table, width)

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
