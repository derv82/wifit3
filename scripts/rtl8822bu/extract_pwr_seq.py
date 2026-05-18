"""Extract rtw_pwr_seq_cmd tables for 8822b from the kernel C source.

Parses `data_dumps/rtw88-source-v6.18/rtw8822b.c` and emits Python tuples
matching `wifit3.chips.rtw88_base.power_seq` conventions:

    (offset, cut_mask, intf_mask, base, cmd, mask, value)

Writes to `src/wifit3/chips/rtl8822bu/assets/pwr_seq.py`. Re-run whenever
the upstream kernel driver changes.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Symbolic name → integer value, taken from `data_dumps/rtw88-source-v6.18/main.h`.
SYMBOL_TABLE = {
    # Cut masks (main.h:946..954)
    "RTW_PWR_CUT_TEST_MSK": 0x01,
    "RTW_PWR_CUT_A_MSK": 0x02,
    "RTW_PWR_CUT_B_MSK": 0x04,
    "RTW_PWR_CUT_C_MSK": 0x08,
    "RTW_PWR_CUT_D_MSK": 0x10,
    "RTW_PWR_CUT_E_MSK": 0x20,
    "RTW_PWR_CUT_F_MSK": 0x40,
    "RTW_PWR_CUT_G_MSK": 0x80,
    "RTW_PWR_CUT_ALL_MSK": 0xFF,
    # Interface masks (main.h:941..944)
    "RTW_PWR_INTF_SDIO_MSK": 0x01,
    "RTW_PWR_INTF_USB_MSK": 0x02,
    "RTW_PWR_INTF_PCI_MSK": 0x04,
    "RTW_PWR_INTF_ALL_MSK": 0x0F,
    # Address bases (main.h:935..937)
    "RTW_PWR_ADDR_MAC": 0x00,
    "RTW_PWR_ADDR_USB": 0x01,
    "RTW_PWR_ADDR_PCI": 0x02,
    "RTW_PWR_ADDR_SDIO": 0x03,
    # Commands (main.h:929..933)
    "RTW_PWR_CMD_READ": 0x00,
    "RTW_PWR_CMD_WRITE": 0x01,
    "RTW_PWR_CMD_POLLING": 0x02,
    "RTW_PWR_CMD_DELAY": 0x03,
    "RTW_PWR_CMD_END": 0x04,
    # Delay units (main.h:957..958)
    "RTW_PWR_DELAY_US": 0x00,
    "RTW_PWR_DELAY_MS": 0x01,
}


def evaluate_expr(expr: str) -> int:
    """Evaluate a C bit-or/BIT()/hex-literal expression."""
    expr = expr.strip()
    # BIT(n) → (1<<n)
    expr = re.sub(r"BIT\((\d+)\)", r"(1<<\1)", expr)
    # Replace each symbol with its value.
    for name in sorted(SYMBOL_TABLE, key=len, reverse=True):
        expr = re.sub(rf"\b{re.escape(name)}\b", str(SYMBOL_TABLE[name]), expr)
    # Strip C casts (none expected here, but be safe).
    expr = re.sub(r"\(unsigned\s+\w+\)|\(u\d+\)|\(int\)", "", expr)
    # Validate: only digits, hex, parens, ops, whitespace.
    if not re.fullmatch(r"[0-9xXaAbBcCdDeEfF\s+\-*/()|&<>~]*", expr):
        raise ValueError(f"unevaluable expression: {expr!r}")
    return eval(expr, {"__builtins__": {}}) & 0xFFFFFFFF


def parse_table(source: str, table_name: str) -> list[tuple[int, ...]]:
    """Find `static const struct rtw_pwr_seq_cmd <name>[] = { ... };` and parse."""
    pattern = re.compile(
        rf"static\s+const\s+struct\s+rtw_pwr_seq_cmd\s+{re.escape(table_name)}\s*\[\]\s*=\s*\{{(.*?)\}};",
        re.DOTALL,
    )
    m = pattern.search(source)
    if not m:
        raise ValueError(f"table not found: {table_name}")
    body = m.group(1)

    # Each entry is `{ off, cut, intf, base, cmd, mask, value }` (7 fields).
    # Strip C-style comments first.
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.DOTALL)
    body = re.sub(r"//[^\n]*\n", "\n", body)

    entries: list[tuple[int, ...]] = []
    # Match `{ ... }` allowing nested parens inside fields.
    depth = 0
    cur = []
    buf = []
    for ch in body:
        if ch == "{":
            if depth == 0:
                buf = []
            else:
                buf.append(ch)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                cur.append("".join(buf))
            else:
                buf.append(ch)
        elif depth >= 1:
            buf.append(ch)

    for entry in cur:
        # Split top-level commas (no nested parens at this level).
        fields = []
        pdepth = 0
        cur_f = []
        for ch in entry:
            if ch == "(":
                pdepth += 1
                cur_f.append(ch)
            elif ch == ")":
                pdepth -= 1
                cur_f.append(ch)
            elif ch == "," and pdepth == 0:
                fields.append("".join(cur_f))
                cur_f = []
            else:
                cur_f.append(ch)
        if cur_f:
            fields.append("".join(cur_f))
        if len(fields) != 7:
            raise ValueError(f"entry has {len(fields)} fields, expected 7: {entry!r}")
        values = tuple(evaluate_expr(f) for f in fields)
        entries.append(values)
    return entries


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("source", type=Path,
                   help="Path to rtw8822b.c")
    p.add_argument("output", type=Path,
                   help="Output Python module path")
    args = p.parse_args()

    text = args.source.read_text(encoding="utf-8")

    tables = [
        "trans_carddis_to_cardemu_8822b",
        "trans_cardemu_to_act_8822b",
        "trans_act_to_cardemu_8822b",
        "trans_cardemu_to_carddis_8822b",
    ]

    parsed = {name: parse_table(text, name) for name in tables}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        f.write('"""Power-on / power-off sub-sequences for RTL8822BU.\n')
        f.write("\n")
        f.write("Direct extraction of `rtw_pwr_seq_cmd` tables from\n")
        f.write("`data_dumps/rtw88-source-v6.18/rtw8822b.c` via\n")
        f.write("`scripts/rtl8822bu/extract_pwr_seq.py`. Each tuple is\n")
        f.write("(offset, cut_mask, intf_mask, base, cmd, mask, value) — matches\n")
        f.write("the C struct rtw_pwr_seq_cmd field order.\n")
        f.write('"""\n')
        f.write("from __future__ import annotations\n\n")
        for name, entries in parsed.items():
            f.write(f"{name.upper()} = (\n")
            for off, cm, im, base, cmd, mask, val in entries:
                f.write(
                    f"    (0x{off:04X}, 0x{cm:02X}, 0x{im:02X}, "
                    f"0x{base:02X}, 0x{cmd:02X}, 0x{mask:02X}, 0x{val:02X}),\n"
                )
            f.write(")\n\n")
        f.write("CARD_ENABLE_FLOW_8822B = (\n")
        f.write("    TRANS_CARDDIS_TO_CARDEMU_8822B,\n")
        f.write("    TRANS_CARDEMU_TO_ACT_8822B,\n")
        f.write(")\n\n")
        f.write("CARD_DISABLE_FLOW_8822B = (\n")
        f.write("    TRANS_ACT_TO_CARDEMU_8822B,\n")
        f.write("    TRANS_CARDEMU_TO_CARDDIS_8822B,\n")
        f.write(")\n")

    total = sum(len(e) for e in parsed.values())
    print(f"[+] Wrote {args.output} with {total} pwr_seq entries "
          f"across {len(parsed)} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
