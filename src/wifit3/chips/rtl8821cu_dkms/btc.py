"""RTL8821CU BT-coexistence power-on setting.

`rtw_hal_power_on` calls this at its tail whenever the card reports BT present
(`EEPROMBluetoothCoexist`), which a combo-silicon 8821CU does even when used WiFi-only.
For a 1-antenna combo card it parks the antenna path to the BT side (BB-SW DPDT control)
and writes the WiFi-only coexistence tables, so the antenna/PA/LNA front end is in a known
state before firmware loads. Only the register writes are ported — the runtime BT-coex
decision machine is out of scope for a WiFi-only auditing tool.

The btc layer addresses registers as raw hex (no symbolic names in the vendor source), so
the magic addresses below carry their source citation instead of a shared constant.

Ported from:
  [SRC] hal/btc/halbtc8821c1ant.c:3838  ex_halbtc8821c1ant_power_on_setting
  [SRC] hal/btc/halbtc8821c1ant.c:2546  halbtc8821c1ant_set_ant_path (POWERON phase)
  [SRC] hal/btc/halbtc8821c1ant.c:2340  halbtc8821c1ant_set_ant_switch
  [SRC] hal/btc/halbtc8821c1ant.c:2474  halbtc8821c1ant_set_rfe_type
  [SRC] hal/btc/halbtc8821c1ant.c:1596/1648/1674  coex_ctrl_owner / set_table / table
  [SRC] hal/hal_btcoex.c (halbtcoutsrc_Write1ByteBitMask — mask 0xFF writes whole byte)
"""
from __future__ import annotations

from dataclasses import dataclass

REG_SYS_FUNC_EN = 0x0002        # btc_write_2byte(0x2, |BIT0|BIT1) — BB reset release
REG_PATH_OWNER = 0x0073         # 0x70[26] path-control owner (BBSW vs PTA) [SRC] :1604
REG_ANT_SW_CTRL_LO = 0x004E     # 0x4c[23] BB-SW select  [SRC] :2398
REG_ANT_SW_CTRL_HI = 0x004F     # 0x4c[24] DPDT enable    [SRC] :2400
REG_DPDT_CTRL = 0x00CB4         # DPDT/SPDT RFE-ctrl8/9 sel [SRC] :2402
REG_DPDT_POL = 0x00CB7          # 0xcb4[29:28] DPDT_SEL polarity [SRC] :2407
REG_RFE_PAPE_LNA = 0x0067       # 0x64[29] PAPE / 0x64[28] LNA_ON [SRC] :2462-2469
REG_BT_COEX_TABLE = 0x06C0      # [SRC] halmac_reg2.h REG_BT_COEX_TABLE
REG_BT_COEX_TABLE2 = 0x06C4
REG_BT_COEX_BREAK_TABLE = 0x06C8
REG_BT_COEX_TABLE_H = 0x06CC
REG_USB_ANT_PATH_FW = 0xFE08    # USB local reg: single-ant pos for FW [SRC] :3895


@dataclass
class RfeType:
    """`halbtc8821c1ant_set_rfe_type` decode of `board_info->rfe_type & 0x1F`
    [SRC] halbtc8821c1ant.c:2474-2542."""
    ext_ant_switch_exist: bool
    ant_at_main_port: bool
    wlg_locate_at_btg: bool
    ext_ant_switch_ctrl_polarity: int = 0     # always 0 in set_rfe_type (:2482)


def _decode_rfe(rfe_type: int) -> RfeType:
    m = rfe_type & 0x1F
    # main-port + switch-exist by module type; defaults match the switch's `default` arm.
    no_switch = m in (5, 6, 13, 14)
    aux_port = m in (3, 11, 4, 12)
    wlg_at_btg = m in (2, 10, 4, 12, 7, 15)
    return RfeType(ext_ant_switch_exist=not no_switch,
                   ant_at_main_port=not aux_port, wlg_locate_at_btg=wlg_at_btg)


def _write_bitmask8(t, addr: int, mask: int, val: int) -> None:
    """halbtcoutsrc_Write1ByteBitMask: mask 0xFF writes the whole byte (no read);
    otherwise read-modify-write with `val` shifted into the mask's low bit."""
    if mask == 0xFF:
        t.write8(addr, val & 0xFF)
        return
    shift = (mask & -mask).bit_length() - 1
    cur = t.read8(addr)
    t.write8(addr, (cur & ~mask) | ((val << shift) & mask))


def _set_ant_switch_to_bt(t, rfe: RfeType) -> None:
    """`set_ant_switch(BBSW, TO_BT)` — the POWERON ctrl/pos. [SRC] :2394-2470.
    ant_div_cfg is FALSE on a combo 1-ant card, so ctrl stays BBSW (not ANTDIV)."""
    if not rfe.ext_ant_switch_exist:
        return
    polarity_inverse = bool(rfe.ext_ant_switch_ctrl_polarity)
    if not rfe.ant_at_main_port:
        polarity_inverse = not polarity_inverse
    # pos_type == TO_BT: the pos switch leaves polarity unchanged (:2377-2388).
    _write_bitmask8(t, REG_ANT_SW_CTRL_LO, 0x80, 0x0)
    _write_bitmask8(t, REG_ANT_SW_CTRL_HI, 0x01, 0x1)
    _write_bitmask8(t, REG_DPDT_CTRL, 0xFF, 0x77)
    _write_bitmask8(t, REG_DPDT_POL, 0x30, 0x1 if not polarity_inverse else 0x2)
    # PAPE/LNA_ON controlled by WL (not BT) while not BY_BT ctrl (:2465-2469).
    _write_bitmask8(t, REG_RFE_PAPE_LNA, 0x20, 0x1)
    _write_bitmask8(t, REG_RFE_PAPE_LNA, 0x10, 0x1)


def _coex_table0(t) -> None:
    """`table(FC_EXCU, 0)` -> `set_table` direct writes (force_exec skips the read).
    Type 0 + non-concurrent-rx: 0x55555555 / 0x55555555 / 0xffffff / 0x13. [SRC] :1664-1667,1688-1701."""
    t.write32(REG_BT_COEX_TABLE, 0x55555555)
    t.write32(REG_BT_COEX_TABLE2, 0x55555555)
    t.write32(REG_BT_COEX_BREAK_TABLE, 0x00FFFFFF)
    t.write8(REG_BT_COEX_TABLE_H, 0x13)


def power_on_setting(t, rfe_type: int, single_ant_path: int) -> None:
    """[SRC] ex_halbtc8821c1ant_power_on_setting halbtc8821c1ant.c:3838 (1-antenna).

    `set_rfe_type` is pure logic; `set_ant_path(POWERON)` sets path owner to BT then routes
    the BB-SW antenna switch to BT; `table(0)` lays the WiFi-only coex tables; the single-ant
    position is parked in a USB local reg for the not-yet-loaded FW. (2-antenna and the GNT
    debug-to-GPIO path are not on this card's graph.)
    """
    t.write16(REG_SYS_FUNC_EN, t.read16(REG_SYS_FUNC_EN) | (1 << 0) | (1 << 1))
    rfe = _decode_rfe(rfe_type)
    _write_bitmask8(t, REG_PATH_OWNER, 1 << 2, 0)       # coex_ctrl_owner(BTSIDE)
    _set_ant_switch_to_bt(t, rfe)
    _coex_table0(t)
    # single_ant_path: AUX(0) -> 1, MAIN(1) -> 0  [SRC] :3867-3873
    t.write8(REG_USB_ANT_PATH_FW, 0 if single_ant_path == 1 else 1)
