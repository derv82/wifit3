"""rtlwifi rtl8192cu/rf.c: RF6052 bring-up, bandwidth, and CCK/OFDM TX power."""
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


def _clamp_tx_agc_bytes(value: int) -> int:
    """rf.c:91-97: clamp each byte of a TX AGC word to RF6052_MAX_TX_PWR."""
    out = 0
    for idx2 in range(4):
        byte = (value >> (idx2 * 8)) & 0xFF
        out |= min(byte, C.RF6052_MAX_TX_PWR) << (idx2 * 8)
    return out


def _replicate_byte(level: int) -> int:
    return (level | (level << 8) | (level << 16) | (level << 24)) & 0xFFFFFFFF


def rtl92cu_phy_rf6052_set_cck_txpower(rtlpriv: RtlPriv, ppowerlevel: list[int]) -> None:
    """rf.c:36. rtlphy->mcs_offset is never filled for rtl8192cu, so its terms add 0."""
    rtlphy = rtlpriv.phy
    mac = rtlpriv.mac80211
    rtlefuse = rtlpriv.efuse
    tx_agc = [0, 0]
    if mac.act_scanning:
        for idx1 in (C.RF90_PATH_A, C.RF90_PATH_B):
            tx_agc[idx1] = _replicate_byte(ppowerlevel[idx1])
            if tx_agc[idx1] > 0x20 and rtlefuse.external_pa:
                tx_agc[idx1] = 0x20
    elif rtlpriv.dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_LEVEL1:
        tx_agc = [0x10101010, 0x10101010]
    elif rtlpriv.dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_LEVEL2:
        tx_agc = [0x00000000, 0x00000000]
    else:
        for idx1 in (C.RF90_PATH_A, C.RF90_PATH_B):
            tx_agc[idx1] = _replicate_byte(ppowerlevel[idx1])
        if rtlefuse.eeprom_regulatory == 0:
            tmpval = rtlphy.mcs_offset[0][6] + (rtlphy.mcs_offset[0][7] << 8)
            tx_agc[C.RF90_PATH_A] = (tx_agc[C.RF90_PATH_A] + tmpval) & 0xFFFFFFFF
            tmpval = rtlphy.mcs_offset[0][14] + (rtlphy.mcs_offset[0][15] << 24)
            tx_agc[C.RF90_PATH_B] = (tx_agc[C.RF90_PATH_B] + tmpval) & 0xFFFFFFFF
    for idx1 in (C.RF90_PATH_A, C.RF90_PATH_B):
        tx_agc[idx1] = _clamp_tx_agc_bytes(tx_agc[idx1])
    tmpval = tx_agc[C.RF90_PATH_A] & 0xff
    rtl_set_bbreg(rtlpriv, C.RTXAGC_A_CCK1_MCS32, C.MASKBYTE1, tmpval)
    tmpval = tx_agc[C.RF90_PATH_A] >> 8
    if mac.mode == C.WIRELESS_MODE_B:
        tmpval = tmpval & 0xff00ffff
    rtl_set_bbreg(rtlpriv, C.RTXAGC_B_CCK11_A_CCK2_11, 0xffffff00, tmpval)
    tmpval = tx_agc[C.RF90_PATH_B] >> 24
    rtl_set_bbreg(rtlpriv, C.RTXAGC_B_CCK11_A_CCK2_11, C.MASKBYTE0, tmpval)
    tmpval = tx_agc[C.RF90_PATH_B] & 0x00ffffff
    rtl_set_bbreg(rtlpriv, C.RTXAGC_B_CCK1_55_MCS32, 0xffffff00, tmpval)


def rtl92c_phy_get_power_base(rtlpriv: RtlPriv, ppowerlevel: list[int], channel: int,
                              ofdmbase: list[int], mcsbase: list[int]) -> None:
    """rf.c:126. The s8 diffs land in u8 locals; powerlevel[] is u8, so the HT20 add wraps."""
    rtlefuse = rtlpriv.efuse
    powerlevel = [0, 0]
    for i in range(2):
        powerlevel[i] = ppowerlevel[i]
        legacy_pwrdiff = rtlefuse.txpwr_legacyhtdiff[i][channel - 1] & 0xFF
        powerbase0 = powerlevel[i] + legacy_pwrdiff
        ofdmbase[i] = ((powerbase0 << 24) | (powerbase0 << 16) | (powerbase0 << 8) | powerbase0) & 0xFFFFFFFF
    for i in range(2):
        if rtlpriv.phy.current_chan_bw == C.HT_CHANNEL_WIDTH_20:
            ht20_pwrdiff = rtlefuse.txpwr_ht20diff[i][channel - 1] & 0xFF
            powerlevel[i] = (powerlevel[i] + ht20_pwrdiff) & 0xFF
        mcsbase[i] = _replicate_byte(powerlevel[i])


def _rtl92c_get_txpower_writeval_by_regulatory(rtlpriv: RtlPriv, channel: int, index: int, powerbase0: list[int],
                                               powerbase1: list[int], p_outwriteval: list[int]) -> None:
    """rf.c:162."""
    rtlphy = rtlpriv.phy
    rtlefuse = rtlpriv.efuse
    chnlgroup = 0
    for rf in range(2):
        base = powerbase0[rf] if index < 2 else powerbase1[rf]
        column = index + (8 if rf else 0)
        if rtlefuse.eeprom_regulatory == 0:
            chnlgroup = 0
            writeval = rtlphy.mcs_offset[chnlgroup][column] + base
        elif rtlefuse.eeprom_regulatory == 1:
            if rtlphy.pwrgroup_cnt == 1:
                chnlgroup = 0
            if rtlphy.pwrgroup_cnt >= 3:
                if channel <= 3:
                    chnlgroup = 0
                elif 4 <= channel <= 9:
                    chnlgroup = 1
                elif channel > 9:
                    chnlgroup = 2
                if rtlphy.current_chan_bw == C.HT_CHANNEL_WIDTH_20:
                    chnlgroup += 1
                else:
                    chnlgroup += 4
            writeval = rtlphy.mcs_offset[chnlgroup][column] + base
        elif rtlefuse.eeprom_regulatory == 2:
            writeval = base
        elif rtlefuse.eeprom_regulatory == 3:
            chnlgroup = 0
            if rtlphy.current_chan_bw == C.HT_CHANNEL_WIDTH_20_40:
                limit = rtlefuse.pwrgroup_ht40[rf][channel - 1]
            else:
                limit = rtlefuse.pwrgroup_ht20[rf][channel - 1]
            pwr_diff_limit = [0, 0, 0, 0]
            for i in range(4):
                pwr_diff_limit[i] = (rtlphy.mcs_offset[chnlgroup][column] & (0x7f << (i * 8))) >> (i * 8)
                if pwr_diff_limit[i] > limit:
                    pwr_diff_limit[i] = limit
            customer_limit = ((pwr_diff_limit[3] << 24) | (pwr_diff_limit[2] << 16) | (pwr_diff_limit[1] << 8)
                              | pwr_diff_limit[0])
            writeval = customer_limit + base
        else:
            chnlgroup = 0
            writeval = rtlphy.mcs_offset[chnlgroup][column] + base
        if rtlpriv.dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_LEVEL1:
            writeval = 0x14141414
        elif rtlpriv.dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_LEVEL2:
            writeval = 0x00000000
        if rtlpriv.dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_BT1:
            writeval = writeval - 0x06060606
        p_outwriteval[rf] = writeval & 0xFFFFFFFF


def _rtl92c_write_ofdm_power_reg(rtlpriv: RtlPriv, index: int, value: list[int]) -> None:
    """rf.c:293."""
    regoffset_a = (C.RTXAGC_A_RATE18_06, C.RTXAGC_A_RATE54_24, C.RTXAGC_A_MCS03_MCS00, C.RTXAGC_A_MCS07_MCS04,
                   C.RTXAGC_A_MCS11_MCS08, C.RTXAGC_A_MCS15_MCS12)
    regoffset_b = (C.RTXAGC_B_RATE18_06, C.RTXAGC_B_RATE54_24, C.RTXAGC_B_MCS03_MCS00, C.RTXAGC_B_MCS07_MCS04,
                   C.RTXAGC_B_MCS11_MCS08, C.RTXAGC_B_MCS15_MCS12)
    is_2t2r = rtlpriv.phy.rf_type == C.RF_2T2R
    pwr_val = [0, 0, 0, 0]
    for rf in range(2):
        writeval = value[rf]
        for i in range(4):
            pwr_val[i] = (writeval & (0x7f << (i * 8))) >> (i * 8)
            if pwr_val[i] > C.RF6052_MAX_TX_PWR:
                pwr_val[i] = C.RF6052_MAX_TX_PWR
        writeval = (pwr_val[3] << 24) | (pwr_val[2] << 16) | (pwr_val[1] << 8) | pwr_val[0]
        regoffset = regoffset_a[index] if rf == 0 else regoffset_b[index]
        rtl_set_bbreg(rtlpriv, regoffset, C.MASKDWORD, writeval)
        if ((is_2t2r and regoffset in (C.RTXAGC_A_MCS15_MCS12, C.RTXAGC_B_MCS15_MCS12))
                or (not is_2t2r and regoffset in (C.RTXAGC_A_MCS07_MCS04, C.RTXAGC_B_MCS07_MCS04))):
            writeval = pwr_val[3]
            if regoffset in (C.RTXAGC_A_MCS15_MCS12, C.RTXAGC_A_MCS07_MCS04):
                regoffset = 0xc90
            if regoffset in (C.RTXAGC_B_MCS15_MCS12, C.RTXAGC_B_MCS07_MCS04):
                regoffset = 0xc98
            for i in range(3):
                if i != 2:
                    writeval = writeval - 8 if writeval > 8 else 0
                else:
                    writeval = writeval - 6 if writeval > 6 else 0
                rtlpriv.io.write8(regoffset + i, writeval)


def rtl92cu_phy_rf6052_set_ofdm_txpower(rtlpriv: RtlPriv, ppowerlevel: list[int], channel: int) -> None:
    """rf.c:345."""
    writeval = [0, 0]
    powerbase0 = [0, 0]
    powerbase1 = [0, 0]
    rtl92c_phy_get_power_base(rtlpriv, ppowerlevel, channel, powerbase0, powerbase1)
    for index in range(6):
        _rtl92c_get_txpower_writeval_by_regulatory(rtlpriv, channel, index, powerbase0, powerbase1, writeval)
        _rtl92c_write_ofdm_power_reg(rtlpriv, index, writeval)


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
