"""rtlwifi rtl8192c/phy_common.c: BB/RF register access, BB config, IQK, LCK, RF path switch.

RT_CANNOT_IO is the constant false (phy_common.h:13), so its early returns are not ported.
"""
from __future__ import annotations

import logging
import time

from . import constants as C
from .mac import IS_92C_SERIAL
from .priv import RtlPriv
from .wifi import calculate_bit_shift, is_hal_stop, rtl_get_bbreg, rtl_set_bbreg

logger = logging.getLogger(__name__)


def rtl92c_phy_query_bb_reg(rtlpriv: RtlPriv, regaddr: int, bitmask: int) -> int:
    """phy_common.c:12."""
    originalvalue = rtlpriv.io.read32(regaddr)
    bitshift = calculate_bit_shift(bitmask)
    return (originalvalue & bitmask) >> bitshift


def rtl92c_phy_set_bb_reg(rtlpriv: RtlPriv, regaddr: int, bitmask: int, data: int) -> None:
    """phy_common.c:31. ``data`` is not masked: a value wider than ``bitmask`` spills, as in C."""
    if bitmask != C.MASKDWORD:
        originalvalue = rtlpriv.io.read32(regaddr)
        bitshift = calculate_bit_shift(bitmask)
        data = (originalvalue & ~bitmask) | (data << bitshift)
    rtlpriv.io.write32(regaddr, data & 0xFFFFFFFF)


def _rtl92c_phy_fw_rf_serial_read(rtlpriv: RtlPriv, rfpath: int, offset: int) -> int:
    """phy_common.c:55: deprecated in the source, returns 0."""
    logger.warning("rtl8192c-common: _rtl92c_phy_fw_rf_serial_read deprecated!")
    return 0


def _rtl92c_phy_fw_rf_serial_write(rtlpriv: RtlPriv, rfpath: int, offset: int, data: int) -> None:
    """phy_common.c:63: deprecated in the source, writes nothing."""
    logger.warning("rtl8192c-common: _rtl92c_phy_fw_rf_serial_write deprecated!")


def _rtl92c_phy_rf_serial_read(rtlpriv: RtlPriv, rfpath: int, offset: int) -> int:
    """phy_common.c:71."""
    pphyreg = rtlpriv.phy.phyreg_def[rfpath]
    newoffset = offset & 0x3f
    tmplong = rtl_get_bbreg(rtlpriv, C.RFPGA0_XA_HSSIPARAMETER2, C.MASKDWORD)
    if rfpath == C.RF90_PATH_A:
        tmplong2 = tmplong
    else:
        tmplong2 = rtl_get_bbreg(rtlpriv, pphyreg.rfhssi_para2, C.MASKDWORD)
    tmplong2 = (tmplong2 & ~C.BLSSIREADADDRESS) | (newoffset << 23) | C.BLSSIREADEDGE
    rtl_set_bbreg(rtlpriv, C.RFPGA0_XA_HSSIPARAMETER2, C.MASKDWORD, tmplong & ~C.BLSSIREADEDGE)
    time.sleep(1e-3)
    rtl_set_bbreg(rtlpriv, pphyreg.rfhssi_para2, C.MASKDWORD, tmplong2)
    time.sleep(1e-3)
    rtl_set_bbreg(rtlpriv, C.RFPGA0_XA_HSSIPARAMETER2, C.MASKDWORD, tmplong | C.BLSSIREADEDGE)
    time.sleep(1e-3)
    rfpi_enable = 0
    if rfpath == C.RF90_PATH_A:
        rfpi_enable = rtl_get_bbreg(rtlpriv, C.RFPGA0_XA_HSSIPARAMETER1, C.BIT(8)) & 0xFF
    elif rfpath == C.RF90_PATH_B:
        rfpi_enable = rtl_get_bbreg(rtlpriv, C.RFPGA0_XB_HSSIPARAMETER1, C.BIT(8)) & 0xFF
    if rfpi_enable:
        return rtl_get_bbreg(rtlpriv, pphyreg.rf_rbpi, C.BLSSIREADBACKDATA)
    return rtl_get_bbreg(rtlpriv, pphyreg.rf_rb, C.BLSSIREADBACKDATA)


def _rtl92c_phy_rf_serial_write(rtlpriv: RtlPriv, rfpath: int, offset: int, data: int) -> None:
    """phy_common.c:122."""
    pphyreg = rtlpriv.phy.phyreg_def[rfpath]
    newoffset = offset & 0x3f
    data_and_addr = ((newoffset << 20) | (data & 0x000fffff)) & 0x0fffffff
    rtl_set_bbreg(rtlpriv, pphyreg.rf3wire_offset, C.MASKDWORD, data_and_addr)


def _rtl92c_phy_bb_config_1t(rtlpriv: RtlPriv) -> None:
    """phy_common.c:146; the 0xe74-0xe88 registers are unnamed in the source."""
    rtl_set_bbreg(rtlpriv, C.RFPGA0_TXINFO, 0x3, 0x2)
    rtl_set_bbreg(rtlpriv, C.RFPGA1_TXINFO, 0x300033, 0x200022)
    rtl_set_bbreg(rtlpriv, C.RCCK0_AFESETTING, C.MASKBYTE3, 0x45)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_TRXPATHENABLE, C.MASKBYTE0, 0x23)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_AGCPARAMETER1, 0x30, 0x1)
    rtl_set_bbreg(rtlpriv, 0xe74, 0x0c000000, 0x2)
    rtl_set_bbreg(rtlpriv, 0xe78, 0x0c000000, 0x2)
    rtl_set_bbreg(rtlpriv, 0xe7c, 0x0c000000, 0x2)
    rtl_set_bbreg(rtlpriv, 0xe80, 0x0c000000, 0x2)
    rtl_set_bbreg(rtlpriv, 0xe88, 0x0c000000, 0x2)


def rtl92c_phy_rf_config(rtlpriv: RtlPriv) -> bool:
    return rtlpriv.cfg.ops.phy_rf6052_config(rtlpriv)


def _rtl92c_phy_bb8192c_config_parafile(rtlpriv: RtlPriv) -> bool:
    """phy_common.c:168."""
    rtlphy = rtlpriv.phy
    rtstatus = rtlpriv.cfg.ops.config_bb_with_headerfile(rtlpriv, C.BASEBAND_CONFIG_PHY_REG)
    if not rtstatus:
        logger.error("Write BB Reg Fail!!")
        return False
    if rtlphy.rf_type == C.RF_1T2R:
        _rtl92c_phy_bb_config_1t(rtlpriv)
    if not rtlpriv.efuse.autoload_failflag:
        rtlphy.pwrgroup_cnt = 0
        rtstatus = rtlpriv.cfg.ops.config_bb_with_pgheaderfile(rtlpriv, C.BASEBAND_CONFIG_PHY_REG)
    if not rtstatus:
        logger.error("BB_PG Reg Fail!!")
        return False
    rtstatus = rtlpriv.cfg.ops.config_bb_with_headerfile(rtlpriv, C.BASEBAND_CONFIG_AGC_TAB)
    if not rtstatus:
        logger.error("AGC Table Fail")
        return False
    rtlphy.cck_high_power = bool(rtl_get_bbreg(rtlpriv, C.RFPGA0_XA_HSSIPARAMETER2, 0x200))
    return True


# _rtl92c_store_pwrindex_diffrate_offset (phy_common.c:208): register -> origoffset column.
_PWRINDEX_COLUMN = {
    C.RTXAGC_A_RATE18_06: 0,
    C.RTXAGC_A_RATE54_24: 1,
    C.RTXAGC_A_CCK1_MCS32: 6,
    C.RTXAGC_A_MCS03_MCS00: 2,
    C.RTXAGC_A_MCS07_MCS04: 3,
    C.RTXAGC_A_MCS11_MCS08: 4,
    C.RTXAGC_A_MCS15_MCS12: 5,
    C.RTXAGC_B_RATE18_06: 8,
    C.RTXAGC_B_RATE54_24: 9,
    C.RTXAGC_B_CCK1_55_MCS32: 14,
    C.RTXAGC_B_MCS03_MCS00: 10,
    C.RTXAGC_B_MCS07_MCS04: 11,
    C.RTXAGC_B_MCS11_MCS08: 12,
    C.RTXAGC_B_MCS15_MCS12: 13,
}


def _rtl92c_store_pwrindex_diffrate_offset(rtlpriv: RtlPriv, regaddr: int, bitmask: int, data: int) -> None:
    """phy_common.c:208; RTXAGC_B_CCK11_A_CCK2_11 picks its column by mask."""
    rtlphy = rtlpriv.phy
    row = rtlphy.mcs_txpwrlevel_origoffset[rtlphy.pwrgroup_cnt]
    if regaddr in _PWRINDEX_COLUMN:
        row[_PWRINDEX_COLUMN[regaddr]] = data
    if regaddr == C.RTXAGC_B_CCK11_A_CCK2_11 and bitmask == 0xffffff00:
        row[7] = data
    if regaddr == C.RTXAGC_B_CCK11_A_CCK2_11 and bitmask == 0x000000ff:
        row[15] = data
    if regaddr == C.RTXAGC_B_MCS15_MCS12:
        rtlphy.pwrgroup_cnt += 1


def _rtl92c_phy_init_bb_rf_register_definition(rtlpriv: RtlPriv) -> None:
    """phy_common.c:395."""
    a, b, c, d = rtlpriv.phy.phyreg_def

    a.rfintfs = C.RFPGA0_XAB_RFINTERFACESW
    b.rfintfs = C.RFPGA0_XAB_RFINTERFACESW
    c.rfintfs = C.RFPGA0_XCD_RFINTERFACESW
    d.rfintfs = C.RFPGA0_XCD_RFINTERFACESW

    a.rfintfi = C.RFPGA0_XAB_RFINTERFACERB
    b.rfintfi = C.RFPGA0_XAB_RFINTERFACERB
    c.rfintfi = C.RFPGA0_XCD_RFINTERFACERB
    d.rfintfi = C.RFPGA0_XCD_RFINTERFACERB

    a.rfintfo = C.RFPGA0_XA_RFINTERFACEOE
    b.rfintfo = C.RFPGA0_XB_RFINTERFACEOE

    a.rfintfe = C.RFPGA0_XA_RFINTERFACEOE
    b.rfintfe = C.RFPGA0_XB_RFINTERFACEOE

    a.rf3wire_offset = C.RFPGA0_XA_LSSIPARAMETER
    b.rf3wire_offset = C.RFPGA0_XB_LSSIPARAMETER

    a.rflssi_select = C.RFPGA0_XAB_RFPARAMETER
    b.rflssi_select = C.RFPGA0_XAB_RFPARAMETER
    c.rflssi_select = C.RFPGA0_XCD_RFPARAMETER
    d.rflssi_select = C.RFPGA0_XCD_RFPARAMETER

    a.rftxgain_stage = C.RFPGA0_TXGAINSTAGE
    b.rftxgain_stage = C.RFPGA0_TXGAINSTAGE
    c.rftxgain_stage = C.RFPGA0_TXGAINSTAGE
    d.rftxgain_stage = C.RFPGA0_TXGAINSTAGE

    a.rfhssi_para1 = C.RFPGA0_XA_HSSIPARAMETER1
    b.rfhssi_para1 = C.RFPGA0_XB_HSSIPARAMETER1

    a.rfhssi_para2 = C.RFPGA0_XA_HSSIPARAMETER2
    b.rfhssi_para2 = C.RFPGA0_XB_HSSIPARAMETER2

    a.rfsw_ctrl = C.RFPGA0_XAB_SWITCHCONTROL
    b.rfsw_ctrl = C.RFPGA0_XAB_SWITCHCONTROL
    c.rfsw_ctrl = C.RFPGA0_XCD_SWITCHCONTROL
    d.rfsw_ctrl = C.RFPGA0_XCD_SWITCHCONTROL

    a.rfagc_control1 = C.ROFDM0_XAAGCCORE1
    b.rfagc_control1 = C.ROFDM0_XBAGCCORE1
    c.rfagc_control1 = C.ROFDM0_XCAGCCORE1
    d.rfagc_control1 = C.ROFDM0_XDAGCCORE1

    a.rfagc_control2 = C.ROFDM0_XAAGCCORE2
    b.rfagc_control2 = C.ROFDM0_XBAGCCORE2
    c.rfagc_control2 = C.ROFDM0_XCAGCCORE2
    d.rfagc_control2 = C.ROFDM0_XDAGCCORE2

    a.rfrxiq_imbal = C.ROFDM0_XARXIQIMBALANCE
    b.rfrxiq_imbal = C.ROFDM0_XBRXIQIMBALANCE
    c.rfrxiq_imbal = C.ROFDM0_XCRXIQIMBANLANCE
    d.rfrxiq_imbal = C.ROFDM0_XDRXIQIMBALANCE

    a.rfrx_afe = C.ROFDM0_XARXAFE
    b.rfrx_afe = C.ROFDM0_XBRXAFE
    c.rfrx_afe = C.ROFDM0_XCRXAFE
    d.rfrx_afe = C.ROFDM0_XDRXAFE

    a.rftxiq_imbal = C.ROFDM0_XATXIQIMBALANCE
    b.rftxiq_imbal = C.ROFDM0_XBTXIQIMBALANCE
    c.rftxiq_imbal = C.ROFDM0_XCTXIQIMBALANCE
    d.rftxiq_imbal = C.ROFDM0_XDTXIQIMBALANCE

    a.rftx_afe = C.ROFDM0_XATXAFE
    b.rftx_afe = C.ROFDM0_XBTXAFE
    c.rftx_afe = C.ROFDM0_XCTXAFE
    d.rftx_afe = C.ROFDM0_XDTXAFE

    a.rf_rb = C.RFPGA0_XA_LSSIREADBACK
    b.rf_rb = C.RFPGA0_XB_LSSIREADBACK
    c.rf_rb = C.RFPGA0_XC_LSSIREADBACK
    d.rf_rb = C.RFPGA0_XD_LSSIREADBACK

    a.rf_rbpi = C.TRANSCEIVEA_HSPI_READBACK
    b.rf_rbpi = C.TRANSCEIVEB_HSPI_READBACK


def rtl92c_phy_set_bw_mode(rtlpriv: RtlPriv, ch_type: int) -> None:
    """phy_common.c:652; ``ch_type`` is unused, the callback reads rtlphy->current_chan_bw."""
    rtlphy = rtlpriv.phy
    tmp_bw = rtlphy.current_chan_bw
    if rtlphy.set_bwmode_inprogress:
        return
    rtlphy.set_bwmode_inprogress = True
    if not is_hal_stop(rtlpriv.rtlhal):
        rtlpriv.cfg.ops.phy_set_bw_mode_callback(rtlpriv)
    else:
        logger.debug("false driver sleep or unload")
        rtlphy.set_bwmode_inprogress = False
        rtlphy.current_chan_bw = tmp_bw


def _rtl92c_phy_path_a_iqk(rtlpriv: RtlPriv, config_pathb: bool) -> int:
    """phy_common.c:895."""
    rtl_set_bbreg(rtlpriv, 0xe30, C.MASKDWORD, 0x10008c1f)
    rtl_set_bbreg(rtlpriv, 0xe34, C.MASKDWORD, 0x10008c1f)
    rtl_set_bbreg(rtlpriv, 0xe38, C.MASKDWORD, 0x82140102)
    rtl_set_bbreg(rtlpriv, 0xe3c, C.MASKDWORD, 0x28160202 if config_pathb else 0x28160502)
    if config_pathb:
        rtl_set_bbreg(rtlpriv, 0xe50, C.MASKDWORD, 0x10008c22)
        rtl_set_bbreg(rtlpriv, 0xe54, C.MASKDWORD, 0x10008c22)
        rtl_set_bbreg(rtlpriv, 0xe58, C.MASKDWORD, 0x82140102)
        rtl_set_bbreg(rtlpriv, 0xe5c, C.MASKDWORD, 0x28160202)
    rtl_set_bbreg(rtlpriv, 0xe4c, C.MASKDWORD, 0x001028d1)
    rtl_set_bbreg(rtlpriv, 0xe48, C.MASKDWORD, 0xf9000000)
    rtl_set_bbreg(rtlpriv, 0xe48, C.MASKDWORD, 0xf8000000)
    time.sleep(C.IQK_DELAY_TIME / 1000)

    reg_eac = rtl_get_bbreg(rtlpriv, 0xeac, C.MASKDWORD)
    reg_e94 = rtl_get_bbreg(rtlpriv, 0xe94, C.MASKDWORD)
    reg_e9c = rtl_get_bbreg(rtlpriv, 0xe9c, C.MASKDWORD)
    reg_ea4 = rtl_get_bbreg(rtlpriv, 0xea4, C.MASKDWORD)

    result = 0x00
    if (not (reg_eac & C.BIT(28)) and ((reg_e94 & 0x03FF0000) >> 16) != 0x142
            and ((reg_e9c & 0x03FF0000) >> 16) != 0x42):
        result |= 0x01
    else:
        return result
    if (not (reg_eac & C.BIT(27)) and ((reg_ea4 & 0x03FF0000) >> 16) != 0x132
            and ((reg_eac & 0x03FF0000) >> 16) != 0x36):
        result |= 0x02
    return result


def _rtl92c_phy_path_b_iqk(rtlpriv: RtlPriv) -> int:
    """phy_common.c:938."""
    rtl_set_bbreg(rtlpriv, 0xe60, C.MASKDWORD, 0x00000002)
    rtl_set_bbreg(rtlpriv, 0xe60, C.MASKDWORD, 0x00000000)
    time.sleep(C.IQK_DELAY_TIME / 1000)
    reg_eac = rtl_get_bbreg(rtlpriv, 0xeac, C.MASKDWORD)
    reg_eb4 = rtl_get_bbreg(rtlpriv, 0xeb4, C.MASKDWORD)
    reg_ebc = rtl_get_bbreg(rtlpriv, 0xebc, C.MASKDWORD)
    reg_ec4 = rtl_get_bbreg(rtlpriv, 0xec4, C.MASKDWORD)
    reg_ecc = rtl_get_bbreg(rtlpriv, 0xecc, C.MASKDWORD)

    result = 0x00
    if (not (reg_eac & C.BIT(31)) and ((reg_eb4 & 0x03FF0000) >> 16) != 0x142
            and ((reg_ebc & 0x03FF0000) >> 16) != 0x42):
        result |= 0x01
    else:
        return result
    if (not (reg_eac & C.BIT(30)) and ((reg_ec4 & 0x03FF0000) >> 16) != 0x132
            and ((reg_ecc & 0x03FF0000) >> 16) != 0x36):
        result |= 0x02
    return result


def _rtl92c_phy_path_a_fill_iqk_matrix(rtlpriv: RtlPriv, b_iqk_ok: bool, result: list[list[int]],
                                       final_candidate: int, btxonly: bool) -> None:
    """phy_common.c:965. ``x`` is u32 and ``y`` is long; only their low bits reach the registers."""
    if final_candidate == 0xFF:
        return
    if not b_iqk_ok:
        return
    oldval_0 = (rtl_get_bbreg(rtlpriv, C.ROFDM0_XATXIQIMBALANCE, C.MASKDWORD) >> 22) & 0x3FF
    x = result[final_candidate][0]
    if x & 0x00000200:
        x = x | 0xFFFFFC00
    tx0_a = ((x * oldval_0) & 0xFFFFFFFF) >> 8
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XATXIQIMBALANCE, 0x3FF, tx0_a)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_ECCATHRESHOLD, C.BIT(31), ((x * oldval_0) >> 7) & 0x1)
    y = result[final_candidate][1]
    if y & 0x00000200:
        y = y | 0xFFFFFC00
    tx0_c = (y * oldval_0) >> 8
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XCTXAFE, 0xF0000000, (tx0_c & 0x3C0) >> 6)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XATXIQIMBALANCE, 0x003F0000, tx0_c & 0x3F)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_ECCATHRESHOLD, C.BIT(29), ((y * oldval_0) >> 7) & 0x1)
    if btxonly:
        return
    reg = result[final_candidate][2]
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XARXIQIMBALANCE, 0x3FF, reg)
    reg = result[final_candidate][3] & 0x3F
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XARXIQIMBALANCE, 0xFC00, reg)
    reg = (result[final_candidate][3] >> 6) & 0xF
    rtl_set_bbreg(rtlpriv, 0xca0, 0xF0000000, reg)


def _rtl92c_phy_path_b_fill_iqk_matrix(rtlpriv: RtlPriv, b_iqk_ok: bool, result: list[list[int]],
                                       final_candidate: int, btxonly: bool) -> None:
    """phy_common.c:1005."""
    if final_candidate == 0xFF:
        return
    if not b_iqk_ok:
        return
    oldval_1 = (rtl_get_bbreg(rtlpriv, C.ROFDM0_XBTXIQIMBALANCE, C.MASKDWORD) >> 22) & 0x3FF
    x = result[final_candidate][4]
    if x & 0x00000200:
        x = x | 0xFFFFFC00
    tx1_a = ((x * oldval_1) & 0xFFFFFFFF) >> 8
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XBTXIQIMBALANCE, 0x3FF, tx1_a)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_ECCATHRESHOLD, C.BIT(27), ((x * oldval_1) >> 7) & 0x1)
    y = result[final_candidate][5]
    if y & 0x00000200:
        y = y | 0xFFFFFC00
    tx1_c = (y * oldval_1) >> 8
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XDTXAFE, 0xF0000000, (tx1_c & 0x3C0) >> 6)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XBTXIQIMBALANCE, 0x003F0000, tx1_c & 0x3F)
    rtl_set_bbreg(rtlpriv, C.ROFDM0_ECCATHRESHOLD, C.BIT(25), ((y * oldval_1) >> 7) & 0x1)
    if btxonly:
        return
    reg = result[final_candidate][6]
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XBRXIQIMBALANCE, 0x3FF, reg)
    reg = result[final_candidate][7] & 0x3F
    rtl_set_bbreg(rtlpriv, C.ROFDM0_XBRXIQIMBALANCE, 0xFC00, reg)
    reg = (result[final_candidate][7] >> 6) & 0xF
    rtl_set_bbreg(rtlpriv, C.ROFDM0_AGCRSSITABLE, 0x0000F000, reg)


def _rtl92c_phy_save_adda_registers(rtlpriv: RtlPriv, addareg: list[int], addabackup: list[int],
                                    registernum: int) -> None:
    for i in range(registernum):
        addabackup[i] = rtl_get_bbreg(rtlpriv, addareg[i], C.MASKDWORD)


def _rtl92c_phy_save_mac_registers(rtlpriv: RtlPriv, macreg: list[int], macbackup: list[int]) -> None:
    """phy_common.c:1055: bytes, then the last register as a dword."""
    io = rtlpriv.io
    for i in range(C.IQK_MAC_REG_NUM - 1):
        macbackup[i] = io.read8(macreg[i])
    last = C.IQK_MAC_REG_NUM - 1
    macbackup[last] = io.read32(macreg[last])


def _rtl92c_phy_reload_adda_registers(rtlpriv: RtlPriv, addareg: list[int], addabackup: list[int],
                                      regiesternum: int) -> None:
    for i in range(regiesternum):
        rtl_set_bbreg(rtlpriv, addareg[i], C.MASKDWORD, addabackup[i])


def _rtl92c_phy_reload_mac_registers(rtlpriv: RtlPriv, macreg: list[int], macbackup: list[int]) -> None:
    io = rtlpriv.io
    for i in range(C.IQK_MAC_REG_NUM - 1):
        io.write8(macreg[i], macbackup[i] & 0xFF)
    last = C.IQK_MAC_REG_NUM - 1
    io.write32(macreg[last], macbackup[last])


def _rtl92c_phy_path_adda_on(rtlpriv: RtlPriv, addareg: list[int], is_patha_on: bool, is2t: bool) -> None:
    """phy_common.c:1087."""
    pathon = 0x04db25a4 if is_patha_on else 0x0b1b25a4
    if not is2t:
        pathon = 0x0bdb25a0
        rtl_set_bbreg(rtlpriv, addareg[0], C.MASKDWORD, 0x0b1b25a0)
    else:
        rtl_set_bbreg(rtlpriv, addareg[0], C.MASKDWORD, pathon)
    for i in range(1, C.IQK_ADDA_REG_NUM):
        rtl_set_bbreg(rtlpriv, addareg[i], C.MASKDWORD, pathon)


def _rtl92c_phy_mac_setting_calibration(rtlpriv: RtlPriv, macreg: list[int], macbackup: list[int]) -> None:
    """phy_common.c:1105."""
    io = rtlpriv.io
    io.write8(macreg[0], 0x3F)
    for i in range(1, C.IQK_MAC_REG_NUM - 1):
        io.write8(macreg[i], macbackup[i] & ~C.BIT(3) & 0xFF)
    last = C.IQK_MAC_REG_NUM - 1
    io.write8(macreg[last], macbackup[last] & ~C.BIT(5) & 0xFF)


def _rtl92c_phy_path_a_standby(rtlpriv: RtlPriv) -> None:
    rtl_set_bbreg(rtlpriv, 0xe28, C.MASKDWORD, 0x0)
    rtl_set_bbreg(rtlpriv, 0x840, C.MASKDWORD, 0x00010000)
    rtl_set_bbreg(rtlpriv, 0xe28, C.MASKDWORD, 0x80800000)


def _rtl92c_phy_pi_mode_switch(rtlpriv: RtlPriv, pi_mode: bool) -> None:
    mode = 0x01000100 if pi_mode else 0x01000000
    rtl_set_bbreg(rtlpriv, 0x820, C.MASKDWORD, mode)
    rtl_set_bbreg(rtlpriv, 0x828, C.MASKDWORD, mode)


def _rtl92c_phy_simularity_compare(rtlpriv: RtlPriv, result: list[list[int]], c1: int, c2: int) -> bool:
    """phy_common.c:1135."""
    final_candidate = [0xFF, 0xFF]
    bresult = True
    is2t = IS_92C_SERIAL(rtlpriv.rtlhal.version)
    bound = 8 if is2t else 4
    simularity_bitmap = 0

    for i in range(bound):
        diff = abs(result[c1][i] - result[c2][i])
        if diff > C.MAX_TOLERANCE:
            if (i == 2 or i == 6) and not simularity_bitmap:
                if result[c1][i] + result[c1][i + 1] == 0:
                    final_candidate[i // 4] = c2
                elif result[c2][i] + result[c2][i + 1] == 0:
                    final_candidate[i // 4] = c1
                else:
                    simularity_bitmap |= 1 << i
            else:
                simularity_bitmap |= 1 << i

    if simularity_bitmap == 0:
        for i in range(bound // 4):
            if final_candidate[i] != 0xFF:
                for j in range(i * 4, (i + 1) * 4 - 2):
                    result[3][j] = result[final_candidate[i]][j]
                bresult = False
        return bresult
    if not (simularity_bitmap & 0x0F):
        for i in range(4):
            result[3][i] = result[c1][i]
        return False
    if not (simularity_bitmap & 0xF0) and is2t:
        for i in range(4, 8):
            result[3][i] = result[c1][i]
        return False
    return False


def _iqk_result(rtlpriv: RtlPriv, reg: int) -> int:
    return (rtl_get_bbreg(rtlpriv, reg, C.MASKDWORD) & 0x3FF0000) >> 16


def _rtl92c_phy_iq_calibrate(rtlpriv: RtlPriv, result: list[list[int]], t: int, is2t: bool) -> None:
    """phy_common.c:1194. The path-A retry loop re-reads 0xe9c on every pass that does not break."""
    rtlphy = rtlpriv.phy
    adda_reg = [0x85c, 0xe6c, 0xe70, 0xe74,
                0xe78, 0xe7c, 0xe80, 0xe84,
                0xe88, 0xe8c, 0xed0, 0xed4,
                0xed8, 0xedc, 0xee0, 0xeec]
    iqk_mac_reg = [0x522, 0x550, 0x551, 0x040]
    retrycount = 2

    if t == 0:
        rtl_get_bbreg(rtlpriv, 0x800, C.MASKDWORD)
        _rtl92c_phy_save_adda_registers(rtlpriv, adda_reg, rtlphy.adda_backup, 16)
        _rtl92c_phy_save_mac_registers(rtlpriv, iqk_mac_reg, rtlphy.iqk_mac_backup)
    _rtl92c_phy_path_adda_on(rtlpriv, adda_reg, True, is2t)
    if t == 0:
        rtlphy.rfpi_enable = rtl_get_bbreg(rtlpriv, C.RFPGA0_XA_HSSIPARAMETER1, C.BIT(8)) & 0xFF
    if not rtlphy.rfpi_enable:
        _rtl92c_phy_pi_mode_switch(rtlpriv, True)
    if t == 0:
        rtlphy.reg_c04 = rtl_get_bbreg(rtlpriv, 0xc04, C.MASKDWORD)
        rtlphy.reg_c08 = rtl_get_bbreg(rtlpriv, 0xc08, C.MASKDWORD)
        rtlphy.reg_874 = rtl_get_bbreg(rtlpriv, 0x874, C.MASKDWORD)
    rtl_set_bbreg(rtlpriv, 0xc04, C.MASKDWORD, 0x03a05600)
    rtl_set_bbreg(rtlpriv, 0xc08, C.MASKDWORD, 0x000800e4)
    rtl_set_bbreg(rtlpriv, 0x874, C.MASKDWORD, 0x22204000)
    if is2t:
        rtl_set_bbreg(rtlpriv, 0x840, C.MASKDWORD, 0x00010000)
        rtl_set_bbreg(rtlpriv, 0x844, C.MASKDWORD, 0x00010000)
    _rtl92c_phy_mac_setting_calibration(rtlpriv, iqk_mac_reg, rtlphy.iqk_mac_backup)
    rtl_set_bbreg(rtlpriv, 0xb68, C.MASKDWORD, 0x00080000)
    if is2t:
        rtl_set_bbreg(rtlpriv, 0xb6c, C.MASKDWORD, 0x00080000)
    rtl_set_bbreg(rtlpriv, 0xe28, C.MASKDWORD, 0x80800000)
    rtl_set_bbreg(rtlpriv, 0xe40, C.MASKDWORD, 0x01007c00)
    rtl_set_bbreg(rtlpriv, 0xe44, C.MASKDWORD, 0x01004800)
    for i in range(retrycount):
        patha_ok = _rtl92c_phy_path_a_iqk(rtlpriv, is2t)
        if patha_ok == 0x03:
            result[t][0] = _iqk_result(rtlpriv, 0xe94)
            result[t][1] = _iqk_result(rtlpriv, 0xe9c)
            result[t][2] = _iqk_result(rtlpriv, 0xea4)
            result[t][3] = _iqk_result(rtlpriv, 0xeac)
            break
        elif i == retrycount - 1 and patha_ok == 0x01:
            result[t][0] = _iqk_result(rtlpriv, 0xe94)
        result[t][1] = _iqk_result(rtlpriv, 0xe9c)

    if is2t:
        _rtl92c_phy_path_a_standby(rtlpriv)
        _rtl92c_phy_path_adda_on(rtlpriv, adda_reg, False, is2t)
        for i in range(retrycount):
            pathb_ok = _rtl92c_phy_path_b_iqk(rtlpriv)
            if pathb_ok == 0x03:
                result[t][4] = _iqk_result(rtlpriv, 0xeb4)
                result[t][5] = _iqk_result(rtlpriv, 0xebc)
                result[t][6] = _iqk_result(rtlpriv, 0xec4)
                result[t][7] = _iqk_result(rtlpriv, 0xecc)
                break
            elif i == retrycount - 1 and pathb_ok == 0x01:
                result[t][4] = _iqk_result(rtlpriv, 0xeb4)
            result[t][5] = _iqk_result(rtlpriv, 0xebc)
    rtl_set_bbreg(rtlpriv, 0xc04, C.MASKDWORD, rtlphy.reg_c04)
    rtl_set_bbreg(rtlpriv, 0x874, C.MASKDWORD, rtlphy.reg_874)
    rtl_set_bbreg(rtlpriv, 0xc08, C.MASKDWORD, rtlphy.reg_c08)
    rtl_set_bbreg(rtlpriv, 0xe28, C.MASKDWORD, 0)
    rtl_set_bbreg(rtlpriv, 0x840, C.MASKDWORD, 0x00032ed3)
    if is2t:
        rtl_set_bbreg(rtlpriv, 0x844, C.MASKDWORD, 0x00032ed3)
    if t != 0:
        if not rtlphy.rfpi_enable:
            _rtl92c_phy_pi_mode_switch(rtlpriv, False)
        _rtl92c_phy_reload_adda_registers(rtlpriv, adda_reg, rtlphy.adda_backup, 16)
        _rtl92c_phy_reload_mac_registers(rtlpriv, iqk_mac_reg, rtlphy.iqk_mac_backup)


def _rtl92c_phy_set_rfpath_switch(rtlpriv: RtlPriv, bmain: bool, is2t: bool) -> None:
    """phy_common.c:1323."""
    if is_hal_stop(rtlpriv.rtlhal):
        rtl_set_bbreg(rtlpriv, C.REG_LEDCFG0, C.BIT(23), 0x01)
        rtl_set_bbreg(rtlpriv, C.RFPGA0_XAB_RFPARAMETER, C.BIT(13), 0x01)
    if is2t:
        rtl_set_bbreg(rtlpriv, C.RFPGA0_XB_RFINTERFACEOE, C.BIT(5) | C.BIT(6), 0x1 if bmain else 0x2)
    else:
        rtl_set_bbreg(rtlpriv, C.RFPGA0_XA_RFINTERFACEOE, 0x300, 0x2 if bmain else 0x1)


IQK_BB_REG = [
    C.ROFDM0_XARXIQIMBALANCE,
    C.ROFDM0_XBRXIQIMBALANCE,
    C.ROFDM0_ECCATHRESHOLD,
    C.ROFDM0_AGCRSSITABLE,
    C.ROFDM0_XATXIQIMBALANCE,
    C.ROFDM0_XBTXIQIMBALANCE,
    C.ROFDM0_XCTXIQIMBALANCE,
    C.ROFDM0_XCTXAFE,
    C.ROFDM0_XDTXAFE,
    C.ROFDM0_RXIQEXTANTA,
]


def rtl92c_phy_iq_calibrate(rtlpriv: RtlPriv, b_recovery: bool) -> None:
    """phy_common.c:1350."""
    rtlphy = rtlpriv.phy
    is_92c_serial = IS_92C_SERIAL(rtlpriv.rtlhal.version)
    if b_recovery:
        _rtl92c_phy_reload_adda_registers(rtlpriv, IQK_BB_REG, rtlphy.iqk_bb_backup, 10)
        return
    result = [[0] * 8 for _ in range(4)]
    final_candidate = 0xff
    b_patha_ok = False
    b_pathb_ok = False
    for i in range(3):
        _rtl92c_phy_iq_calibrate(rtlpriv, result, i, is_92c_serial)
        if i == 1:
            if _rtl92c_phy_simularity_compare(rtlpriv, result, 0, 1):
                final_candidate = 0
                break
        if i == 2:
            if _rtl92c_phy_simularity_compare(rtlpriv, result, 0, 2):
                final_candidate = 0
                break
            if _rtl92c_phy_simularity_compare(rtlpriv, result, 1, 2):
                final_candidate = 1
            else:
                reg_tmp = sum(result[3])
                final_candidate = 3 if reg_tmp != 0 else 0xFF

    reg_e94 = result[3][0]
    reg_ea4 = result[3][2]
    reg_eb4 = result[3][4]
    reg_ec4 = result[3][6]
    if final_candidate != 0xff:
        rtlphy.reg_e94 = reg_e94 = result[final_candidate][0]
        rtlphy.reg_e9c = result[final_candidate][1]
        reg_ea4 = result[final_candidate][2]
        rtlphy.reg_eb4 = reg_eb4 = result[final_candidate][4]
        rtlphy.reg_ebc = result[final_candidate][5]
        reg_ec4 = result[final_candidate][6]
        b_patha_ok = True
        b_pathb_ok = True
    else:
        rtlphy.reg_e94 = rtlphy.reg_eb4 = 0x100
        rtlphy.reg_e9c = rtlphy.reg_ebc = 0x0
    if reg_e94 != 0:
        _rtl92c_phy_path_a_fill_iqk_matrix(rtlpriv, b_patha_ok, result, final_candidate, reg_ea4 == 0)
    if is_92c_serial:
        if reg_eb4 != 0:
            _rtl92c_phy_path_b_fill_iqk_matrix(rtlpriv, b_pathb_ok, result, final_candidate, reg_ec4 == 0)
    _rtl92c_phy_save_adda_registers(rtlpriv, IQK_BB_REG, rtlphy.iqk_bb_backup, 10)


def _rtl92c_phy_ap_calibrate(rtlpriv: RtlPriv, delta: int, is2t: bool) -> None:
    """phy_common.c:1318: empty in the source."""


def rtl92c_phy_ap_calibrate(rtlpriv: RtlPriv, delta: int) -> None:
    _rtl92c_phy_ap_calibrate(rtlpriv, delta, IS_92C_SERIAL(rtlpriv.rtlhal.version))


def rtl92c_phy_lc_calibrate(rtlpriv: RtlPriv) -> None:
    rtlpriv.cfg.ops.phy_lc_calibrate(rtlpriv, IS_92C_SERIAL(rtlpriv.rtlhal.version))


def rtl92c_phy_set_rfpath_switch(rtlpriv: RtlPriv, bmain: bool) -> None:
    _rtl92c_phy_set_rfpath_switch(rtlpriv, bmain, IS_92C_SERIAL(rtlpriv.rtlhal.version))
