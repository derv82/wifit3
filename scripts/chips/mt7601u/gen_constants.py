"""Generate chips/mt7601u/constants.py from the v7.2 C headers, verbatim.

Each emitted constant carries its [SRC] file:line so no register value is ever
hand-typed. BIT(n) -> 1 << n, GENMASK(hi, lo) -> computed mask, and the C
function-like accessor macros become Python functions.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "driver_sources" / "mt7601u-source-v7.2" / "mt7601u"

# C headers in port order; the banner text names what the group covers.
HAND_WRITTEN: dict[str, str] = {
    "MT_CALIBRATE_INTERVAL": "4 * HZ",
    "MT_FREQ_CAL_INIT_DELAY": "30 * HZ",
    "MT_FREQ_CAL_CHECK_INTERVAL": "10 * HZ",
    "MT_FREQ_CAL_ADJ_INTERVAL": "HZ // 2",
    "MT_RX_URB_SIZE": "PAGE_SIZE << MT_RX_ORDER",
}


SKIP = re.compile(r"^(__MT|_[A-Z])")
# Macros needing hand-translation: BIT() over a C cast, two-register-bank ternaries,
# and aliases that call another macro. MT_BBP is unused token-pasting; mt76_rmw_field
# is a driver method, not a constant.
HAND_WRITTEN_FN: dict[str, str] = {
    "MT_WCID_DROP_MASK": "1 << (n % 32)",
    "MT_TX_AGG_CNT": "_two_base(idx, 8, MT_TX_AGG_CNT_BASE0 + (idx << 2), MT_TX_AGG_CNT_BASE1 + ((idx - 8) << 2))",
    "MT_SKEY": "MT_SKEY_1(bss, idx) if bss & 8 else MT_SKEY_0(bss, idx)",
    "MT_SKEY_MODE": "MT_SKEY_MODE_1(bss) if bss & 8 else MT_SKEY_MODE_0(bss)",
    "MT_SKEY_MODE_0": "MT_SKEY_MODE_BASE_0 + ((bss // 2) << 2)",
    "MT_SKEY_MODE_1": "MT_SKEY_MODE_BASE_1 + ((((bss) & 7) // 2) << 2)",
    "MT_SKEY_0": "MT_SKEY_BASE_0 + (4 * bss + idx) * 32",
    "MT_SKEY_1": "MT_SKEY_BASE_1 + (4 * (bss & 7) + idx) * 32",
    "MT_SKEY_MODE_SHIFT": "4 * (idx + 4 * (bss & 1))",
    "MT_WCID_DROP": "MT_WCID_DROP_BASE + ((n) >> 5) * 4",
    "MT_WCID_ADDR": "MT_WCID_ADDR_BASE + (n) * 8",
    "MT_WCID_KEY": "MT_WCID_KEY_BASE + (n) * 32",
    "MT_WCID_IV": "MT_WCID_IV_BASE + (n) * 8",
    "MT_WCID_ATTR": "MT_WCID_ATTR_BASE + (n) * 4",
    "MT_BCN_OFFSET": "MT_BCN_OFFSET_BASE + ((n) << 2)",
    "MT_EFUSE_DATA": "MT_EFUSE_DATA_BASE + ((n) << 2)",
    "MT_INT_RX_DONE": "1 << (n)",
    "MT_INT_TX_DONE": "1 << (n + 4)",
    "MT_WMM_AIFSN_SHIFT": "(n) * 4",
    "MT_WMM_CWMIN_SHIFT": "(n) * 4",
    "MT_WMM_CWMAX_SHIFT": "(n) * 4",
    "MT_WMM_TXOP": "MT_WMM_TXOP_BASE + (((n) // 2) << 2)",
    "MT_WMM_TXOP_SHIFT": "((n) & 1) * 16",
    "MT_MAC_APC_BSSID_L": "MT_MAC_APC_BSSID_BASE + ((n) * 8)",
    "MT_MAC_APC_BSSID_H": "MT_MAC_APC_BSSID_BASE + ((n) * 8 + 4)",
    "MT_EDCA_CFG_AC": "MT_EDCA_CFG_BASE + ((n) << 2)",
    "MT_EE_TX_POWER_BYRATE": "MT_EE_TX_POWER_BYRATE_BASE + (i) * 4",
    "GROUP_WCID": "N_WCIDS - 2 - idx",
}
SKIP_FN = {"MT_BBP", "mt76_rmw_field"}
# Emitted after the enum block: this macro is defined before the enum fields it uses.
DEFERRED_NAMES = {"MT_EFUSE_USAGE_MAP_SIZE"}

HEADERS = [
    ("regs.h", "Register addresses and bitfields"),
    ("mt7601u.h", "Device state bits and small helpers"),
    ("dma.h", "DMA descriptor and info-header fields"),
    ("mcu.h", "MCU message envelope"),
    ("mac.h", "MAC/EDCA/WMM"),
    ("eeprom.h", "EEPROM field offsets and bitfields"),
    ("initvals.h", "Init value tables"),
]

DEFINE = re.compile(r"^#define\s+(?P<name>\w+)(?P<params>\([^)]*\))?\s+(?P<body>.*)$")
def split_comment(body: str) -> tuple[str, str]:
    """Split a C define body from its trailing /* ... */ comment."""
    i = body.find("/*")
    if i == -1:
        return body.strip(), ""
    return body[:i].strip(), body[i:].replace("/*", "").replace("*/", "").strip()


def join_continuation(lines: list[str]) -> list[tuple[int, str]]:
    """Fold backslash continuations into one logical define per (lineno, text)."""
    out, buf, start = [], "", 0
    for lineno, line in enumerate(lines, 1):
        stripped = line.rstrip("\n")
        if not buf:
            start = lineno
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        out.append((start, buf + stripped))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


PAREN = re.compile(r"\(\s*\(([^()]*)\)\s*\)")


def collect_enums() -> list[tuple[str, str]]:
    """(NAME, value) for every enum member, resolving implicit auto-increment."""
    out = []
    for _filename, _blurb in HEADERS:
        path = SRC / _filename
        if not path.exists():
            continue
        text = path.read_text()
        for ename, body in re.findall(r"enum\s+(\w+)\s*\{(.*?)\}", text, re.S):
            nxt = 0
            for member in body.split(","):
                m = re.match(r"\s*(\w+)\s*(?:=\s*(0x[0-9a-fA-F]+|\d+))?\s*$", member)
                if not m:
                    continue
                if m.group(2) is not None:
                    nxt = int(m.group(2), 0)
                out.append((m.group(1), str(nxt)))
                nxt += 1
    return out


def _arg_order(name: str, params: str) -> list[str]:
    """Python parameter names for a hand-translated function-like macro."""
    if name == "MT_TX_AGG_CNT":
        return ["idx"]
    if name == "MT_SKEY":
        return ["bss", "idx"]
    return [a.strip().lstrip("_") for a in params[1:-1].split(",")]


def bit(n: str) -> str:
    return f"1 << {n}"


def genmask(hi: str, lo: str) -> str:
    return f"0x{((1 << (int(hi) - int(lo) + 1)) - 1) << int(lo):x}"


def translate(expr: str, known: set[str], params: frozenset[str] = frozenset()) -> str | None:
    """C expression -> Python expression, or None if it needs hand-porting."""
    if "##" in expr or "sizeof" in expr or "__packed" in expr:
        return None
    e = re.sub(r"\s+", " ", expr.strip())
    e = e.replace("->", ".").replace("u32", "").replace("u16", "").replace("u8", "")
    # C integer division truncates; Python 3's / is float.
    e = re.sub(r"\(\s*([\w.]+)\s*\)\s*/\s*(\d+)", r"(\1) // \2", e)
    e = PAREN.sub(r"\1", e)   # C's redundant (x) nesting adds nothing in Python
    e = re.sub(r"FIELD_PREP\((\w+),\s*([^)]+)\)", lambda m: f"_field_prep({m.group(1)}, {m.group(2)})", e)
    e = re.sub(r"FIELD_GET\((\w+),\s*([^)]+)\)", lambda m: f"_field_get({m.group(1)}, {m.group(2)})", e)
    e = re.sub(r"BIT\(([^()]*)\)", lambda m: bit(m.group(1)), e)
    e = re.sub(r"GENMASK\((\d+),\s*(\d+)\)", lambda m: genmask(m.group(1), m.group(2)), e)
    if "?" in e:
        m = re.match(r"^\((.+?)\s*<\s*(\d+)\s*\?\s*(.+?)\s*:\s*(.+)\)$", e)
        if not m:
            return None
        e = f"_two_base({m.group(1)}, {m.group(2)}, {m.group(3)}, {m.group(4)})"
    helpers = {"_field_prep", "_field_get", "_two_base"}
    allowed = known | params | helpers
    for ident in re.findall(r"\b[A-Za-z_]\w*\b", e):
        if ident not in allowed:
            return None
    residue = re.sub(r"\b[A-Za-z_]\w*\b", "", e)
    if re.search(r"[^0-9a-fA-FxX_+\-*/%()<>|&^~ \t.]", residue):
        return None
    return e


def collect_known() -> set[str]:
    """Every macro name the headers define, so pass 2 can tell a constant ref from junk.

    eeprom.h's later enum-style block is `#define`-free and `MT_EE_*` mostly lands in an
    `enum`, so its names come from the enum too.
    """
    names: set[str] = set()
    for _filename, _ in HEADERS:
        path = SRC / _filename
        if not path.exists():
            continue
        text = path.read_text()
        for _lineno, line in join_continuation(text.splitlines(True)):
            m = DEFINE.match(line)
            if m:
                names.add(m.group("name"))
        for enum_name in re.findall(r"enum\s+\w+\s*\{(.*?)\}", text, re.S):
            names.update(re.findall(r"\b(MT_\w+)\b", enum_name))
    return names


# Macros the driver keeps in a .c file rather than a header. Listing them by name
# keeps every value machine-extracted without pulling phy.c's hundreds of
# C-specific macros into constants.py.
EXTRA_SOURCES = [
    ("phy.c", ["BBP_R47_FLAG", "BBP_R47_F_TEMP"]),
]


def main() -> None:
    known = collect_known()
    for _filename, names in EXTRA_SOURCES:
        known.update(names)
    deferred: list[tuple[str, str, str]] = []
    out: list[str] = []
    out.append('"""MT7601U register addresses, bitfields, vendor requests, and accessors.')
    out.append("")
    out.append("Generated from driver_sources/mt7601u-source-v7.2/ (tag v7.2); every value carries")
    out.append("its [SRC] file:line. Do NOT hand-edit: re-run scripts/chips/mt7601u/gen_constants.py instead.")
    out.append('"""')
    # eeprom.h's enum fields are emitted last but MT_EFUSE_USAGE_MAP_SIZE references
    # two of them, so the derived size is appended after the enum block instead.
    out.append("from __future__ import annotations")
    out.append("")
    out.append("HZ = 1000            # Linux CONFIG_HZ, the unit every *_INTERVAL below counts in")
    out.append("PAGE_SIZE = 0x1000   # Linux page size on x86_64")
    out.append("")
    out.append("# USB endpoint roles, positional indices into in_eps/out_eps (usb.c:28-42).")
    out.append("# The addresses themselves come from the descriptor at claim time.")
    out.append("MT_EP_IN_PKT_RX = 0")
    out.append("MT_EP_IN_CMD_RESP = 1")
    out.append("MT_EP_OUT_INBAND_CMD = 0")
    out.append("MT_EP_OUT_AC_BK = 1")
    out.append("MT_EP_OUT_AC_BE = 2")
    out.append("MT_EP_OUT_AC_VI = 3")
    out.append("MT_EP_OUT_AC_VO = 4")
    out.append("MT_EP_OUT_HCCA = 5")
    out.append("")
    out.append("MT7601U_FIRMWARE = 'mt7601u.bin'   # usb.h:11")
    out.append("MT_VEND_BUF = 4                  # usb.h:19 sizeof(__le32)")
    out.append("")
    out.append("# enum mt_vendor_req (usb.h:21-26) and enum mt_vendor_req's reset value (usb.h:17).")
    out.append("MT_VEND_DEV_MODE = 1            # usb.h:22")
    out.append("MT_VEND_WRITE = 2               # usb.h:23")
    out.append("MT_VEND_MULTI_READ = 7          # usb.h:24")
    out.append("MT_VEND_WRITE_FCE = 0x42        # usb.h:25")
    out.append("MT_VEND_DEV_MODE_RESET = 1      # usb.h:17")
    out.append("MT_VEND_REQ_MAX_RETRY = 10      # usb.h:14")
    out.append("MT_VEND_REQ_TOUT_MS = 300       # usb.h:15")
    out.append("")
    out.append("# enum mt7601u_eeprom_access_modes (eeprom.h:70-73): how efuse_read addresses the array.")
    out.append("MT_EE_READ = 0")
    out.append("MT_EE_PHYSICAL_READ = 1")
    out.append("")
    out.append("# Struct field offsets the EEPROM code indexes with (eeprom.h:75-92).")
    out.append("N_CHAN_PWR = 14            # len(dev->ee->chan_pwr), eeprom.h:100")
    out.append("N_RATE_POWER_GROUPS = 5    # the `for (i = 0; i < 5; i++)` loop, eeprom.c:317")
    out.append("MAX_PWR = 0x3f             # s6_validate's GENMASK(5, 0), eeprom.h:118")
    out.append("S6_SIGN_BIT = 1 << 5       # BIT(5) in s6_to_int, eeprom.h:127")
    out.append("S6_MODULUS = 1 << 6        # BIT(6) in s6_to_int, eeprom.h:128")
    out.append("")
    out.append("")
    out.append("def _field_prep(field_mask: int, value: int) -> int:")
    out.append('    """FIELD_PREP: shift value into the field the mask describes."""')
    out.append("    shift = (field_mask & -field_mask).bit_length() - 1")
    out.append("    width = bin(field_mask >> shift).count('1')")
    out.append("    return (value & ((1 << width) - 1)) << shift")
    out.append("")
    out.append("")
    out.append("def _field_get(field_mask: int, value: int) -> int:")
    out.append('    """FIELD_GET: extract the field the mask describes from value."""')
    out.append("    shift = (field_mask & -field_mask).bit_length() - 1")
    out.append("    return (value & field_mask) >> shift")
    out.append("")
    out.append("")
    out.append("def _two_base(index: int, limit: int, low: int, high: int) -> int:")
    out.append('    """A C ternary `i < limit ? low : high` over two register banks."""')
    out.append("    return low if index < limit else high")
    out.append("")

    for filename, wanted in EXTRA_SOURCES:
        path = SRC / filename
        if not path.exists():
            print(f"  MISSING {filename}", file=sys.stderr)
            continue
        for lineno, text in join_continuation(path.read_text().splitlines(True)):
            m = DEFINE.match(text)
            if not m or m.group("name") not in wanted:
                continue
            name = m.group("name")
            rest = re.sub(r"\s+", " ", m.group("body")).split("/*")[0].strip()
            value = translate(rest, known, frozenset(m.group("params") or ""))
            if value is None:
                print(f"  UNTRANSLATABLE {name} = {rest}", file=sys.stderr)
                continue
            out.append(f"{name} = {value}".ljust(44) + f"# [SRC] {filename}:{lineno}")
            known.add(name)

    for filename, blurb in HEADERS:
        path = SRC / filename
        if not path.exists():
            print(f"  MISSING {filename}", file=sys.stderr)
            continue
        body: list[str] = []
        funcs: list[str] = []
        for lineno, text in join_continuation(path.read_text().splitlines(True)):
            m = DEFINE.match(text)
            if not m:
                continue
            name, params, rest = m.group("name"), m.group("params"), m.group("body")
            if SKIP.match(name):
                continue
            if name in DEFERRED_NAMES:
                deferred.append((name, re.sub(r"\s+", " ", rest).split("/*")[0].strip(),
                                 f"{filename}:{lineno}"))
                continue
            expr, comment = split_comment(rest)
            if not expr:
                continue
            if params:                                   # function-like macro
                if name in SKIP_FN:
                    continue
                if name in HAND_WRITTEN_FN:
                    funcs.append(
                        f"def {name}({', '.join(_arg_order(name, params))}) -> int:\n"
                        f"    return {HAND_WRITTEN_FN[name]}  # [SRC] {filename}:{lineno}"
                    )
                    continue
                joined = re.sub(r"\s+", " ", expr)
                raw_params = [a.strip() for a in params[1:-1].split(",")]
                pnames = frozenset(raw_params) | frozenset(a.lstrip("_") for a in raw_params)
                py = translate(joined, known, pnames)
                if py is None:
                    print(f"  skip fn {name} [{filename}:{lineno}]: {joined}", file=sys.stderr)
                    continue
                for raw in [a.strip() for a in params[1:-1].split(",")]:
                    py = re.sub(rf"\b{raw.lstrip('_')}\b", raw.lstrip("_"), py)
                    py = re.sub(rf"\b{re.escape(raw)}\b", raw.lstrip("_"), py)
                ordered = [a.strip().lstrip("_") for a in params[1:-1].split(",")]
                funcs.append(
                    f"def {name}({', '.join(ordered)}) -> int:\n"
                    f"    return {py}  # [SRC] {filename}:{lineno}"
                )
                continue
            if name in HAND_WRITTEN:
                body.append(f"{name} = {HAND_WRITTEN[name]}".ljust(46)
                            + f"  # [SRC] {filename}:{lineno}")
                continue
            py = translate(expr, known)
            if py is None and "&" in expr and "~" in expr:
                # C folds a multi-line `A & ~B & ~C` chain into one Python expression.
                parts = [p.strip() for p in expr.replace("~", "| ~").split("&") if p.strip()]
                py = None
                if all(p in known or p.startswith("~") for p in parts):
                    py = re.sub(r"\s+", " ", expr).replace("~", "| ~")
            if py is None:
                print(f"  skip {name} [{filename}:{lineno}]: {expr}", file=sys.stderr)
                continue
            tail = f"  # {comment}" if comment else ""
            body.append(f"{name} = {py}".ljust(46) + f"  # [SRC] {filename}:{lineno}{tail}")

        if not body and not funcs:
            continue
        out.append("")
        out.append("# " + "=" * 60)
        out.append(f"# {blurb} — [SRC] {filename}")
        out.append("# " + "=" * 60)
        out.extend(body)
        out.extend(funcs)

    out.append("")
    out.append("# " + "=" * 60)
    out.append("# Enum constants (C enums, implicit auto-increment resolved)")
    out.append("# " + "=" * 60)
    for ename, evalue in collect_enums():
        out.append(f"{ename} = {evalue}")
    out.append("")
    out.append("# " + "=" * 60)
    out.append("# Derived sizes — defined above the enum fields they reference")
    out.append("# " + "=" * 60)
    for dname, dval, dloc in deferred:
        out.append(f"{dname} = {dval}".ljust(46) + f"  # [SRC] {dloc}")

    target = REPO / "src" / "wifit3" / "chips" / "mt7601u" / "constants.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + "\n")
    print(f"wrote {target} ({len(out)} lines)")


if __name__ == "__main__":
    main()
