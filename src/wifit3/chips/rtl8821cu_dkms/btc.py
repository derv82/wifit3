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

from . import firmware
from .rf import write_rf_masked

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


# ======================================================================
# BT-coex HAL init (rtw_btcoex_HAL_Initialize -> 1-ant init_hw_config)
# ======================================================================
# `rtl8821c_hal_init` runs this after `phy_bf_init` on a combo card
# (`EEPROMBluetoothCoexist`). The chain is
# `rtw_btcoex_HAL_Initialize(_FALSE)` -> `hal_btcoex_InitHwConfig` ->
# `halbtc8821c1ant_init_hw_config(back_up=TRUE, wifi_only=FALSE)`. The companion
# `EXhalbtcoutsrc_init_coex_dm` -> `halbtc8821c1ant_init_coex_dm` is an empty
# function, so all the wire traffic in this phase lives in init_hw_config.
# [SRC] hal/rtl8821c/rtl8821c_halinit.c:285 ; hal/btc/halbtc8821c1ant.c:3739.
#
# btc register access maps to the transport directly: btc_read/write_Nbyte ->
# rtw_read/write (transport read/write + the 0x4E0 ON-section mirror),
# btc_set_rf_reg -> phy_set_rf_reg (rf.write_rf_masked), btc_write_1byte_bitmask
# -> the _write_bitmask8 above. [SRC] hal/hal_btcoex.c:2112/2467.

REG_BT_SCOREBOARD_W = 0x00AA    # BT scoreboard mirror (write) [SRC] halbtc8821c1ant.c:478
REG_LTECOEX_CTRL = 0x1700       # LTE-coex indirect-access control [SRC] :1523
REG_LTECOEX_WDATA = 0x1704      # indirect write data [SRC] :1542
REG_LTECOEX_RDATA = 0x1708      # indirect read data [SRC] :1525
REG_LTECOEX_READY = 0x1703      # indirect ready flag, BIT5 [SRC] :1503
REG_BT_CAL_CHK = 0x049C         # 0x49c[1] = BT is calibrating [SRC] :2607
REG_COEX_CTRL_OWNER = 0x0073    # 0x70[26] path-control owner (BBSW vs PTA) [SRC] :1604
REG_COEX_TABLE0 = 0x06C0        # PTA coex table [SRC] :1664
REG_COEX_TABLE1 = 0x06C4
REG_COEX_BREAK_TABLE = 0x06C8
REG_COEX_TABLE_TYPE = 0x06CC

_LTECOEX_GNT_BT_HI = 0xC000     # 0x38 GNT_BT sw-control fields [SRC] set_gnt_bt :1616
_LTECOEX_GNT_BT_LO = 0x0C00
_LTECOEX_GNT_WL_HI = 0x3000     # 0x38 GNT_WL sw-control fields [SRC] set_gnt_wl :1636
_LTECOEX_GNT_WL_LO = 0x0300
_GNT_SW_LOW, _GNT_SW_HIGH, _GNT_HW_PTA = 0x1, 0x3, 0x0   # [SRC] :1611-1623

_SCBD_ACTIVE, _SCBD_ONOFF, _SCBD_TDMA = 1 << 0, 1 << 1, 1 << 9   # [SRC] halbtc8821c1ant.h:162
_SCBD_INIT = 0x8002             # write_scbd static originalval seed [SRC] :467

# set_ant_switch control/position selectors [SRC] halbtc8821c1ant.h
_CTRL_BY_BBSW, _CTRL_BY_PTA, _CTRL_BY_ANTDIV = 0, 1, 2
_CTRL_BY_MAC, _CTRL_BY_FW, _CTRL_BY_BT = 3, 4, 5
_TO_WLG, _TO_NOCARE = 1, 4      # TO_WLG is the only pos that flips polarity (when wlg not at btg)

_SCBD_SCAN, _SCBD_BTCQDDR = 1 << 2, 1 << 10     # [SRC] halbtc8821c1ant.h:164/168
_PHASE_INIT, _PHASE_2G, _PHASE_5G = 0x0, 0x3, 0x4   # [SRC] halbtc8821c1ant.h:149/152/153
_ANT_PATH_WIFI, _ANT_PATH_PTA, _ANT_PATH_AUTO = 0, 2, 4   # [SRC] halbtcoutsrc.h:164-168
_TO_WLA = 0x2                                   # [SRC] halbtc8821c1ant.h:143
_RSN_2GSWITCHBAND, _RSN_2GMEDIA = 0x3, 0x9      # [SRC] halbtc8821c1ant.h:176/182
_RSN_5GSWITCHBAND = 0x4                         # [SRC] halbtc8821c1ant.h:177
_BAND_5G = 1                                    # chan._need_switch_band latches t.current_band
# the 2G reasons that set is_wifi_linkscan_process -> action_wifi_linkscan [SRC] :3510
_LINKSCAN_REASONS = frozenset({0x0, 0x3, 0x5, 0xC})   # 2G SCANSTART / SWITCHBAND / CONSTART / SPECIALPKT


@dataclass
class BtcState:
    """The persistent btc `coex_sta`/`coex_dm` state — the `GLBtCoexist` analog. `init_hw_config`
    sets it up and the channel tune's `run_coex` reads/mutates it, so bring-up keeps one instance
    for the session (stored on the transport as ``t.btc``). The scoreboard mirror tracks its own
    last-written value (write_scbd reads back the driver copy, not the register)."""
    rfe: RfeType
    concurrent_rx_mode_on: bool = False
    scbd_val: int = _SCBD_INIT
    scbd_prev: int = 0
    # run_coex run-time flags (channel-tune path); seeded by set_ant_path(INIT) -> run_time FALSE
    run_time_state: bool = False
    cur_ant_pos_type: int = -1          # (ant_pos_type<<8)|phase, set_ant_path no-change guard
    coex_run_reason: int = -1
    wl_tx_limit_en: bool = False
    wl_ampdu_limit_en: bool = False
    cur_low_penalty_ra: bool = False
    cur_low_penalty_thres: int = 0
    wl_rxagg_limit_en: bool = False
    wl_rxagg_size: int = 0
    wl_0x430: int = 0
    wl_0x434: int = 0
    wl_0x42a: int = 0
    wl_0x455: int = 0
    wl_slot_toggle_change: bool = False
    tdma_timer_base: int = 0
    cur_ps_tdma: int = -1
    cur_ps_tdma_on: bool = False


def _wait_indirect_ready(t) -> None:
    """halbtc8821c1ant_wait_indirect_reg_ready [SRC] :1497 — poll 0x1703[5] before any 0x1700
    access. The chip is ready in the cold-boot wire, so this reads once (the 10-try give-up arm
    only fires on a stuck LTE-coex block)."""
    for _ in range(10):
        if t.read8(REG_LTECOEX_READY) & (1 << 5):
            break


def _read_indirect(t, reg_addr: int) -> int:
    """halbtc8821c1ant_read_indirect_reg [SRC] :1516 — read a 0x1700-space (LTE-coex) register:
    wait ready, latch the address (0x800F0000|addr), read the data port."""
    _wait_indirect_ready(t)
    t.write32(REG_LTECOEX_CTRL, 0x800F0000 | reg_addr)
    return t.read32(REG_LTECOEX_RDATA)


def _write_indirect(t, reg_addr: int, bit_mask: int, reg_value: int) -> None:
    """halbtc8821c1ant_write_indirect_reg [SRC] :1529 — a full-mask write skips the read-back;
    a partial mask read-modify-writes (the field is shifted to the mask's lowest bit). The
    write latches via 0x1704 (data) then 0x1700 (0xC00F0000|addr)."""
    if bit_mask == 0:
        return
    if bit_mask == 0xFFFFFFFF:
        _wait_indirect_ready(t)
        t.write32(REG_LTECOEX_WDATA, reg_value & 0xFFFFFFFF)
        t.write32(REG_LTECOEX_CTRL, 0xC00F0000 | reg_addr)
        return
    bitpos = (bit_mask & -bit_mask).bit_length() - 1
    val = _read_indirect(t, reg_addr)
    val = (val & ~bit_mask) | (reg_value << bitpos)
    _wait_indirect_ready(t)
    t.write32(REG_LTECOEX_WDATA, val & 0xFFFFFFFF)
    t.write32(REG_LTECOEX_CTRL, 0xC00F0000 | reg_addr)


def _ltecoex_enable(t, enable: bool) -> None:
    """halbtc8821c1ant_ltecoex_enable [SRC] :1567 — LTE-coex on/off via 0x38[7]."""
    _write_indirect(t, 0x38, 1 << 7, 1 if enable else 0)


def _set_gnt_bt(t, val: int) -> None:
    """halbtc8821c1ant_set_gnt_bt [SRC] :1607 — drive both GNT_BT sw-control nibbles of 0x38."""
    _write_indirect(t, 0x38, _LTECOEX_GNT_BT_HI, val)
    _write_indirect(t, 0x38, _LTECOEX_GNT_BT_LO, val)


def _set_gnt_wl(t, val: int) -> None:
    """halbtc8821c1ant_set_gnt_wl [SRC] :1627 — drive both GNT_WL sw-control nibbles of 0x38."""
    _write_indirect(t, 0x38, _LTECOEX_GNT_WL_HI, val)
    _write_indirect(t, 0x38, _LTECOEX_GNT_WL_LO, val)


def _set_ant_switch(t, rfe: RfeType, ctrl_type: int, pos_type: int) -> None:
    """halbtc8821c1ant_set_ant_switch (force_exec) [SRC] :2340 — route the external antenna
    switch. Polarity flips for an Aux-port 1-Ant or a non-BTG WLG position. Only BBSW/TO_BT is
    exercised at init (AUTO->BT); the other ctrl arms are the runtime channel-tune positions."""
    if not rfe.ext_ant_switch_exist:
        return
    polarity_inverse = bool(rfe.ext_ant_switch_ctrl_polarity)
    if not rfe.ant_at_main_port:
        polarity_inverse = not polarity_inverse
    if pos_type == _TO_WLG and not rfe.wlg_locate_at_btg:
        polarity_inverse = not polarity_inverse
    if ctrl_type == _CTRL_BY_BBSW:
        _write_bitmask8(t, REG_ANT_SW_CTRL_LO, 0x80, 0x0)
        _write_bitmask8(t, REG_ANT_SW_CTRL_HI, 0x01, 0x1)
        _write_bitmask8(t, REG_DPDT_CTRL, 0xFF, 0x77)
        _write_bitmask8(t, REG_DPDT_POL, 0x30, 0x1 if not polarity_inverse else 0x2)
    elif ctrl_type == _CTRL_BY_PTA:
        _write_bitmask8(t, REG_ANT_SW_CTRL_LO, 0x80, 0x0)
        _write_bitmask8(t, REG_ANT_SW_CTRL_HI, 0x01, 0x1)
        _write_bitmask8(t, REG_DPDT_CTRL, 0xFF, 0x66)
        _write_bitmask8(t, REG_DPDT_POL, 0x30, 0x2 if not polarity_inverse else 0x1)
    elif ctrl_type == _CTRL_BY_ANTDIV:
        _write_bitmask8(t, REG_ANT_SW_CTRL_LO, 0x80, 0x0)
        _write_bitmask8(t, REG_ANT_SW_CTRL_HI, 0x01, 0x1)
        _write_bitmask8(t, REG_DPDT_CTRL, 0xFF, 0x88)
    elif ctrl_type == _CTRL_BY_MAC:
        _write_bitmask8(t, REG_ANT_SW_CTRL_LO, 0x80, 0x1)
        _write_bitmask8(t, 0x64, 0x1, 0x0 if not polarity_inverse else 0x1)
    elif ctrl_type == _CTRL_BY_FW:
        _write_bitmask8(t, REG_ANT_SW_CTRL_LO, 0x80, 0x0)
        _write_bitmask8(t, REG_ANT_SW_CTRL_HI, 0x01, 0x1)
    elif ctrl_type == _CTRL_BY_BT:
        _write_bitmask8(t, REG_ANT_SW_CTRL_LO, 0x80, 0x0)
        _write_bitmask8(t, REG_ANT_SW_CTRL_HI, 0x01, 0x0)
    # PAPE/LNA_ON pinned to BT only while WLAN is off (leakage); else WL-controlled.
    pape_lna = 0x0 if ctrl_type == _CTRL_BY_BT else 0x1
    _write_bitmask8(t, REG_RFE_PAPE_LNA, 0x20, pape_lna)
    _write_bitmask8(t, REG_RFE_PAPE_LNA, 0x10, pape_lna)


def _set_ant_path_init(t, st: BtcState) -> None:
    """halbtc8821c1ant_set_ant_path(AUTO, FC_EXCU, PHASE_INIT) [SRC] :2585. Disable WiFi-side
    LTE-coex and pin GNT_WL/BT_LTE high (no LTE on this card), wait out any in-progress BT
    calibration (path owner is BT during BT IQK), then take path control to WL with GNT_BT
    forced high / GNT_WL low, and route the antenna switch to BT (AUTO resolves to BT)."""
    _ltecoex_enable(t, False)
    _write_indirect(t, 0xA0, 0xFFFF, 0xFFFF)        # ltecoex_table(WL_VS_LTE) [SRC] :2594
    _write_indirect(t, 0xA4, 0xFFFF, 0xFFFF)        # ltecoex_table(BT_VS_LTE) [SRC] :2600
    for _ in range(21):
        if not (t.read8(REG_BT_CAL_CHK) & (1 << 1)):
            break
    _write_bitmask8(t, REG_COEX_CTRL_OWNER, 1 << 2, 1)   # coex_ctrl_owner(WLSIDE) [SRC] :1604
    _set_gnt_bt(t, _GNT_SW_HIGH)
    _set_gnt_wl(t, _GNT_SW_LOW)
    _set_ant_switch(t, st.rfe, _CTRL_BY_BBSW, 0x0)       # AUTO->BT, pos TO_BT
    st.run_time_state = False                            # PHASE_INIT [SRC] :2632
    st.cur_ant_pos_type = (_ANT_PATH_AUTO << 8) | _PHASE_INIT


def _write_scbd(t, st: BtcState, bitpos: int, state: bool) -> None:
    """halbtc8821c1ant_write_scbd [SRC] :463 — set/clear bits of the BT scoreboard and write
    0xaa only when the (driver-tracked) value changes. The seed value is 0x8002."""
    if state:
        st.scbd_val |= bitpos
    else:
        st.scbd_val &= ~bitpos
    if st.scbd_val != st.scbd_prev:
        st.scbd_prev = st.scbd_val
        t.write16(REG_BT_SCOREBOARD_W, st.scbd_val & 0xFFFF)


# coex table (type -> (0x6c0, 0x6c4)); break/select come from concurrent_rx_mode_on. [SRC] :1697
_COEX_TABLE = {0: (0x55555555, 0x55555555), 4: (0x66555555, 0x5A5A5A5A)}      # [SRC] :1697/1718
# PS-TDMA-on cases (type -> the 5 set_tdma bytes) [SRC] halbtc8821c1ant.c:2156 — ported as reached.
_TDMA_ON_CASES = {21: (0x61, 0x30, 0x03, 0x11, 0x10)}


def _set_table(t, st: BtcState, v6c0: int, v6c4: int, v6c8: int, v6cc: int, force: bool) -> None:
    """halbtc8821c1ant_set_table [SRC] :1648 — a non-force call (and no wl-slot-toggle change)
    reads back 0x6c0/0x6c4 and returns when both match the wanted values; force writes all 4 rows
    unconditionally."""
    if not force and not st.wl_slot_toggle_change:
        cur0 = t.read32(REG_COEX_TABLE0)                 # both read-backs happen before the compare
        cur1 = t.read32(REG_COEX_TABLE1)                 # (the C reads into temps; no short-circuit)
        if cur0 == v6c0 and cur1 == v6c4:
            return
    t.write32(REG_COEX_TABLE0, v6c0)
    t.write32(REG_COEX_TABLE1, v6c4)
    t.write32(REG_COEX_BREAK_TABLE, v6c8)
    t.write8(REG_COEX_TABLE_TYPE, v6cc)


def _table(t, st: BtcState, type_: int, force: bool) -> None:
    """halbtc8821c1ant_table(type) [SRC] :1674. concurrent_rx_mode_on picks the WL-hi-pri break/
    select tables. Init lays type 0 (force); the not-connected action re-lays type 0 (non-force,
    which is a no-op read here since the table is unchanged). Other action-algorithm types aren't
    reached in the cold-boot window."""
    if st.concurrent_rx_mode_on:
        break_table, select_table = 0xF0FFFFFF, 0x1B
    else:
        break_table, select_table = 0x00FFFFFF, 0x13
    v6c0, v6c4 = _COEX_TABLE[type_]
    _set_table(t, st, v6c0, v6c4, break_table, select_table, force)


def _fill_h2c(t, cmd_id: int, pbuf: tuple[int, ...]) -> None:
    """btc_fill_h2c -> rtw_hal_fill_h2c_cmd -> rtl8821c_fillh2ccmd [SRC] rtl8821c_cmd.c:32 —
    prepend the command id to the params and send through the HMEBOX rotation."""
    firmware.send_h2c_by_reg(t, bytes((cmd_id, *pbuf)))


def _set_tdma_timer_base(t, st: BtcState, type_: int) -> None:
    """halbtc8821c1ant_set_tdma_timer_base [SRC] :384 — pick the TDMA slot timer base from the
    beacon period (4-slot 50ms when ``type_``==3). Returns without an H2C when the base is
    unchanged. The exercised tcases here carry no 4-slot bit (``type_``==0) and the base starts 0,
    so this is wire-silent; the other beacon-period arms are kept for the runtime tdma path."""
    tbtt = 100                                          # BTC_GET_U2_BEACON_PERIOD default
    if type_ == 3 and tbtt >= 100:
        if st.tdma_timer_base == 3:
            return
        para1 = ((tbtt // 50) - 1) | 0xC0
        st.tdma_timer_base = 3
    elif 0 < tbtt < 80:
        para1 = (100 // tbtt) + (1 if 100 % tbtt else 0)
        if st.tdma_timer_base == 2:
            return
        para1 &= 0x3F
        st.tdma_timer_base = 2
    elif tbtt >= 180:
        if st.tdma_timer_base == 1:
            return
        para1 = ((tbtt // 100) - (1 if tbtt % 100 <= 80 else 0)) & 0x3F | 0x80
        st.tdma_timer_base = 1
    else:
        if st.tdma_timer_base == 0:
            return
        para1, st.tdma_timer_base = 0x1, 0
    _fill_h2c(t, 0x69, (0xB, para1))


def _set_tdma(t, b1: int, b2: int, b3: int, b4: int, b5: int) -> None:
    """halbtc8821c1ant_set_tdma [SRC] :2010 — the PS-TDMA H2C 0x60 (5 bytes). The Force-LPS arm
    (byte1 BIT4 set, BIT5 clear) is not on the exercised off-cases (byte1 0x8/0x0), so
    power_save_state stays WIFI_NATIVE (wire-silent) and the H2C is always sent."""
    _fill_h2c(t, 0x60, (b1, b2, b3, b4, b5))


def _tdma(t, st: BtcState, force: bool, turn_on: bool, tcase: int) -> None:
    """halbtc8821c1ant_tdma [SRC] :2101 — set the PS-TDMA case. Init and the not-connected action
    both use (force, off, 8): TDMA-off type 8 is PTA control, so it clears the TDMA scoreboard bit
    (no-op when already clear) and sends set_tdma(0x8,0,0,0,0). ``turn_on`` (the PS-TDMA-on cases)
    isn't reached in the cold-boot/monitor window; only the off arm is ported."""
    _set_tdma_timer_base(t, st, 3 if (tcase & 0x100) else 0)
    type_ = tcase & 0xFF
    if not force and turn_on == st.cur_ps_tdma_on and type_ == st.cur_ps_tdma:
        return
    # wifi not busy in monitor -> TDMA scoreboard bit off.
    _write_scbd(t, st, _SCBD_TDMA, False)
    if turn_on:
        _write_bitmask8(t, 0x0550, 0x8, 0x1)            # enable TBTT interrupt [SRC] :2154
        _set_tdma(t, *_TDMA_ON_CASES[type_])            # PS-TDMA on (native-PS, byte1 BIT4 clear)
    else:
        _write_scbd(t, st, _SCBD_TDMA, False)
        if type_ == 8:
            _set_tdma(t, 0x8, 0x0, 0x0, 0x0, 0x0)       # PTA control
        elif type_ == 1:
            _set_tdma(t, 0x0, 0x0, 0x0, 0x48, 0x0)      # 2-ant antenna-diversity control
        else:
            _set_tdma(t, 0x0, 0x0, 0x0, 0x0, 0x0)       # software control, antenna at BT
    st.cur_ps_tdma_on, st.cur_ps_tdma = turn_on, type_


def _query_bt_info(t) -> None:
    """halbtc8821c1ant_query_bt_info [SRC] :494 — trigger a BT-info report (H2C 0x61, BIT0).
    bt_disabled is FALSE at init, so the H2C is sent."""
    _fill_h2c(t, 0x61, (0x1,))


def hal_init(t, info) -> None:
    """halbtc8821c1ant_init_hw_config(back_up=TRUE, wifi_only=FALSE) [SRC] :3739 — the BT-coex
    HAL init `rtl8821c_hal_init` runs after `phy_bf_init`. PTA/3-wire enable, take the antenna
    to BT, lay the WiFi-only coex table.

    `init_coex_var` and `enable_gnt_to_gpio` (dbg_mode off) are wire-silent here; the else arm
    runs because the RF is on and the card is not WiFi-only. The state persists on ``t.btc`` (the
    GLBtCoexist analog) for the channel tune's run_coex."""
    st = t.btc = BtcState(rfe=_decode_rfe(info.rfe_type))
    (t.read8(0x00F1) >> 4)                           # coex_sta->kt_ver [SRC] :3765
    _write_bitmask8(t, 0x0550, 0x8, 0x1)             # enable TBTT interrupt [SRC] :3768
    t.write8(0x0790, 0x5)                            # BT report packet sample rate [SRC] :3771
    t.write8(0x0778, 0x1)                            # 0x778=1 for 1-Ant [SRC] :3774
    _write_bitmask8(t, 0x0040, 0x20, 0x1)            # PTA 3-wire from BT [SRC] :3777
    _write_bitmask8(t, 0x0041, 0x02, 0x1)            # [SRC] :3778
    _write_bitmask8(t, 0x04C6, 0x30, 0x1)            # PTA tx/rx from WiFi [SRC] :3781
    _write_bitmask8(t, 0x0763, 0x10, 0x1)            # GNT_BT=1, coex table both [SRC] :3784
    _write_bitmask8(t, 0x06CF, 1 << 3, 0x1)          # beacon queue hi-pri [SRC] :3787
    st.concurrent_rx_mode_on = True
    write_rf_masked(t, 0x1, 0x2, 0x0)                # btc_set_rf_reg(RF_A,0x1,0x2,0x0) [SRC] :3811
    _set_ant_path_init(t, st)
    _write_scbd(t, st, _SCBD_ACTIVE | _SCBD_ONOFF, True)
    _table(t, st, 0, force=True)
    _tdma(t, st, force=True, turn_on=False, tcase=8)
    _query_bt_info(t)


# ======================================================================
# run_coex (the channel-tune coex path)
# ======================================================================
# The phydm band switch calls `rtw_btcoex_switchband_notify(under_scan=FALSE, band)`. For a 2.4G
# band with no scan that maps to `run_coex(RSN_2GSWITCHBAND)`. run_coex first runs
# `update_wifi_link_info` (which, for monitor mode / no link / BT-idle, applies the low-penalty-RA
# + tx/rx limits) then early-returns at `!run_time_state` (set FALSE by PHASE_INIT, not yet TRUE).
# So the only wire effect at the first band switch is `limited_tx`'s 4 backup reads.
# [SRC] halbtc8821c1ant.c:3493 run_coex / :1080 update_wifi_link_info / :171 limited_tx.

_REG_WL_0x430, _REG_WL_0x434, _REG_WL_0x42A, _REG_WL_0x455 = 0x0430, 0x0434, 0x042A, 0x0455


def _low_penalty_ra(t, st: BtcState, force: bool, low_penalty: bool, thres: int) -> None:
    """halbtc8821c1ant_low_penalty_ra [SRC] :1xxx — modify the RA PCR threshold via phydm; a
    no-change call (force off, same low_penalty + thres) returns without touching the chip. At
    monitor idle this is the no-change path (silent)."""
    if not force and low_penalty == st.cur_low_penalty_ra and thres == st.cur_low_penalty_thres:
        return
    # btc_phydm_modify_RA_PCR_threshold(0, thres or 0) — phydm H2C/reg; not exercised at idle.
    st.cur_low_penalty_ra = low_penalty
    st.cur_low_penalty_thres = thres


def _limited_tx(t, st: BtcState, force: bool, tx_limit_en: bool, ampdu_limit_en: bool) -> None:
    """halbtc8821c1ant_limited_tx [SRC] :171 — back up the WL tx-retry / AMPDU regs while the BT
    tx-limit is not engaged, then either return (no change) or apply / restore the limit. At
    monitor / no-link / BT-idle (tx_limit_en=ampdu_limit_en=FALSE, already FALSE) only the 4
    backup reads hit the wire."""
    if not st.wl_tx_limit_en:
        st.wl_0x430 = t.read32(_REG_WL_0x430)
        st.wl_0x434 = t.read32(_REG_WL_0x434)
        st.wl_0x42a = t.read16(_REG_WL_0x42A)
    if not st.wl_ampdu_limit_en:
        st.wl_0x455 = t.read8(_REG_WL_0x455)
    if not force and tx_limit_en == st.wl_tx_limit_en and ampdu_limit_en == st.wl_ampdu_limit_en:
        return
    st.wl_tx_limit_en, st.wl_ampdu_limit_en = tx_limit_en, ampdu_limit_en
    if tx_limit_en:
        _write_bitmask8(t, 0x045E, 0x8, 0x1)
        _write_bitmask8(t, 0x0426, 0xF, 0xF)
        t.write16(0x042A, 0x0808)
        t.write32(0x0430, 0x1000000)
        t.write32(0x0434, 0x4030201)            # wifi !b-mode (the captured cards are not 11b)
    else:
        _write_bitmask8(t, 0x045E, 0x8, 0x0)
        _write_bitmask8(t, 0x0426, 0xF, 0x0)
        t.write16(0x042A, st.wl_0x42a)
        t.write32(0x0430, st.wl_0x430)
        t.write32(0x0434, st.wl_0x434)
    t.write8(0x0455, 0x20 if ampdu_limit_en else st.wl_0x455)


def _limited_rx(t, st: BtcState, force: bool, bt_ctrl_agg: bool, agg_size: int) -> None:
    """halbtc8821c1ant_limited_rx [SRC] :limited_rx — updates RX-aggregation via btc_set (driver
    state / RX-thread), no direct register write on this path. Tracked for the no-change guard."""
    if not force and bt_ctrl_agg == st.wl_rxagg_limit_en and agg_size == st.wl_rxagg_size:
        return
    st.wl_rxagg_limit_en, st.wl_rxagg_size = bt_ctrl_agg, agg_size


def _update_wifi_link_info(t, st: BtcState) -> None:
    """halbtc8821c1ant_update_wifi_link_info [SRC] :1080 (monitor / num_of_wifi_link==0 /
    BT-NCON-IDLE branch): low-penalty-RA off, tx-limit off, rx-limit off. The btc_get classifiers
    are software; the only wire effect is `limited_tx`'s 4 backup reads."""
    _low_penalty_ra(t, st, force=False, low_penalty=False, thres=0)
    _limited_tx(t, st, force=False, tx_limit_en=False, ampdu_limit_en=False)
    _limited_rx(t, st, force=False, bt_ctrl_agg=True, agg_size=64)


def _set_ant_path_2g(t, st: BtcState, force: bool) -> None:
    """halbtc8821c1ant_set_ant_path(AUTO, force, PHASE_2G) [SRC] :2678 — route the shared antenna
    to WiFi at runtime. A non-force call returns early when the path is unchanged (run_coex
    re-asserts the path after the media-connect trigger already set it). Otherwise: wait out any
    in-progress WL/BT IQK (0x49c[0]/[1]), take path control to WL, drive both GNT to HW-PTA, arm
    run_time_state, then route the BB-SW switch to WiFi (AUTO -> WIFI for a wlg-at-btg card)."""
    key = (_ANT_PATH_AUTO << 8) | _PHASE_2G
    if not force and st.cur_ant_pos_type == key:
        return
    st.cur_ant_pos_type = key
    for _ in range(21):
        if not (t.read8(REG_BT_CAL_CHK) & ((1 << 0) | (1 << 1))):
            break
    _write_bitmask8(t, REG_COEX_CTRL_OWNER, 1 << 2, 1)   # coex_ctrl_owner(WLSIDE) [SRC] :2706
    _set_gnt_bt(t, _GNT_HW_PTA)
    _set_gnt_wl(t, _GNT_HW_PTA)
    st.run_time_state = True                             # PHASE_2G [SRC] :2713
    if st.rfe.wlg_locate_at_btg:                         # AUTO -> WIFI (BBSW/TO_WLG)
        _set_ant_switch(t, st.rfe, _CTRL_BY_BBSW, _TO_WLG)
    else:                                                # AUTO -> PTA (BBSW->PTA, TO_NOCARE)
        _set_ant_switch(t, st.rfe, _CTRL_BY_PTA, _TO_NOCARE)


def _action_wifi_not_connected(t, st: BtcState) -> None:
    """halbtc8821c1ant_action_wifi_not_connected [SRC] :3297 — the monitor / no-link coex action:
    coex table type 0 (non-force, a no-op read here) + PTA-control PS-TDMA off (type 8)."""
    _table(t, st, 0, force=False)
    _tdma(t, st, force=True, turn_on=False, tcase=8)


def _action_wifi_linkscan(t, st: BtcState) -> None:
    """halbtc8821c1ant_action_wifi_linkscan [SRC] :3280 — the scan / link / band-switch coex action
    (no BT link here -> the pan/a2dp-absent arm): coex table type 4 + PS-TDMA on type 21."""
    _table(t, st, 4, force=False)
    _tdma(t, st, force=False, turn_on=True, tcase=21)


def run_coex(t, reason: int) -> None:
    """halbtc8821c1ant_run_coex [SRC] :3493. `update_wifi_link_info` runs first (the `limited_tx`
    backup reads), then the run-time gate: at the first band switch `run_time_state` is FALSE
    (PHASE_INIT) so it returns there. Once a runtime phase armed it (media-connect's PHASE_2G),
    the single-port-2G path re-asserts the antenna (non-force -> no wire), sets the BTCQDDR
    scoreboard bit, and runs the not-connected action. The connected / link-scan / BT-active
    branches need link or BT state that monitor bring-up never has — ported when a pass reaches
    them."""
    st = t.btc
    st.coex_run_reason = reason
    _update_wifi_link_info(t, st)
    if not st.run_time_state:
        return
    if t.current_band == _BAND_5G:                       # is_all_under_5g [SRC] :3588
        _action_wifi_under5g(t, st)
        return
    _set_ant_path_2g(t, st, force=False)                 # single-port 2G, re-assert (no wire)
    _write_scbd(t, st, _SCBD_BTCQDDR, True)
    if reason in _LINKSCAN_REASONS:                      # is_wifi_linkscan_process [SRC] :3696
        _action_wifi_linkscan(t, st)
    else:
        _action_wifi_not_connected(t, st)


def _set_ant_path_5g(t, st: BtcState, force: bool) -> None:
    """halbtc8821c1ant_set_ant_path(AUTO, force, PHASE_5G) [SRC] :2722 — 5G is WiFi-exclusive, so
    (unlike PHASE_2G) there is no BT-IQK 0x49c poll and no ltecoex setup: take path control to WL,
    drive GNT_BT to HW-PTA and GNT_WL to SW-high, arm run_time, then route the BB-SW switch to 5G
    WiFi (AUTO -> WIFI5G -> BBSW/TO_WLA). A non-force call returns early when the path is unchanged."""
    key = (_ANT_PATH_AUTO << 8) | _PHASE_5G
    if not force and st.cur_ant_pos_type == key:
        return
    st.cur_ant_pos_type = key
    _write_bitmask8(t, REG_COEX_CTRL_OWNER, 1 << 2, 1)   # coex_ctrl_owner(WLSIDE) [SRC] :2725
    _set_gnt_bt(t, _GNT_HW_PTA)
    _set_gnt_wl(t, _GNT_SW_HIGH)
    st.run_time_state = True                             # PHASE_5G [SRC] :2733
    _set_ant_switch(t, st.rfe, _CTRL_BY_BBSW, _TO_WLA)


def _action_wifi_under5g(t, st: BtcState) -> None:
    """halbtc8821c1ant_action_wifi_under5g [SRC] :3257 — the WiFi-is-under-5G coex action:
    set_ant_path(PHASE_5G) + coex table type 0 + PTA-control PS-TDMA off (type 8). Both the table
    and the tdma are non-force here: the table read-compare matches (no write) and the tdma is a
    no-op (cur PS-TDMA is already off/type-8 from init), so neither emits an H2C."""
    _set_ant_path_5g(t, st, force=False)
    _table(t, st, 0, force=False)
    _tdma(t, st, force=False, turn_on=False, tcase=8)


def switchband_notify_2g(t) -> None:
    """rtw_btcoex_switchband_notify(under_scan=FALSE, BAND_ON_2_4G) [SRC] hal_btcoex.c -> the 1-ant
    `ex_halbtc8821c1ant_switchband_notify(BTC_SWITCH_TO_24G_NOFORSCAN)` -> `run_coex(2GSWITCHBAND)`.
    Returns immediately if stop_coex_dm (not the case here)."""
    run_coex(t, _RSN_2GSWITCHBAND)


def switchband_notify_5g(t) -> None:
    """rtw_btcoex_switchband_notify(BAND_ON_5G) [SRC] hal_btcoex.c -> the 1-ant
    `ex_halbtc8821c1ant_switchband_notify(BTC_SWITCH_TO_5G)` -> `run_coex(5GSWITCHBAND)` [SRC] :4761.
    With the channel now on 5G (`t.current_band`), run_coex takes the wifi-under-5G action."""
    run_coex(t, _RSN_5GSWITCHBAND)


def media_status_notify_connect_2g(t) -> None:
    """ex_halbtc8821c1ant_media_status_notify(BTC_MEDIA_CONNECT) [SRC] :4851 (2.4 GHz arm). The
    airmon setopmode(MONITOR) handler fires this 'connect' notify [SRC] core/rtw_mlme_ext.c:13575,
    after the monitor RX-filter. It is the antenna switch the cold HW test was missing: route the
    shared antenna from BT to WiFi (set_ant_path PHASE_2G, which arms run_time_state), set CCK
    Tx/Rx hi-priority (not 11b), send the leap-AP-protection H2C, then run_coex(2GMEDIA) lays the
    not-connected coex table + PTA tdma."""
    st = t.btc
    _write_scbd(t, st, _SCBD_ACTIVE | _SCBD_ONOFF, True)     # already set at init -> no wire
    _set_ant_path_2g(t, st, force=True)
    _write_bitmask8(t, 0x06CF, 1 << 4, 0x1)                 # CCK Tx/Rx hi-pri (not 11b) [SRC] :4897
    _fill_h2c(t, 0x69, (0xC, 0x0))                          # leap-AP protection reopen [SRC] :4900
    run_coex(t, _RSN_2GMEDIA)


# ======================================================================
# the BT-coex periodical (the 4th operational async producer)
# ======================================================================
# `hal_btcoex_Hanlder` [SRC] hal_btcoex.c:6069 runs every ~2 s from the driver's dynamic-check
# thread (alongside the phydm watchdog). It calls `EXhalbtcoutsrc_periodical` ->
# `ex_halbtc8821c1ant_periodical` [SRC] halbtc8821c1ant.c:5411, then a one-shot BT-FW-version
# query. Opener = the BT hi-pri TX/RX counter read 0x0770 (unique in the operational phase).

REG_BT_HI_PRI_TXRX = 0x0770     # monitor_bt_ctr hi-pri counter [SRC] :590
REG_BT_LO_PRI_TXRX = 0x0774     # monitor_bt_ctr lo-pri counter [SRC] :591
REG_BT_CNT_RST = 0x076E         # 0x76e=0xc resets the BT counters [SRC] :613
_RSN_PERIODICAL = 0xF           # [SRC] halbtc8821c1ant.h:188
_H2C_BT_MP_OPER = 0x67          # [SRC] include/hal_com_h2c.h:95 H2C_BT_MP_OPER
_BT_OP_GET_BT_VERSION = 0x00    # [SRC] hal_btcoex.c:165


@dataclass
class PeriodicalState:
    """ex_halbtc8821c1ant_periodical carried state — the bits the time-less replay can't re-derive
    from a chip read. `run_coex` re-runs only when a wifi/bt status change is detected
    (`moniter_wifibt_status`): in monitor-idle that is the monitor port-count going 0->1 on the
    first tick, then steady, so the run fires once. The BT-FW-version query also fires once — the
    real C2H reply caches `bt_get_fw_ver` non-zero, which gates out later ticks."""
    first_tick: bool = True
    fw_ver_queried: bool = False
    bt_mp_oper_seq: int = 0


def _monitor_bt_ctr(t) -> None:
    """halbtc8821c1ant_monitor_bt_ctr [SRC] :576 — sample the BT hi/lo-priority TX/RX counters and
    reset them. The `is_run_coex` return (a >50 counter delta while BT is NCON_IDLE) is FALSE in the
    capture (the counters read 0), so it never forces run_coex; the fold-in stats are software."""
    t.read32(REG_BT_HI_PRI_TXRX)
    t.read32(REG_BT_LO_PRI_TXRX)
    t.write8(REG_BT_CNT_RST, 0x0C)


def _monitor_wifi_ctr(t, st: BtcState) -> None:
    """halbtc8821c1ant_monitor_wifi_ctr [SRC] :669 — the CCK-lock / wl-noisy identification reads
    phydm's cached PHY counters (software, not the wire). The only register effect is the WL-FW-dbg
    H2C 0x69 {0x8}, gated on `cur_ps_tdma_on` (the fw-version outer gate is TRUE for this card's FW).
    PS-TDMA is off in monitor, so this stays silent until a PS-TDMA-on action sets the flag."""
    if st.cur_ps_tdma_on:
        _fill_h2c(t, 0x69, (0x8,))


def _read_scbd(t) -> int:
    """halbtc8821c1ant_read_scbd [SRC] :487 — read the 15-bit BT scoreboard mirror (0xaa). Called by
    monitor_bt_enable; the rest of that function (bt_disable_cnt bookkeeping) is software."""
    return t.read16(REG_BT_SCOREBOARD_W) & 0x7FFF


def _get_bt_patch_ver(t, st: PeriodicalState) -> None:
    """hal_btcoex_Hanlder's post-periodical BT-FW-version query [SRC] hal_btcoex.c:6075 ->
    halbtcoutsrc_GetBtPatchVer :859 -> _btmpoper_cmd(BT_OP_GET_BT_VERSION, 0, NULL, 0) :775. The
    H2C is BT_MP_OPER (0x67) carrying buf = {(seq<<4)|opcodever, opcode} = {0, 0} on the first send
    (seq starts 0), so the message-box word reads 0x00000067. The function then blocks on the C2H
    reply (interrupt-IN, off the gate's replay) — we send the H2C and return."""
    seq = st.bt_mp_oper_seq & 0xF
    st.bt_mp_oper_seq += 1
    _fill_h2c(t, _H2C_BT_MP_OPER, ((seq << 4), _BT_OP_GET_BT_VERSION))


def periodical(t, st: PeriodicalState) -> None:
    """One BT-coex periodical tick, in wire order [SRC] ex_halbtc8821c1ant_periodical :5411 wrapped
    by hal_btcoex_Hanlder :6069. `auto_report` is on so the leading query_bt_info is skipped; the
    downcounts / freeze / moniter_wifibt_status bookkeeping is software.

    Wire effects: monitor_bt_ctr (counters + reset), monitor_wifi_ctr (silent in monitor),
    update_wifi_link_info (the limited_tx backup reads), monitor_bt_enable (read_scbd); then the
    gated run_coex (first tick) and the gated post-periodical BT-FW-version query (first tick)."""
    _monitor_bt_ctr(t)
    _monitor_wifi_ctr(t, t.btc)
    _update_wifi_link_info(t, t.btc)
    _read_scbd(t)
    if st.first_tick:
        run_coex(t, _RSN_PERIODICAL)
    if not st.fw_ver_queried:
        _get_bt_patch_ver(t, st)
        st.fw_ver_queried = True
    st.first_tick = False
