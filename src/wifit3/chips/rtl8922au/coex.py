"""RTL8922AU BT-coexistence init, ported from rtw89-7.2 coex.c and rtw8922a.c.

rtw89_btc_ntfy_init configures the WL/BT antenna sharing before the interface runs. On this card
(rfe_type 1) btc_set_rfe resolves to a 2-antenna shared setup with the BT-general path on RF path
B, and btc_init_cfg programs the per-path trx-mask LUT and the PTA priority / break tables.
"""
from .constants import (
    RF_PATH_A, RF_PATH_B, RR_LUTWE, RR_LUTWA, RR_LUTWD0, B_LUTWEN,
    BTC_BT_SS_GROUP, BTC_BT_TX_GROUP, BTC_BT_RX_GROUP,
    BTC_TRX_MASK_SS, BTC_TRX_MASK_RX, BTC_TRX_MASK_TX_BTG, BTC_TRX_MASK_TX,
    R_BTC_COEX_WL_REQ_BE, B_BTC_RSP_ACK_HI, B_BTC_TX_BCN_HI, B_BTC_TX_TRI_HI, B_BTC_TX_NULL_HI,
    R_BE_BT_BREAK_TABLE, BTC_BREAK_PARAM, R_BTC_ZB_COEX_TBL_0, R_BTC_ZB_COEX_TBL_1,
    R_BTC_ZB_BREAK_TBL, BTC_ZB_COEX_TBL_VAL, BTC_ZB_BREAK_TBL_VAL,
)
from . import phy


def _set_rfe(rfe_type: int, cv: int) -> dict:
    """rtw8922a_btc_set_rfe (software): resolve the antenna module info from efuse rfe_type and
    chip cut. Returns the fields btc_init_cfg consumes. [SRC] rtw8922a.c:2727-2769."""
    ant_num = 2 if (rfe_type % 2) else 3
    if cv == 0:
        ant_num = 2
    shared = ant_num != 3                    # num 3 -> dedicated, else shared
    btg_pos = RF_PATH_B
    single_pos = RF_PATH_A
    return {"num": ant_num, "shared": shared, "btg_pos": btg_pos, "single_pos": single_pos}


def _set_trx_mask(t, path: int, group: int, val: int) -> None:
    """rtw8922a_set_trx_mask: write the group index then its WL trx-mask value. [SRC] rtw8922a.c:2771."""
    phy.write_rf(t, path, RR_LUTWA, group)
    phy.write_rf(t, path, RR_LUTWD0, val)


def _init_cfg(t, ant: dict) -> None:
    """rtw8922a_btc_init_cfg: per-path trx-mask LUT setup, then the PTA priority, break table, and
    ZB coex tables. [SRC] rtw8922a.c:2778-2833."""
    if ant["num"] == 1:
        path_min = path_max = ant["single_pos"]
    else:
        path_min, path_max = RF_PATH_A, RF_PATH_B

    for path in range(path_min, path_max + 1):
        phy.write_rf(t, path, RR_LUTWE, B_LUTWEN)
        _set_trx_mask(t, path, BTC_BT_SS_GROUP, BTC_TRX_MASK_SS)
        _set_trx_mask(t, path, BTC_BT_RX_GROUP, BTC_TRX_MASK_RX)
        if ant["shared"] and ant["btg_pos"] == path:
            _set_trx_mask(t, path, BTC_BT_TX_GROUP, BTC_TRX_MASK_TX_BTG)
        else:
            _set_trx_mask(t, path, BTC_BT_TX_GROUP, BTC_TRX_MASK_TX)
        phy.write_rf(t, path, RR_LUTWE, 0)

    wl_pri = B_BTC_RSP_ACK_HI | B_BTC_TX_BCN_HI | B_BTC_TX_TRI_HI | B_BTC_TX_NULL_HI
    t.write32(R_BTC_COEX_WL_REQ_BE, wl_pri)
    t.write32(R_BE_BT_BREAK_TABLE, BTC_BREAK_PARAM)
    t.write32(R_BTC_ZB_COEX_TBL_0, BTC_ZB_COEX_TBL_VAL)
    t.write32(R_BTC_ZB_COEX_TBL_1, BTC_ZB_COEX_TBL_VAL)
    t.write32(R_BTC_ZB_BREAK_TBL, BTC_ZB_BREAK_TBL_VAL)


def ntfy_init(t, h2c_ep: int, cv: int) -> None:
    """rtw89_btc_ntfy_init(BTC_MODE_NORMAL): the wire-emitting part is btc_set_rfe (software) plus
    btc_init_cfg. [SRC] coex.c:7746."""
    ant = _set_rfe(t.rfe_type, cv)
    _init_cfg(t, ant)
