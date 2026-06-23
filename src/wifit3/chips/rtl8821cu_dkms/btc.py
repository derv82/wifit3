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
_TO_WLG = 1                     # the only pos that flips polarity (when wlg not at btg)


@dataclass
class BtcState:
    """The slice of btc `coex_sta`/`coex_dm` the init phase keeps between calls: the decoded
    RFE board type, the concurrent-RX flag that selects the coex table, and the BT scoreboard
    mirror (write_scbd reads back its own last-written value, not the register)."""
    rfe: RfeType
    concurrent_rx_mode_on: bool = False
    scbd_val: int = _SCBD_INIT
    scbd_prev: int = 0


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
_COEX_TABLE = {0: (0x55555555, 0x55555555)}


def _table(t, st: BtcState, type_: int) -> None:
    """halbtc8821c1ant_table(FC_EXCU, type) [SRC] :1674 -> set_table (force_exec writes all 4
    rows, no read-back). concurrent_rx_mode_on picks the WL-hi-pri break/select tables. Init
    lays type 0 only; the runtime action-algorithm tables aren't in the cold-boot window."""
    if st.concurrent_rx_mode_on:
        break_table, select_table = 0xF0FFFFFF, 0x1B
    else:
        break_table, select_table = 0x00FFFFFF, 0x13
    v6c0, v6c4 = _COEX_TABLE[type_]
    t.write32(REG_COEX_TABLE0, v6c0)
    t.write32(REG_COEX_TABLE1, v6c4)
    t.write32(REG_COEX_BREAK_TABLE, break_table)
    t.write8(REG_COEX_TABLE_TYPE, select_table)


def _fill_h2c(t, cmd_id: int, pbuf: tuple[int, ...]) -> None:
    """btc_fill_h2c -> rtw_hal_fill_h2c_cmd -> rtl8821c_fillh2ccmd [SRC] rtl8821c_cmd.c:32 —
    prepend the command id to the params and send through the HMEBOX rotation."""
    firmware.send_h2c_by_reg(t, bytes((cmd_id, *pbuf)))


def _tdma(t, st: BtcState) -> None:
    """halbtc8821c1ant_tdma(FC_EXCU, turn_on=FALSE, tcase=8) [SRC] :2101 — at init, TDMA-off
    type 8 is PTA control: clear the TDMA scoreboard bit (no-op, already clear) and send the
    stop-PS-TDMA H2C set_tdma(0x8,0,0,0,0)->0x60. set_tdma_timer_base(0) early-returns
    (timer_base already 0) and power_save_state(WIFI_NATIVE) only notifies — both wire-silent."""
    _write_scbd(t, st, _SCBD_TDMA, False)
    _fill_h2c(t, 0x60, (0x8, 0x0, 0x0, 0x0, 0x0))


def _query_bt_info(t) -> None:
    """halbtc8821c1ant_query_bt_info [SRC] :494 — trigger a BT-info report (H2C 0x61, BIT0).
    bt_disabled is FALSE at init, so the H2C is sent."""
    _fill_h2c(t, 0x61, (0x1,))


def hal_init(t, info) -> None:
    """halbtc8821c1ant_init_hw_config(back_up=TRUE, wifi_only=FALSE) [SRC] :3739 — the BT-coex
    HAL init `rtl8821c_hal_init` runs after `phy_bf_init`. PTA/3-wire enable, take the antenna
    to BT, lay the WiFi-only coex table.

    `init_coex_var` and `enable_gnt_to_gpio` (dbg_mode off) are wire-silent here; the else arm
    runs because the RF is on and the card is not WiFi-only."""
    st = BtcState(rfe=_decode_rfe(info.rfe_type))
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
    _table(t, st, 0)
    _tdma(t, st)
    _query_bt_info(t)
