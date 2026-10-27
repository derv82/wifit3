"""rtlwifi rtl8192cu/rf.c: RF6052 bring-up and bandwidth."""
from __future__ import annotations

import logging
import time

from . import constants as C
from . import phy  # rf.c and phy.c call each other
from .priv import RtlPriv
from .wifi import rtl_get_bbreg, rtl_set_bbreg, rtl_set_rfreg

logger = logging.getLogger(__name__)


def rtl92cu_phy_rf6052_set_bandwidth(rtlpriv: RtlPriv, bandwidth: int) -> None:
    """rf.c:13."""
    rtlphy = rtlpriv.phy
    if bandwidth == C.HT_CHANNEL_WIDTH_20:
        rtlphy.rfreg_chnlval[0] = (rtlphy.rfreg_chnlval[0] & 0xfffff3ff) | 0x0400
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, C.RF_CHNLBW, C.RFREG_OFFSET_MASK, rtlphy.rfreg_chnlval[0])
    elif bandwidth == C.HT_CHANNEL_WIDTH_20_40:
        rtlphy.rfreg_chnlval[0] = rtlphy.rfreg_chnlval[0] & 0xfffff3ff
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, C.RF_CHNLBW, C.RFREG_OFFSET_MASK, rtlphy.rfreg_chnlval[0])
    else:
        logger.error("unknown bandwidth: %#X", bandwidth)


def rtl92cu_phy_rf6052_config(rtlpriv: RtlPriv) -> bool:
    """rf.c:362."""
    rtlphy = rtlpriv.phy
    b_reg_hwparafile = 1
    rtstatus = True
    rtlphy.num_total_rfpath = 1 if rtlphy.rf_type == C.RF_1T1R else 2
    if b_reg_hwparafile == 1:
        rtstatus = _rtl92c_phy_rf6052_config_parafile(rtlpriv)
    return rtstatus


def _rtl92c_phy_rf6052_config_parafile(rtlpriv: RtlPriv) -> bool:
    """rf.c:378."""
    rtlphy = rtlpriv.phy
    u4_regvalue = 0
    rtstatus = True
    for rfpath in range(rtlphy.num_total_rfpath):
        pphyreg = rtlphy.phyreg_def[rfpath]
        if rfpath in (C.RF90_PATH_A, C.RF90_PATH_C):
            u4_regvalue = rtl_get_bbreg(rtlpriv, pphyreg.rfintfs, C.BRFSI_RFENV)
        elif rfpath in (C.RF90_PATH_B, C.RF90_PATH_D):
            u4_regvalue = rtl_get_bbreg(rtlpriv, pphyreg.rfintfs, C.BRFSI_RFENV << 16)
        rtl_set_bbreg(rtlpriv, pphyreg.rfintfe, C.BRFSI_RFENV << 16, 0x1)
        time.sleep(1e-6)
        rtl_set_bbreg(rtlpriv, pphyreg.rfintfo, C.BRFSI_RFENV, 0x1)
        time.sleep(1e-6)
        rtl_set_bbreg(rtlpriv, pphyreg.rfhssi_para2, C.B3WIREADDREAALENGTH, 0x0)
        time.sleep(1e-6)
        rtl_set_bbreg(rtlpriv, pphyreg.rfhssi_para2, C.B3WIREDATALENGTH, 0x0)
        time.sleep(1e-6)
        if rfpath in (C.RF90_PATH_A, C.RF90_PATH_B):
            rtstatus = phy.rtl92cu_phy_config_rf_with_headerfile(rtlpriv, rfpath)
        if rfpath in (C.RF90_PATH_A, C.RF90_PATH_C):
            rtl_set_bbreg(rtlpriv, pphyreg.rfintfs, C.BRFSI_RFENV, u4_regvalue)
        elif rfpath in (C.RF90_PATH_B, C.RF90_PATH_D):
            rtl_set_bbreg(rtlpriv, pphyreg.rfintfs, C.BRFSI_RFENV << 16, u4_regvalue)
        if not rtstatus:
            break
    return rtstatus
