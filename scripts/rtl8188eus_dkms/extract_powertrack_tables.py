"""Extract the RTL8188E thermal power-tracking swing tables from PHYDM source.

The 8188e TX-power-tracking thermal loop selects BB swing words from a set of
fixed tables (``ofdm_swing_table_new``, the per-band CCK swing matrices) and
indexes them with the 2.4 GHz delta-swing-index arrays. This parses each array
verbatim from the vendor source into a Python module the port consumes 1:1:

  - OFDM_SWING_TABLE            ofdm_swing_table_new[]            (43 u32)
  - CCK_SWING_TABLE_CH1_CH13    cck_swing_table_ch1_ch13_new[][8] (33 x 8 u8)
  - CCK_SWING_TABLE_CH14        cck_swing_table_ch14_new[][8]     (33 x 8 u8)
  - DELTA_SWING_IDX_2GA_P       delta_swing_table_idx_2ga_p_8188e[] (u8)
  - DELTA_SWING_IDX_2GA_N       delta_swing_table_idx_2ga_n_8188e[] (u8)

The runtime ch1-13 thermal loop instead uses the HWImg TxPowerTrack_USB
delta-swing-index tables (the 8 ``g_delta_swing_table_idx_mp_2g*_..._usb_8188e``
1-D u8[30] arrays); the ``DELTA_SWING_IDX_2GA_*`` above are the static fallback
for out-of-band channels. The USB tables are parsed into:

  - DELTA_TT_2GA_P / _2GA_N / _2GB_P / _2GB_N            (OFDM, per RF path)
  - DELTA_TT_2G_CCK_A_P / _A_N / _B_P / _B_N             (CCK, per RF path)

    uv run python scripts/rtl8188eus_dkms/extract_powertrack_tables.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HALRF = (REPO / "usb_dumps_new" / "captures_8188eu" / "driver-source"
         / "hal" / "phydm" / "halrf")
SRC_C = HALRF / "halrf_powertracking_ce.c"
SRC_H = HALRF / "halrf_powertracking_ce.h"
HWIMG = (REPO / "usb_dumps_new" / "captures_8188eu" / "driver-source"
         / "hal" / "phydm" / "rtl8188e" / "halhwimg8188e_rf.c")
OUT = (REPO / "src" / "wifit3" / "chips" / "rtl8188eus_dkms" / "powertrack_tbl.py")

# The 8 HWImg TxPowerTrack_USB 1-D u8[30] delta-swing tables, in
# output-symbol -> C-array-name order. These are the runtime ch1-13 tables.
DELTA_TT_TABLES = [
    ("DELTA_TT_2GA_P", "g_delta_swing_table_idx_mp_2ga_p_txpowertrack_usb_8188e"),
    ("DELTA_TT_2GA_N", "g_delta_swing_table_idx_mp_2ga_n_txpowertrack_usb_8188e"),
    ("DELTA_TT_2GB_P", "g_delta_swing_table_idx_mp_2gb_p_txpowertrack_usb_8188e"),
    ("DELTA_TT_2GB_N", "g_delta_swing_table_idx_mp_2gb_n_txpowertrack_usb_8188e"),
    ("DELTA_TT_2G_CCK_A_P",
     "g_delta_swing_table_idx_mp_2g_cck_a_p_txpowertrack_usb_8188e"),
    ("DELTA_TT_2G_CCK_A_N",
     "g_delta_swing_table_idx_mp_2g_cck_a_n_txpowertrack_usb_8188e"),
    ("DELTA_TT_2G_CCK_B_P",
     "g_delta_swing_table_idx_mp_2g_cck_b_p_txpowertrack_usb_8188e"),
    ("DELTA_TT_2G_CCK_B_N",
     "g_delta_swing_table_idx_mp_2g_cck_b_n_txpowertrack_usb_8188e"),
]
DELTA_SWINGIDX_SIZE = 30

# An int token: 0x.. hex or plain decimal. Used after slicing out the array body,
# so it never matches a C type/size identifier.
_INT = re.compile(r"0[xX][0-9A-Fa-f]+|\d+")


def _array_body(text: str, name: str) -> str:
    """Return the text between the first ``{`` after ``name[...] = `` and its
    matching closing ``};`` (the array initializer body), trailing-comment-free
    enough for token parsing. Targets the *definition* (``... = {``), so it skips
    bare struct-field declarations (``u8 name[SIZE];``) sharing the identifier."""
    m = re.search(rf"\b{re.escape(name)}\s*\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)?=\s*{{",
                  text)
    if m is None:
        raise ValueError(f"{name}: definition not found")
    start = m.end() - 1  # position of the opening '{'
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise ValueError(f"{name}: unterminated initializer")


def _strip_comments(body: str) -> str:
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    body = re.sub(r"//[^\n]*", "", body)
    return body


def extract_1d(text: str, name: str) -> list[int]:
    body = _strip_comments(_array_body(text, name))
    return [int(t, 0) for t in _INT.findall(body)]


def extract_2d(text: str, name: str, cols: int) -> list[list[int]]:
    body = _strip_comments(_array_body(text, name))
    rows: list[list[int]] = []
    for blk in re.findall(r"\{([^{}]*)\}", body):
        vals = [int(t, 0) for t in _INT.findall(blk)]
        if vals:
            rows.append(vals)
    for r, row in enumerate(rows):
        if len(row) != cols:
            raise ValueError(f"{name} row {r}: {len(row)} cols, expected {cols}")
    return rows


def _fmt_1d_hex(values: list[int], width: int) -> str:
    rows = [f"    {', '.join(f'0x{v:0{width}X}' for v in values[i:i + 6])},"
            for i in range(0, len(values), 6)]
    return "\n".join(rows)


def _fmt_1d_dec(values: list[int]) -> str:
    rows = [f"    {', '.join(str(v) for v in values[i:i + 10])},"
            for i in range(0, len(values), 10)]
    return "\n".join(rows)


def _fmt_2d_hex(rows: list[list[int]]) -> str:
    return "\n".join(
        f"    [{', '.join(f'0x{v:02X}' for v in row)}]," for row in rows
    )


def main() -> int:
    c_text = SRC_C.read_text(errors="replace")
    h_text = SRC_H.read_text(errors="replace")
    hwimg_text = HWIMG.read_text(errors="replace")

    ofdm = extract_1d(c_text, "ofdm_swing_table_new")
    cck13 = extract_2d(c_text, "cck_swing_table_ch1_ch13_new", 8)
    cck14 = extract_2d(c_text, "cck_swing_table_ch14_new", 8)
    delta_p = extract_1d(h_text, "delta_swing_table_idx_2ga_p_8188e")
    delta_n = extract_1d(h_text, "delta_swing_table_idx_2ga_n_8188e")

    delta_tt: list[tuple[str, list[int]]] = []
    for sym, c_name in DELTA_TT_TABLES:
        vals = extract_1d(hwimg_text, c_name)
        if len(vals) != DELTA_SWINGIDX_SIZE:
            raise ValueError(
                f"{c_name}: {len(vals)} entries, expected {DELTA_SWINGIDX_SIZE}")
        delta_tt.append((sym, vals))

    doc = (
        '"""RTL8188E thermal power-tracking swing tables — extracted verbatim '
        "from the vendor source.\n\n"
        "OFDM_SWING_TABLE         = ofdm_swing_table_new[] "
        f"[SRC] hal/phydm/halrf/halrf_powertracking_ce.c ({len(ofdm)} u32).\n"
        "CCK_SWING_TABLE_CH1_CH13 = cck_swing_table_ch1_ch13_new[][8] "
        f"[SRC] same .c ({len(cck13)}x8 u8).\n"
        "CCK_SWING_TABLE_CH14     = cck_swing_table_ch14_new[][8] "
        f"[SRC] same .c ({len(cck14)}x8 u8).\n"
        "DELTA_SWING_IDX_2GA_P    = delta_swing_table_idx_2ga_p_8188e[] "
        f"[SRC] hal/phydm/halrf/halrf_powertracking_ce.h ({len(delta_p)} u8).\n"
        "DELTA_SWING_IDX_2GA_N    = delta_swing_table_idx_2ga_n_8188e[] "
        f"[SRC] same .h ({len(delta_n)} u8).\n"
        "DELTA_TT_*               = g_delta_swing_table_idx_mp_2g*_"
        "txpowertrack_usb_8188e[] "
        f"[SRC] hal/phydm/rtl8188e/halhwimg8188e_rf.c (8 x {DELTA_SWINGIDX_SIZE}"
        " u8) — the runtime ch1-13 delta tables.\n"
        "Generated by scripts/rtl8188eus_dkms/extract_powertrack_tables.py "
        '— do not hand-edit.\n"""\n\n'
    )

    out = (
        doc
        + f"OFDM_SWING_TABLE = [\n{_fmt_1d_hex(ofdm, 8)}\n]\n\n"
        + f"CCK_SWING_TABLE_CH1_CH13 = [\n{_fmt_2d_hex(cck13)}\n]\n\n"
        + f"CCK_SWING_TABLE_CH14 = [\n{_fmt_2d_hex(cck14)}\n]\n\n"
        + f"DELTA_SWING_IDX_2GA_P = [\n{_fmt_1d_dec(delta_p)}\n]\n\n"
        + f"DELTA_SWING_IDX_2GA_N = [\n{_fmt_1d_dec(delta_n)}\n]\n\n"
        + "# HWImg TxPowerTrack_USB tables — the runtime ch1-13 delta tables.\n"
        + "# [SRC] halhwimg8188e_rf.c\n"
        + "".join(
            f"{sym} = [\n{_fmt_1d_dec(vals)}\n]\n\n" for sym, vals in delta_tt
        ).rstrip()
        + "\n"
    )
    OUT.write_text(out, encoding="utf-8")

    print(f"wrote {OUT.name}:")
    print(f"  OFDM_SWING_TABLE         = {len(ofdm)} u32")
    print(f"  CCK_SWING_TABLE_CH1_CH13 = {len(cck13)}x8 u8")
    print(f"  CCK_SWING_TABLE_CH14     = {len(cck14)}x8 u8")
    print(f"  DELTA_SWING_IDX_2GA_P    = {len(delta_p)} u8")
    print(f"  DELTA_SWING_IDX_2GA_N    = {len(delta_n)} u8")
    for sym, vals in delta_tt:
        print(f"  {sym:<24} = {len(vals)} u8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
