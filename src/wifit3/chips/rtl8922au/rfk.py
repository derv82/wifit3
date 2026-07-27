"""RTL8922AU RF calibration (RFK), ported from rtw89-7.2 rtw8922a_rfk.c / phy.c / fw.c.

rfk_init_late runs the boot-time DACK + RXDCK per phy, all as firmware offload H2Cs (the driver
sends the command and the fw does the calibration). The per-channel RFK (IQK/DPK/TSSI) comes with
set_channel. [SRC] rtw8922a.c:2349-2367.
"""
import struct

from .constants import (
    H2C_CAT_OUTSRC, H2C_CL_OUTSRC_RF_FW_RFK, H2C_CL_OUTSRC_RF_FW_NOTIFY,
    H2C_FUNC_RFK_PRE_NOTIFY, H2C_FUNC_RFK_DACK_OFFLOAD, H2C_FUNC_RFK_RXDCK_OFFLOAD,
    H2C_FUNC_OUTSRC_RF_MCC_INFO,
)
from . import firmware

_MLO_1_PLUS_1_1RF = 0x1011           # enum rtw89_mlo_dbcc_mode. core.h:4170


def _pre_ntfy(phy_idx: int) -> bytes:
    """rtw89_fw_h2c_rf_pre_ntfy v2 payload: cold chanctx (all zero) + mlo mode/flag + phy_idx.
    [SRC] fw.c:7360, fw.h:4926."""
    b = bytearray(84)
    struct.pack_into("<I", b, 48, _MLO_1_PLUS_1_1RF)     # common.mlo_mode
    struct.pack_into("<I", b, 68, phy_idx)               # common.phy_idx
    struct.pack_into("<I", b, 72, 1)                     # v1.mlo_1_1 (is_mlo_1_1)
    return bytes(b)


def _pre_ntfy_mcc() -> bytes:
    """rtw89_fw_h2c_rf_pre_ntfy_mcc v0: zeroed tables + mlo mode. [SRC] fw.c:7489, fw.h:4937."""
    b = bytearray(36)
    struct.pack_into("<I", b, 32, _MLO_1_PLUS_1_1RF)
    return bytes(b)


def _dack(phy_idx: int) -> bytes:
    """rtw89_fw_h2c_rf_dack: {len=3, phy, type=0}. [SRC] fw.c:7787."""
    return bytes((3, phy_idx, 0))


def _rxdck(phy_idx: int) -> bytes:
    """rtw89_fw_h2c_rf_rxdck: {len=9, phy, is_afe=0, kpath=RF_AB, band/bw/ch=0, dbg=0, is_chl_k=0}.
    [SRC] fw.c:7824."""
    return bytes((9, phy_idx, 0, 0x3, 0, 0, 0, 0, 0))


def _h2c(t, ep: int, cls: int, func: int, payload: bytes) -> None:
    firmware.h2c_command(t, ep, H2C_CAT_OUTSRC, cls, func, payload, rack=False, dack=False)


def _init_late_one(t, ep: int, phy_idx: int) -> None:
    """__rtw8922a_rfk_init_late(phy): pre-notify (+mcc), DACK, RXDCK, all fw offload. [SRC]
    rtw8922a.c:2349."""
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_PRE_NOTIFY, _pre_ntfy(phy_idx))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_NOTIFY, H2C_FUNC_OUTSRC_RF_MCC_INFO, _pre_ntfy_mcc())
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_DACK_OFFLOAD, _dack(phy_idx))
    _h2c(t, ep, H2C_CL_OUTSRC_RF_FW_RFK, H2C_FUNC_RFK_RXDCK_OFFLOAD, _rxdck(phy_idx))


def rfk_init_late(t, ep: int) -> None:
    """rtw8922a_rfk_init_late: DACK + RXDCK for PHY_0 then (DBCC) PHY_1. [SRC] rtw8922a.c:2360."""
    _init_late_one(t, ep, 0)
    _init_late_one(t, ep, 1)
