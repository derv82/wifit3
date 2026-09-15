"""RTL8821CU WiFi-only coexistence — the front-end setup a no-BT (bt_coexist=FALSE) card takes.

`rtl8821c_hal_init` runs `rtw_btcoex_wifionly_hw_config` instead of the BT-coex HAL init when the
card reports no Bluetooth (a no-BT combo die, e.g. MercuSYS MU6H); the phydm band switch likewise
routes through `rtw_btcoex_wifionly_switchband_notify` -> `switch_antenna`. This module ports that
WiFi-only path: without it a no-BT card crashes on the first tune (btc `t.btc` is never created) and
its WiFi front-end (GNT owner, coex tables, antenna switch) is never routed to WL.

`halwifionly_phy_set_bb_reg` is `phy_set_bb_reg` (== ``bb.set_bb_reg``), so BB writes map straight
to it; the four full-dword LTE-coex / coex-table writes match btc.py's direct ``t.write32`` style.

Ported from:
  [SRC] hal/btc/halbtc8821cwifionly.c:166  ex_hal8821c_wifi_only_hw_config
  [SRC] hal/btc/halbtc8821cwifionly.c:22   hal8821c_wifi_only_switch_antenna
  [SRC] hal/btc/halbtc8821cwifionly.c:93   halbtc8821c_wifi_only_set_rfe_type
  [SRC] hal/rtl8821c/rtl8821c_halinit.c:294 / rtl8821c_phy.c:719 (the else arms)
"""
from __future__ import annotations

from . import btc
from .bb import set_bb_reg

# ext-ant-switch position/control selectors [SRC] halbtc8821cwifionly.h:41-55
_CTRL_BY_BBSW, _CTRL_BY_ANTDIV = 0x0, 0x2
_TO_WLG, _TO_WLA = 0x1, 0x2
# AntDivCfg (`haldata_info.ant_div_cfg = pHalData->AntDivCfg`) — the driver-registry antenna
# diversity flag, off on this 1T1R card, so ctrl resolves to BBSW [SRC] halbtc8821cwifionly.c:59.
_ANT_DIV_CFG = False


def _decode_rfe(rfe_type: int) -> btc.RfeType:
    """halbtc8821c_wifi_only_set_rfe_type [SRC] halbtc8821cwifionly.c:93-163. Differs from
    btc._decode_rfe: only module types 1-7 are recognized (all others fall to case 0's WLG/main),
    so decodes for module types 10-15 diverge from the 1-ant table — port it, don't reuse."""
    m = rfe_type & 0x1F
    no_switch = m in (5, 6)                 # 2-Ant, no antenna switch (cases 5/6)
    aux_port = m in (3, 4)                  # 1-Ant at Aux (cases 3/4)
    wlg_locate_at_btg = m in (2, 4, 7)      # WLG located at BTG (cases 2/4/7)
    return btc.RfeType(ext_ant_switch_exist=not no_switch,
                       ant_at_main_port=not aux_port, wlg_locate_at_btg=wlg_locate_at_btg)


def hw_config(t, info) -> None:
    """ex_hal8821c_wifi_only_hw_config [SRC] halbtc8821cwifionly.c:166 — the no-BT front-end setup
    `rtl8821c_hal_init` runs in place of the BT-coex HAL init: GNT owner -> WL, gnt_wl=1/gnt_bt=0
    via the LTE-coex 0x38 word (two direct BB writes), and lay the WiFi coex tables."""
    _decode_rfe(info.rfe_type)                       # set_rfe_type: pure logic, no wire effect
    set_bb_reg(t, 0x70, 0x04000000, 0x1)             # gnt_wl/gnt_bt control owner -> WL
    t.write32(0x1704, 0x7700)                        # gnt_wl=1, gnt_bt=0 (LTE-coex 0x38 wdata)
    t.write32(0x1700, 0xC00F0038)                    # LTE-coex indirect write to 0x38
    t.write32(0x06C0, 0xAAAAAAAA)
    t.write32(0x06C4, 0xAAAAAAAA)


def switch_antenna(t, info, is_5g: bool) -> None:
    """hal8821c_wifi_only_switch_antenna [SRC] halbtc8821cwifionly.c:22 — route the external antenna
    switch to the WiFi band. The scan/connect/switchband notifies all reduce to this; on a no-BT
    card the band-switch path calls it in place of the btc switchband notify."""
    rfe = _decode_rfe(info.rfe_type)
    if not rfe.ext_ant_switch_exist:
        return
    switch_polarity_inverse = bool(rfe.ext_ant_switch_ctrl_polarity)
    if not rfe.ant_at_main_port:
        switch_polarity_inverse = not switch_polarity_inverse
    pos_type = _TO_WLA if is_5g else _TO_WLG
    if pos_type == _TO_WLG and not rfe.wlg_locate_at_btg:
        switch_polarity_inverse = not switch_polarity_inverse
    ctrl_type = _CTRL_BY_ANTDIV if _ANT_DIV_CFG else _CTRL_BY_BBSW
    if ctrl_type == _CTRL_BY_BBSW:
        set_bb_reg(t, 0x4C, 0x01800000, 0x2)
        set_bb_reg(t, 0xCB4, 0x000000FF, 0x77)       # DPDT uses RFE_ctrl8/9 as control pin
        regval_0xcb7 = 0x1 if not switch_polarity_inverse else 0x2
        set_bb_reg(t, 0xCB4, 0x30000000, regval_0xcb7)
    elif ctrl_type == _CTRL_BY_ANTDIV:
        set_bb_reg(t, 0x4C, 0x01800000, 0x2)
        set_bb_reg(t, 0xCB4, 0x000000FF, 0x88)       # switch value driven by antenna diversity
