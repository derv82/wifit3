"""rtlwifi rtl8192cu/sw.c: software state, firmware selection, and the hal ops table."""
from __future__ import annotations

import logging

from . import constants as C
from .core import request_firmware, rtl_fw_cb
from .hw import rtl92cu_set_hw_reg
from .mac import IS_81XXC_VENDOR_UMC_B_CUT, IS_92C_SERIAL, IS_VENDOR_UMC_A_CUT
from .phy import (
    _rtl92cu_phy_config_bb_with_headerfile,
    _rtl92cu_phy_config_bb_with_pgheaderfile,
    _rtl92cu_phy_lc_calibrate,
    rtl92cu_phy_query_rf_reg,
    rtl92cu_phy_set_bw_mode_callback,
    rtl92cu_phy_set_rf_reg,
)
from .phy_common import rtl92c_phy_query_bb_reg, rtl92c_phy_set_bb_reg, rtl92c_phy_set_bw_mode
from .priv import RtlHalCfg, RtlHalOps, RtlModParams, RtlPriv
from .rf import rtl92cu_phy_rf6052_config

logger = logging.getLogger(__name__)


def rtl92cu_init_sw_vars(rtlpriv: RtlPriv) -> int:
    """sw.c:31. request_firmware_nowait's callback runs inline here."""
    rtlpriv.dm.dm_initialgain_enable = True
    rtlpriv.dm.dm_flag = 0
    rtlpriv.dm.disable_framebursting = False
    rtlpriv.dm.thermalvalue = 0
    rtlpriv.rtlhal.pfirmware = bytes(C.MAX_FW_SIZE)
    version = rtlpriv.rtlhal.version
    if IS_VENDOR_UMC_A_CUT(version) and not IS_92C_SERIAL(version):
        fw_name = "rtl8192cufw_A.bin"
    elif IS_81XXC_VENDOR_UMC_B_CUT(version):
        fw_name = "rtl8192cufw_B.bin"
    else:
        fw_name = "rtl8192cufw_TMSC.bin"
    rtlpriv.rtlhal.fw_name = fw_name
    logger.info("Loading firmware %s", fw_name)
    rtlpriv.max_fw_size = C.MAX_FW_SIZE
    rtl_fw_cb(rtlpriv, request_firmware(fw_name))
    return 0


rtl8192cu_hal_ops = RtlHalOps(
    set_hw_reg=rtl92cu_set_hw_reg,
    set_bw_mode=rtl92c_phy_set_bw_mode,
    get_bbreg=rtl92c_phy_query_bb_reg,
    set_bbreg=rtl92c_phy_set_bb_reg,
    get_rfreg=rtl92cu_phy_query_rf_reg,
    set_rfreg=rtl92cu_phy_set_rf_reg,
    phy_rf6052_config=rtl92cu_phy_rf6052_config,
    config_bb_with_headerfile=_rtl92cu_phy_config_bb_with_headerfile,
    config_bb_with_pgheaderfile=_rtl92cu_phy_config_bb_with_pgheaderfile,
    phy_lc_calibrate=_rtl92cu_phy_lc_calibrate,
    phy_set_bw_mode_callback=rtl92cu_phy_set_bw_mode_callback,
)

rtl92cu_hal_cfg = RtlHalCfg(
    name="rtl92c_usb",
    alt_fw_name="rtl8192cufw.bin",
    ops=rtl8192cu_hal_ops,
    mod_params=RtlModParams(sw_crypto=False),
)
