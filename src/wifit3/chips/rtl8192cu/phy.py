"""rtlwifi rtl8192cu/phy.c: RF register access, MAC/BB/RF table config, BW switch, LCK."""
from __future__ import annotations

import logging
import time

from . import constants as C
from . import rf  # rf.c and phy.c call each other
from .core import rtl_addr_delay, rtl_rfreg_delay
from .mac import IS_92C_SERIAL
from .phy_common import (
    _rtl92c_phy_bb8192c_config_parafile,
    _rtl92c_phy_fw_rf_serial_read,
    _rtl92c_phy_fw_rf_serial_write,
    _rtl92c_phy_init_bb_rf_register_definition,
    _rtl92c_phy_rf_serial_read,
    _rtl92c_phy_rf_serial_write,
    _rtl92c_store_pwrindex_diffrate_offset,
)
from .priv import RtlPriv
from .wifi import calculate_bit_shift, is_hal_stop, rtl_get_rfreg, rtl_set_bbreg, rtl_set_rfreg

logger = logging.getLogger(__name__)


def rtl92cu_phy_query_rf_reg(rtlpriv: RtlPriv, rfpath: int, regaddr: int, bitmask: int) -> int:
    """phy.c:18."""
    if rtlpriv.phy.rf_mode != C.RF_OP_BY_FW:
        original_value = _rtl92c_phy_rf_serial_read(rtlpriv, rfpath, regaddr)
    else:
        original_value = _rtl92c_phy_fw_rf_serial_read(rtlpriv, rfpath, regaddr)
    bitshift = calculate_bit_shift(bitmask)
    return (original_value & bitmask) >> bitshift


def rtl92cu_phy_set_rf_reg(rtlpriv: RtlPriv, rfpath: int, regaddr: int, bitmask: int, data: int) -> None:
    """phy.c:43. A masked write folds ``data`` in unmasked, as in C."""
    if rtlpriv.phy.rf_mode != C.RF_OP_BY_FW:
        if bitmask != C.RFREG_OFFSET_MASK:
            original_value = _rtl92c_phy_rf_serial_read(rtlpriv, rfpath, regaddr)
            bitshift = calculate_bit_shift(bitmask)
            data = ((original_value & ~bitmask) | (data << bitshift)) & 0xFFFFFFFF
        _rtl92c_phy_rf_serial_write(rtlpriv, rfpath, regaddr, data)
    else:
        if bitmask != C.RFREG_OFFSET_MASK:
            original_value = _rtl92c_phy_fw_rf_serial_read(rtlpriv, rfpath, regaddr)
            bitshift = calculate_bit_shift(bitmask)
            data = ((original_value & ~bitmask) | (data << bitshift)) & 0xFFFFFFFF
        _rtl92c_phy_fw_rf_serial_write(rtlpriv, rfpath, regaddr, data)


def rtl92cu_phy_mac_config(rtlpriv: RtlPriv) -> bool:
    return _rtl92cu_phy_config_mac_with_headerfile(rtlpriv)


def rtl92cu_phy_bb_config(rtlpriv: RtlPriv) -> bool:
    """phy.c:90."""
    io = rtlpriv.io
    b_reg_hwparafile = 1
    rtstatus = True
    _rtl92c_phy_init_bb_rf_register_definition(rtlpriv)
    regval = io.read16(C.REG_SYS_FUNC_EN)
    io.write16(C.REG_SYS_FUNC_EN, regval | C.BIT(13) | C.BIT(0) | C.BIT(1))
    io.write8(C.REG_AFE_PLL_CTRL, 0x83)
    io.write8(C.REG_AFE_PLL_CTRL + 1, 0xdb)
    io.write8(C.REG_RF_CTRL, C.RF_EN | C.RF_RSTB | C.RF_SDMRSTB)
    io.write8(C.REG_SYS_FUNC_EN, C.FEN_USBA | C.FEN_USBD | C.FEN_BB_GLB_RSTN | C.FEN_BBRSTB)
    regval32 = io.read32(C.RFPGA0_XCD_RFPARAMETER)
    io.write32(C.RFPGA0_XCD_RFPARAMETER, regval32 & ~C.BIT(31))
    io.write8(C.REG_LDOHCI12_CTRL, 0x0f)
    io.write8(C.REG_AFE_XTAL_CTRL + 1, 0x80)
    if b_reg_hwparafile == 1:
        rtstatus = _rtl92c_phy_bb8192c_config_parafile(rtlpriv)
    return rtstatus


def _rtl92cu_phy_config_mac_with_headerfile(rtlpriv: RtlPriv) -> bool:
    """phy.c:116."""
    ptrarray = rtlpriv.phy.hwparam_tables[C.MAC_REG]
    for i in range(0, len(ptrarray), 2):
        rtlpriv.io.write8(ptrarray[i], ptrarray[i + 1] & 0xFF)
    return True


def _rtl92cu_phy_config_bb_with_headerfile(rtlpriv: RtlPriv, configtype: int) -> bool:
    """phy.c:133. PHY_REG rows still write their address after rtl_addr_delay, as in C."""
    rtlphy = rtlpriv.phy
    if IS_92C_SERIAL(rtlpriv.rtlhal.version):
        agctab_array_table = rtlphy.hwparam_tables[C.AGCTAB_2T]
        phy_regarray_table = rtlphy.hwparam_tables[C.PHY_REG_2T]
    else:
        agctab_array_table = rtlphy.hwparam_tables[C.AGCTAB_1T]
        phy_regarray_table = rtlphy.hwparam_tables[C.PHY_REG_1T]
    if configtype == C.BASEBAND_CONFIG_PHY_REG:
        for i in range(0, len(phy_regarray_table), 2):
            rtl_addr_delay(phy_regarray_table[i])
            rtl_set_bbreg(rtlpriv, phy_regarray_table[i], C.MASKDWORD, phy_regarray_table[i + 1])
            time.sleep(1e-6)
    elif configtype == C.BASEBAND_CONFIG_AGC_TAB:
        for i in range(0, len(agctab_array_table), 2):
            rtl_set_bbreg(rtlpriv, agctab_array_table[i], C.MASKDWORD, agctab_array_table[i + 1])
            time.sleep(1e-6)
    return True


def _rtl92cu_phy_config_bb_with_pgheaderfile(rtlpriv: RtlPriv, configtype: int) -> bool:
    """phy.c:180."""
    rtlphy = rtlpriv.phy
    rtlphy.pwrgroup_cnt = 0
    phy_regarray_table_pg = rtlphy.hwparam_tables[C.PHY_REG_PG]
    if configtype == C.BASEBAND_CONFIG_PHY_REG:
        for i in range(0, len(phy_regarray_table_pg), 3):
            rtl_addr_delay(phy_regarray_table_pg[i])
            _rtl92c_store_pwrindex_diffrate_offset(rtlpriv, phy_regarray_table_pg[i],
                                                   phy_regarray_table_pg[i + 1], phy_regarray_table_pg[i + 2])
    return True


def rtl92cu_phy_config_rf_with_headerfile(rtlpriv: RtlPriv, rfpath: int) -> bool:
    """phy.c:207."""
    rtlphy = rtlpriv.phy
    if IS_92C_SERIAL(rtlpriv.rtlhal.version):
        radioa_array_table = rtlphy.hwparam_tables[C.RADIOA_2T]
        radiob_array_table = rtlphy.hwparam_tables[C.RADIOB_2T]
    else:
        radioa_array_table = rtlphy.hwparam_tables[C.RADIOA_1T]
        radiob_array_table = rtlphy.hwparam_tables[C.RADIOB_1T]
    if rfpath == C.RF90_PATH_A:
        for i in range(0, len(radioa_array_table), 2):
            rtl_rfreg_delay(rtlpriv, rfpath, radioa_array_table[i], C.RFREG_OFFSET_MASK, radioa_array_table[i + 1])
    elif rfpath == C.RF90_PATH_B:
        for i in range(0, len(radiob_array_table), 2):
            rtl_rfreg_delay(rtlpriv, rfpath, radiob_array_table[i], C.RFREG_OFFSET_MASK, radiob_array_table[i + 1])
    elif rfpath in (C.RF90_PATH_C, C.RF90_PATH_D):
        logger.error("switch case %#x not processed", rfpath)
    return True


def rtl92cu_phy_set_bw_mode_callback(rtlpriv: RtlPriv) -> None:
    """phy.c:263."""
    io = rtlpriv.io
    rtlphy = rtlpriv.phy
    mac = rtlpriv.mac80211
    if is_hal_stop(rtlpriv.rtlhal):
        rtlphy.set_bwmode_inprogress = False
        return
    reg_bw_opmode = io.read8(C.REG_BWOPMODE)
    reg_prsr_rsc = io.read8(C.REG_RRSR + 2)
    if rtlphy.current_chan_bw == C.HT_CHANNEL_WIDTH_20:
        reg_bw_opmode |= C.BW_OPMODE_20MHZ
        io.write8(C.REG_BWOPMODE, reg_bw_opmode)
    elif rtlphy.current_chan_bw == C.HT_CHANNEL_WIDTH_20_40:
        reg_bw_opmode &= ~C.BW_OPMODE_20MHZ
        io.write8(C.REG_BWOPMODE, reg_bw_opmode)
        reg_prsr_rsc = (reg_prsr_rsc & 0x90) | (mac.cur_40_prime_sc << 5)
        io.write8(C.REG_RRSR + 2, reg_prsr_rsc)
    else:
        logger.error("unknown bandwidth: %#X", rtlphy.current_chan_bw)
    if rtlphy.current_chan_bw == C.HT_CHANNEL_WIDTH_20:
        rtl_set_bbreg(rtlpriv, C.RFPGA0_RFMOD, C.BRFMOD, 0x0)
        rtl_set_bbreg(rtlpriv, C.RFPGA1_RFMOD, C.BRFMOD, 0x0)
        rtl_set_bbreg(rtlpriv, C.RFPGA0_ANALOGPARAMETER2, C.BIT(10), 1)
    elif rtlphy.current_chan_bw == C.HT_CHANNEL_WIDTH_20_40:
        rtl_set_bbreg(rtlpriv, C.RFPGA0_RFMOD, C.BRFMOD, 0x1)
        rtl_set_bbreg(rtlpriv, C.RFPGA1_RFMOD, C.BRFMOD, 0x1)
        rtl_set_bbreg(rtlpriv, C.RCCK0_SYSTEM, C.BCCK_SIDEBAND, mac.cur_40_prime_sc >> 1)
        rtl_set_bbreg(rtlpriv, C.ROFDM1_LSTF, 0xC00, mac.cur_40_prime_sc)
        rtl_set_bbreg(rtlpriv, C.RFPGA0_ANALOGPARAMETER2, C.BIT(10), 0)
        rtl_set_bbreg(rtlpriv, 0x818, C.BIT(26) | C.BIT(27),
                      2 if mac.cur_40_prime_sc == C.HAL_PRIME_CHNL_OFFSET_LOWER else 1)
    else:
        logger.error("unknown bandwidth: %#X", rtlphy.current_chan_bw)
    rf.rtl92cu_phy_rf6052_set_bandwidth(rtlpriv, rtlphy.current_chan_bw)
    rtlphy.set_bwmode_inprogress = False


def rtl92cu_bb_block_on(rtlpriv: RtlPriv) -> None:
    """phy.c:325."""
    rtl_set_bbreg(rtlpriv, C.RFPGA0_RFMOD, C.BCCKEN, 0x1)
    rtl_set_bbreg(rtlpriv, C.RFPGA0_RFMOD, C.BOFDMEN, 0x1)


def _rtl92cu_phy_lc_calibrate(rtlpriv: RtlPriv, is2t: bool) -> None:
    """phy.c:335; 0xd03 is unnamed in the source."""
    io = rtlpriv.io
    rf_a_mode = rf_b_mode = 0
    tmpreg = io.read8(0xd03)
    if (tmpreg & 0x70) != 0:
        io.write8(0xd03, tmpreg & 0x8F)
    else:
        io.write8(C.REG_TXPAUSE, 0xFF)
    if (tmpreg & 0x70) != 0:
        rf_a_mode = rtl_get_rfreg(rtlpriv, C.RF90_PATH_A, 0x00, C.MASK12BITS)
        if is2t:
            rf_b_mode = rtl_get_rfreg(rtlpriv, C.RF90_PATH_B, 0x00, C.MASK12BITS)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, 0x00, C.MASK12BITS, (rf_a_mode & 0x8FFFF) | 0x10000)
        if is2t:
            rtl_set_rfreg(rtlpriv, C.RF90_PATH_B, 0x00, C.MASK12BITS, (rf_b_mode & 0x8FFFF) | 0x10000)
    lc_cal = rtl_get_rfreg(rtlpriv, C.RF90_PATH_A, 0x18, C.MASK12BITS)
    rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, 0x18, C.MASK12BITS, lc_cal | 0x08000)
    time.sleep(100e-3)
    if (tmpreg & 0x70) != 0:
        io.write8(0xd03, tmpreg)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, 0x00, C.MASK12BITS, rf_a_mode)
        if is2t:
            rtl_set_rfreg(rtlpriv, C.RF90_PATH_B, 0x00, C.MASK12BITS, rf_b_mode)
    else:
        io.write8(C.REG_TXPAUSE, 0x00)
