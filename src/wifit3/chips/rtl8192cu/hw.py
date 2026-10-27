from __future__ import annotations

from . import constants as C
from .efuse import rtl_get_hwinfo
from .mac import IS_NORMAL_CHIP
from .priv import RtlPriv


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


def rtl92c_get_chnl_group(chnl: int) -> int:
    """rtl8192ce/hw.h:7."""
    if chnl < 3:
        return 0
    if chnl < 9:
        return 1
    return 2


def IS_HIGHT_PA(boardtype: int) -> bool:
    return boardtype == C.BOARD_USB_HIGH_PA


def _rtl92cu_read_txpower_info_from_hwpg(rtlpriv: RtlPriv, autoload_fail: bool, hwinfo: bytearray) -> None:
    """rtl8192cu/hw.c:98."""
    rtlefuse = rtlpriv.efuse
    for rf_path in range(2):
        for i in range(3):
            if not autoload_fail:
                rtlefuse.eeprom_chnlarea_txpwr_cck[rf_path][i] = hwinfo[C.EEPROM_TXPOWERCCK + rf_path * 3 + i]
                rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][i] = \
                    hwinfo[C.EEPROM_TXPOWERHT40_1S + rf_path * 3 + i]
            else:
                rtlefuse.eeprom_chnlarea_txpwr_cck[rf_path][i] = C.EEPROM_DEFAULT_TXPOWERLEVEL
                rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][i] = C.EEPROM_DEFAULT_TXPOWERLEVEL
    for i in range(3):
        tempval = hwinfo[C.EEPROM_TXPOWERHT40_2SDIFF + i] if not autoload_fail else C.EEPROM_DEFAULT_HT40_2SDIFF
        rtlefuse.eprom_chnl_txpwr_ht40_2sdf[C.RF90_PATH_A][i] = tempval & 0xF
        rtlefuse.eprom_chnl_txpwr_ht40_2sdf[C.RF90_PATH_B][i] = (tempval & 0xF0) >> 4
    for rf_path in range(2):
        for i in range(14):
            index = rtl92c_get_chnl_group(i)
            rtlefuse.txpwrlevel_cck[rf_path][i] = rtlefuse.eeprom_chnlarea_txpwr_cck[rf_path][index]
            rtlefuse.txpwrlevel_ht40_1s[rf_path][i] = rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][index]
            ht40_2s = (rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][index]
                       - rtlefuse.eprom_chnl_txpwr_ht40_2sdf[rf_path][index])
            rtlefuse.txpwrlevel_ht40_2s[rf_path][i] = ht40_2s if ht40_2s > 0 else 0
    for i in range(3):
        if not autoload_fail:
            rtlefuse.eeprom_pwrlimit_ht40[i] = hwinfo[C.EEPROM_TXPWR_GROUP + i]
            rtlefuse.eeprom_pwrlimit_ht20[i] = hwinfo[C.EEPROM_TXPWR_GROUP + 3 + i]
        else:
            rtlefuse.eeprom_pwrlimit_ht40[i] = 0
            rtlefuse.eeprom_pwrlimit_ht20[i] = 0
    for rf_path in range(2):
        for i in range(14):
            index = rtl92c_get_chnl_group(i)
            if rf_path == C.RF90_PATH_A:
                rtlefuse.pwrgroup_ht20[rf_path][i] = rtlefuse.eeprom_pwrlimit_ht20[index] & 0xF
                rtlefuse.pwrgroup_ht40[rf_path][i] = rtlefuse.eeprom_pwrlimit_ht40[index] & 0xF
            elif rf_path == C.RF90_PATH_B:
                rtlefuse.pwrgroup_ht20[rf_path][i] = (rtlefuse.eeprom_pwrlimit_ht20[index] & 0xF0) >> 4
                rtlefuse.pwrgroup_ht40[rf_path][i] = (rtlefuse.eeprom_pwrlimit_ht40[index] & 0xF0) >> 4
    for i in range(14):
        index = rtl92c_get_chnl_group(i)
        tempval = hwinfo[C.EEPROM_TXPOWERHT20DIFF + index] if not autoload_fail else C.EEPROM_DEFAULT_HT20_DIFF
        ht20diff_a = tempval & 0xF
        ht20diff_b = (tempval >> 4) & 0xF
        if ht20diff_a & C.BIT(3):
            ht20diff_a |= 0xF0
        if ht20diff_b & C.BIT(3):
            ht20diff_b |= 0xF0
        rtlefuse.txpwr_ht20diff[C.RF90_PATH_A][i] = _s8(ht20diff_a)
        rtlefuse.txpwr_ht20diff[C.RF90_PATH_B][i] = _s8(ht20diff_b)
        tempval = (hwinfo[C.EEPROM_TXPOWER_OFDMDIFF + index] if not autoload_fail
                   else C.EEPROM_DEFAULT_LEGACYHTTXPOWERDIFF)
        rtlefuse.txpwr_legacyhtdiff[C.RF90_PATH_A][i] = tempval & 0xF
        rtlefuse.txpwr_legacyhtdiff[C.RF90_PATH_B][i] = (tempval >> 4) & 0xF
    rtlefuse.legacy_ht_txpowerdiff = rtlefuse.txpwr_legacyhtdiff[C.RF90_PATH_A][7] & 0xFF
    rtlefuse.eeprom_regulatory = hwinfo[C.RF_OPTION1] & 0x7 if not autoload_fail else 0
    if not autoload_fail:
        rtlefuse.eeprom_tssi[C.RF90_PATH_A] = hwinfo[C.EEPROM_TSSI_A]
        rtlefuse.eeprom_tssi[C.RF90_PATH_B] = hwinfo[C.EEPROM_TSSI_B]
    else:
        rtlefuse.eeprom_tssi[C.RF90_PATH_A] = C.EEPROM_DEFAULT_TSSI
        rtlefuse.eeprom_tssi[C.RF90_PATH_B] = C.EEPROM_DEFAULT_TSSI
    tempval = hwinfo[C.EEPROM_THERMAL_METER] if not autoload_fail else C.EEPROM_DEFAULT_THERMALMETER
    rtlefuse.eeprom_thermalmeter = tempval & 0x1F
    if rtlefuse.eeprom_thermalmeter < 0x06 or rtlefuse.eeprom_thermalmeter > 0x1C:
        rtlefuse.eeprom_thermalmeter = 0x12
    if rtlefuse.eeprom_thermalmeter == 0x1F or autoload_fail:
        rtlefuse.apk_thermalmeterignore = True
    rtlefuse.thermalmeter[0] = rtlefuse.eeprom_thermalmeter


def _rtl92cu_read_board_type(rtlpriv: RtlPriv, contents: bytearray) -> None:
    """rtl8192cu/hw.c:300."""
    rtlefuse = rtlpriv.efuse
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        boardtype = (contents[C.EEPROM_RF_OPT1] & C.BOARD_TYPE_NORMAL_MASK) >> 5
    else:
        boardtype = contents[C.EEPROM_RF_OPT4] & C.BOARD_TYPE_TEST_MASK
    rtlefuse.board_type = boardtype
    if IS_HIGHT_PA(rtlefuse.board_type):
        rtlefuse.external_pa = 1


def _rtl92cu_read_adapter_info(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/hw.c:319."""
    rtlefuse = rtlpriv.efuse
    rtlhal = rtlpriv.rtlhal
    params = [C.RTL8190_EEPROM_ID, C.EEPROM_VID, C.EEPROM_DID, C.EEPROM_SVID, C.EEPROM_SMID,
              C.EEPROM_MAC_ADDR, C.EEPROM_CHANNELPLAN, C.EEPROM_VERSION, C.EEPROM_CUSTOMER_ID, 0]
    hwinfo = bytearray(C.HWSET_MAX_SIZE)

    if rtl_get_hwinfo(rtlpriv, C.HWSET_MAX_SIZE, hwinfo, params):
        return

    _rtl92cu_read_txpower_info_from_hwpg(rtlpriv, rtlefuse.autoload_failflag, hwinfo)
    _rtl92cu_read_board_type(rtlpriv, hwinfo)

    rtlefuse.txpwr_fromeprom = True
    if rtlhal.oem_id == C.RT_CID_DEFAULT:
        if rtlefuse.eeprom_oemid == C.EEPROM_CID_DEFAULT:
            if (rtlefuse.eeprom_did == 0x8176 and rtlefuse.eeprom_svid == 0x103C
                    and rtlefuse.eeprom_smid == 0x1629):
                rtlhal.oem_id = C.RT_CID_819X_HP
            else:
                rtlhal.oem_id = C.RT_CID_DEFAULT
        elif rtlefuse.eeprom_oemid == C.EEPROM_CID_TOSHIBA:
            rtlhal.oem_id = C.RT_CID_TOSHIBA
        elif rtlefuse.eeprom_oemid == C.EEPROM_CID_QMI:
            rtlhal.oem_id = C.RT_CID_819X_QMI
        else:
            rtlhal.oem_id = C.RT_CID_DEFAULT


def _rtl92cu_hal_customized_behavior(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/hw.c:371."""
    if rtlpriv.rtlhal.oem_id == C.RT_CID_819X_HP:
        rtlpriv.ledctl.led_opendrain = True


def rtl92cu_read_eeprom_info(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/hw.c:393. A test chip skips the EFUSE read entirely."""
    rtlefuse = rtlpriv.efuse
    if not IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        return
    tmp_u1b = rtlpriv.io.read8(C.REG_9346CR)
    rtlefuse.epromtype = C.EEPROM_93C46 if tmp_u1b & C.BOOT_FROM_EEPROM else C.EEPROM_BOOT_EFUSE
    rtlefuse.autoload_failflag = not (tmp_u1b & C.EEPROM_EN)
    _rtl92cu_read_adapter_info(rtlpriv)
    _rtl92cu_hal_customized_behavior(rtlpriv)
