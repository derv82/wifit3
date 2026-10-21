from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "driver_sources/rtl8xxxu-source-v7.2.6/drivers/net/wireless/realtek/rtl8xxxu/8192f.c"
OUTPUT = REPO / "src/wifit3/chips/rtl8192fu/tables.py"
TABLES = (
    "rtl8192f_mac_init_table",
    "rtl8192fu_phy_init_table",
    "rtl8192f_agc_table",
    "rtl8192fu_radioa_init_table",
    "rtl8192fu_radiob_init_table",
)


def extract_table(source: str, name: str) -> list[tuple[int, int]]:
    match = re.search(rf"{name}\[\]\s*=\s*\{{(.*?)\n\}};", source, re.DOTALL)
    if match is None:
        raise RuntimeError(f"table not found: {name}")
    entries = []
    for register, value in re.findall(r"\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}", match.group(1)):
        parsed = (int(register, 16), int(value, 16))
        if parsed in ((0xFFFF, 0xFF), (0xFFFF, 0xFFFFFFFF), (0xFF, 0xFFFFFFFF)):
            break
        entries.append(parsed)
    return entries


def main() -> None:
    source = SOURCE.read_text()
    lines = ["# Generated from Linux v7.2.6 rtl8xxxu/8192f.c.\n"]
    for name in TABLES:
        python_name = name.removeprefix("rtl8192f").removeprefix("u_").upper()
        lines.append(f"\n{python_name} = (\n")
        for register, value in extract_table(source, name):
            lines.append(f"    (0x{register:04X}, 0x{value:X}),\n")
        lines.append(")\n")
    OUTPUT.write_text("".join(lines))


if __name__ == "__main__":
    main()
