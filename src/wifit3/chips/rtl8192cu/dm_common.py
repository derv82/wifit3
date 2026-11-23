"""rtlwifi rtl8192c/dm_common.c: dynamic-mechanism init and the rtl92c_dm_watchdog tick."""
from __future__ import annotations

from . import constants as C
from .core import rtl_dm_diginit
from .mac import IS_92C_SERIAL
from .phy_common import (
    rtl92c_phy_ap_calibrate,
    rtl92c_phy_iq_calibrate,
    rtl92c_phy_lc_calibrate,
    rtl92c_phy_set_txpower_level,
)
from .priv import RtlPriv
from .wifi import rtl_get_bbreg, rtl_get_rfreg, rtl_set_bbreg, rtl_set_rfreg

# dm_common.c:25-135
ofdmswing_table = (
    0x7f8001fe, 0x788001e2, 0x71c001c7, 0x6b8001ae, 0x65400195, 0x5fc0017f,
    0x5a400169, 0x55400155, 0x50800142, 0x4c000130, 0x47c0011f, 0x43c0010f,
    0x40000100, 0x3c8000f2, 0x390000e4, 0x35c000d7, 0x32c000cb, 0x300000c0,
    0x2d4000b5, 0x2ac000ab, 0x288000a2, 0x26000098, 0x24000090, 0x22000088,
    0x20000080, 0x1e400079, 0x1c800072, 0x1b00006c, 0x19800066, 0x18000060,
    0x16c0005b, 0x15800056, 0x14400051, 0x1300004c, 0x12000048, 0x11000044,
    0x10000040,
)

cckswing_table_ch1ch13 = (
    (0x36, 0x35, 0x2e, 0x25, 0x1c, 0x12, 0x09, 0x04),
    (0x33, 0x32, 0x2b, 0x23, 0x1a, 0x11, 0x08, 0x04),
    (0x30, 0x2f, 0x29, 0x21, 0x19, 0x10, 0x08, 0x03),
    (0x2d, 0x2d, 0x27, 0x1f, 0x18, 0x0f, 0x08, 0x03),
    (0x2b, 0x2a, 0x25, 0x1e, 0x16, 0x0e, 0x07, 0x03),
    (0x28, 0x28, 0x22, 0x1c, 0x15, 0x0d, 0x07, 0x03),
    (0x26, 0x25, 0x21, 0x1b, 0x14, 0x0d, 0x06, 0x03),
    (0x24, 0x23, 0x1f, 0x19, 0x13, 0x0c, 0x06, 0x03),
    (0x22, 0x21, 0x1d, 0x18, 0x11, 0x0b, 0x06, 0x02),
    (0x20, 0x20, 0x1b, 0x16, 0x11, 0x08, 0x05, 0x02),
    (0x1f, 0x1e, 0x1a, 0x15, 0x10, 0x0a, 0x05, 0x02),
    (0x1d, 0x1c, 0x18, 0x14, 0x0f, 0x0a, 0x05, 0x02),
    (0x1b, 0x1a, 0x17, 0x13, 0x0e, 0x09, 0x04, 0x02),
    (0x1a, 0x19, 0x16, 0x12, 0x0d, 0x09, 0x04, 0x02),
    (0x18, 0x17, 0x15, 0x11, 0x0c, 0x08, 0x04, 0x02),
    (0x17, 0x16, 0x13, 0x10, 0x0c, 0x08, 0x04, 0x02),
    (0x16, 0x15, 0x12, 0x0f, 0x0b, 0x07, 0x04, 0x01),
    (0x14, 0x14, 0x11, 0x0e, 0x0b, 0x07, 0x03, 0x02),
    (0x13, 0x13, 0x10, 0x0d, 0x0a, 0x06, 0x03, 0x01),
    (0x12, 0x12, 0x0f, 0x0c, 0x09, 0x06, 0x03, 0x01),
    (0x11, 0x11, 0x0f, 0x0c, 0x09, 0x06, 0x03, 0x01),
    (0x10, 0x10, 0x0e, 0x0b, 0x08, 0x05, 0x03, 0x01),
    (0x0f, 0x0f, 0x0d, 0x0b, 0x08, 0x05, 0x03, 0x01),
    (0x0e, 0x0e, 0x0c, 0x0a, 0x08, 0x05, 0x02, 0x01),
    (0x0d, 0x0d, 0x0c, 0x0a, 0x07, 0x05, 0x02, 0x01),
    (0x0d, 0x0c, 0x0b, 0x09, 0x07, 0x04, 0x02, 0x01),
    (0x0c, 0x0c, 0x0a, 0x09, 0x06, 0x04, 0x02, 0x01),
    (0x0b, 0x0b, 0x0a, 0x08, 0x06, 0x04, 0x02, 0x01),
    (0x0b, 0x0a, 0x09, 0x08, 0x06, 0x04, 0x02, 0x01),
    (0x0a, 0x0a, 0x09, 0x07, 0x05, 0x03, 0x02, 0x01),
    (0x0a, 0x09, 0x08, 0x07, 0x05, 0x03, 0x02, 0x01),
    (0x09, 0x09, 0x08, 0x06, 0x05, 0x03, 0x01, 0x01),
    (0x09, 0x08, 0x07, 0x06, 0x04, 0x03, 0x01, 0x01),
)

cckswing_table_ch14 = (
    (0x36, 0x35, 0x2e, 0x1b, 0x00, 0x00, 0x00, 0x00),
    (0x33, 0x32, 0x2b, 0x19, 0x00, 0x00, 0x00, 0x00),
    (0x30, 0x2f, 0x29, 0x18, 0x00, 0x00, 0x00, 0x00),
    (0x2d, 0x2d, 0x17, 0x17, 0x00, 0x00, 0x00, 0x00),
    (0x2b, 0x2a, 0x25, 0x15, 0x00, 0x00, 0x00, 0x00),
    (0x28, 0x28, 0x24, 0x14, 0x00, 0x00, 0x00, 0x00),
    (0x26, 0x25, 0x21, 0x13, 0x00, 0x00, 0x00, 0x00),
    (0x24, 0x23, 0x1f, 0x12, 0x00, 0x00, 0x00, 0x00),
    (0x22, 0x21, 0x1d, 0x11, 0x00, 0x00, 0x00, 0x00),
    (0x20, 0x20, 0x1b, 0x10, 0x00, 0x00, 0x00, 0x00),
    (0x1f, 0x1e, 0x1a, 0x0f, 0x00, 0x00, 0x00, 0x00),
    (0x1d, 0x1c, 0x18, 0x0e, 0x00, 0x00, 0x00, 0x00),
    (0x1b, 0x1a, 0x17, 0x0e, 0x00, 0x00, 0x00, 0x00),
    (0x1a, 0x19, 0x16, 0x0d, 0x00, 0x00, 0x00, 0x00),
    (0x18, 0x17, 0x15, 0x0c, 0x00, 0x00, 0x00, 0x00),
    (0x17, 0x16, 0x13, 0x0b, 0x00, 0x00, 0x00, 0x00),
    (0x16, 0x15, 0x12, 0x0b, 0x00, 0x00, 0x00, 0x00),
    (0x14, 0x14, 0x11, 0x0a, 0x00, 0x00, 0x00, 0x00),
    (0x13, 0x13, 0x10, 0x0a, 0x00, 0x00, 0x00, 0x00),
    (0x12, 0x12, 0x0f, 0x09, 0x00, 0x00, 0x00, 0x00),
    (0x11, 0x11, 0x0f, 0x09, 0x00, 0x00, 0x00, 0x00),
    (0x10, 0x10, 0x0e, 0x08, 0x00, 0x00, 0x00, 0x00),
    (0x0f, 0x0f, 0x0d, 0x08, 0x00, 0x00, 0x00, 0x00),
    (0x0e, 0x0e, 0x0c, 0x07, 0x00, 0x00, 0x00, 0x00),
    (0x0d, 0x0d, 0x0c, 0x07, 0x00, 0x00, 0x00, 0x00),
    (0x0d, 0x0c, 0x0b, 0x06, 0x00, 0x00, 0x00, 0x00),
    (0x0c, 0x0c, 0x0a, 0x06, 0x00, 0x00, 0x00, 0x00),
    (0x0b, 0x0b, 0x0a, 0x06, 0x00, 0x00, 0x00, 0x00),
    (0x0b, 0x0a, 0x09, 0x05, 0x00, 0x00, 0x00, 0x00),
    (0x0a, 0x0a, 0x09, 0x05, 0x00, 0x00, 0x00, 0x00),
    (0x0a, 0x09, 0x08, 0x05, 0x00, 0x00, 0x00, 0x00),
    (0x09, 0x09, 0x08, 0x05, 0x00, 0x00, 0x00, 0x00),
    (0x09, 0x08, 0x07, 0x04, 0x00, 0x00, 0x00, 0x00),
)

power_index_reg = [0xc90, 0xc91, 0xc92, 0xc98, 0xc99, 0xc9a]


def dm_savepowerindex(rtlpriv: RtlPriv) -> None:
    """dm_common.c:160."""
    for index in range(6):
        rtlpriv.dm.powerindex_backup[index] = rtlpriv.io.read8(power_index_reg[index])


def rtl92c_dm_init_dynamic_txpower(rtlpriv: RtlPriv) -> None:
    """dm_common.c:520; tests rtlhal.board_type, which rtl8192cu never sets (efuse.board_type is separate)."""
    if rtlpriv.rtlhal.interface == C.INTF_USB and rtlpriv.rtlhal.board_type & 0x1:
        dm_savepowerindex(rtlpriv)
        rtlpriv.dm.dynamic_txpower_enable = True
    else:
        rtlpriv.dm.dynamic_txpower_enable = False
    rtlpriv.dm.last_dtp_lvl = C.TXHIGHPWRLEVEL_NORMAL
    rtlpriv.dm.dynamic_txhighpower_lvl = C.TXHIGHPWRLEVEL_NORMAL


def rtl92c_dm_init_edca_turbo(rtlpriv: RtlPriv) -> None:
    rtlpriv.dm.current_turbo_edca = False
    rtlpriv.dm.is_any_nonbepkts = False
    rtlpriv.dm.is_cur_rdlstate = False


def rtl92c_dm_initialize_txpower_tracking(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1131 via rtl92c_dm_initialize_txpower_tracking_thermalmeter."""
    rtlpriv.dm.txpower_tracking = True
    rtlpriv.dm.txpower_trackinginit = False


def rtl92c_dm_check_txpower_tracking(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1141: alternate between triggering the thermal meter and acting on its reading."""
    if not rtlpriv.dm.txpower_tracking:
        return
    if not rtlpriv.dm.tm_trigger:
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, C.RF_T_METER, C.RFREG_OFFSET_MASK, 0x60)
        rtlpriv.dm.tm_trigger = 1
        return
    rtl92c_dm_txpower_tracking_callback_thermalmeter(rtlpriv)
    rtlpriv.dm.tm_trigger = 0


def _u8(value: int) -> int:
    return value & 0xFF


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


def rtl92c_dm_txpower_tracking_callback_thermalmeter(rtlpriv: RtlPriv) -> None:
    """dm_common.c:698. Index arithmetic wraps as the C u8/s8 types do; the out-of-loop
    txpwr_level[i] read after the per-path loop (i == rf) is kept, it reads 0."""
    dm = rtlpriv.dm
    rtlphy = rtlpriv.phy
    rtlefuse = rtlpriv.efuse
    io = rtlpriv.io
    is2t = IS_92C_SERIAL(rtlpriv.rtlhal.version)
    ofdm_index = [0, 0]
    ofdm_index_old = [0, 0]
    cck_index_old = 0
    cck_index = 0
    txpwr_level = [0, 0, 0]
    ofdm_min_index = 6

    dm.txpower_trackinginit = True
    thermalvalue = _u8(rtl_get_rfreg(rtlpriv, C.RF90_PATH_A, C.RF_T_METER, 0x1f))
    rtl92c_phy_ap_calibrate(rtlpriv, _s8(thermalvalue - rtlefuse.eeprom_thermalmeter))
    rf = 2 if is2t else 1

    if not thermalvalue:
        return
    ele_d = rtl_get_bbreg(rtlpriv, C.ROFDM0_XATXIQIMBALANCE, C.MASKDWORD) & C.MASKOFDM_D
    for i in range(C.OFDM_TABLE_LENGTH):
        if ele_d == (ofdmswing_table[i] & C.MASKOFDM_D):
            ofdm_index_old[0] = i
            break
    if is2t:
        ele_d = rtl_get_bbreg(rtlpriv, C.ROFDM0_XBTXIQIMBALANCE, C.MASKDWORD) & C.MASKOFDM_D
        for i in range(C.OFDM_TABLE_LENGTH):
            if ele_d == (ofdmswing_table[i] & C.MASKOFDM_D):
                ofdm_index_old[1] = i
                break
    temp_cck = rtl_get_bbreg(rtlpriv, C.RCCK0_TXFILTER2, C.MASKDWORD) & C.MASKCCK
    cck_table = cckswing_table_ch14 if dm.cck_inch14 else cckswing_table_ch1ch13
    for i in range(C.CCK_TABLE_LENGTH):
        if temp_cck.to_bytes(4, "little") == bytes(cck_table[i][2:6]):
            cck_index_old = i
            break

    if not dm.thermalvalue:
        dm.thermalvalue = rtlefuse.eeprom_thermalmeter
        dm.thermalvalue_lck = thermalvalue
        dm.thermalvalue_iqk = thermalvalue
        for i in range(rf):
            dm.ofdm_index[i] = _s8(ofdm_index_old[i])
        dm.cck_index = _s8(cck_index_old)

    delta = abs(thermalvalue - dm.thermalvalue)
    delta_lck = abs(thermalvalue - dm.thermalvalue_lck)
    delta_iqk = abs(thermalvalue - dm.thermalvalue_iqk)

    if delta_lck > 1:
        dm.thermalvalue_lck = thermalvalue
        rtl92c_phy_lc_calibrate(rtlpriv)

    if delta > 0 and dm.txpower_track_control:
        if thermalvalue > dm.thermalvalue:
            for i in range(rf):
                dm.ofdm_index[i] = _s8(dm.ofdm_index[i] - delta)
            dm.cck_index = _s8(dm.cck_index - delta)
        else:
            for i in range(rf):
                dm.ofdm_index[i] = _s8(dm.ofdm_index[i] + delta)
            dm.cck_index = _s8(dm.cck_index + delta)

        if thermalvalue > rtlefuse.eeprom_thermalmeter:
            for i in range(rf):
                ofdm_index[i] = _u8(dm.ofdm_index[i] + 1)
            cck_index = _s8(dm.cck_index + 1)
        else:
            for i in range(rf):
                ofdm_index[i] = _u8(dm.ofdm_index[i])
            cck_index = dm.cck_index

        hotter = thermalvalue > rtlefuse.eeprom_thermalmeter
        colder = thermalvalue < rtlefuse.eeprom_thermalmeter
        for i in range(rf):
            if 0 <= txpwr_level[i] <= 26:
                if hotter:
                    ofdm_index[i] = _u8(ofdm_index[i] - (1 if delta < 5 else 2))
                elif delta > 5 and colder:
                    ofdm_index[i] = _u8(ofdm_index[i] + 1)
            elif 27 <= txpwr_level[i] <= 32 and hotter:
                ofdm_index[i] = _u8(ofdm_index[i] - (1 if delta < 5 else 2))
            elif 32 <= txpwr_level[i] <= 38 and hotter and delta > 5:
                ofdm_index[i] = _u8(ofdm_index[i] - 1)
        i = rf
        if 0 <= txpwr_level[i] <= 26:
            if hotter:
                cck_index = _s8(cck_index - (1 if delta < 5 else 2))
            elif delta > 5 and colder:
                cck_index = _s8(cck_index + 1)
        elif 27 <= txpwr_level[i] <= 32 and hotter:
            cck_index = _s8(cck_index - (1 if delta < 5 else 2))
        elif 32 <= txpwr_level[i] <= 38 and hotter and delta > 5:
            cck_index = _s8(cck_index - 1)

        for i in range(rf):
            if ofdm_index[i] > C.OFDM_TABLE_SIZE - 1:
                ofdm_index[i] = C.OFDM_TABLE_SIZE - 1
            elif ofdm_index[i] < ofdm_min_index:
                ofdm_index[i] = ofdm_min_index
        if cck_index > C.CCK_TABLE_SIZE - 1:
            cck_index = C.CCK_TABLE_SIZE - 1
        elif cck_index < 0:
            cck_index = 0

    if dm.txpower_track_control and delta != 0:
        _rtl92c_dm_write_ofdm_swing(rtlpriv, ofdm_index[0], rtlphy.reg_e94, rtlphy.reg_e9c,
                                    C.ROFDM0_XATXIQIMBALANCE, C.ROFDM0_XCTXAFE, C.BIT(31), C.BIT(29))
        cck_row = cckswing_table_ch14[cck_index] if dm.cck_inch14 else cckswing_table_ch1ch13[cck_index]
        for offset in range(8):
            io.write8(0xa22 + offset, cck_row[offset])
        if is2t:
            _rtl92c_dm_write_ofdm_swing(rtlpriv, ofdm_index[1], rtlphy.reg_eb4, rtlphy.reg_ebc,
                                        C.ROFDM0_XBTXIQIMBALANCE, C.ROFDM0_XDTXAFE, C.BIT(27), C.BIT(25))

    if delta_iqk > 3:
        dm.thermalvalue_iqk = thermalvalue
        rtl92c_phy_iq_calibrate(rtlpriv, False)
    if dm.txpower_track_control:
        dm.thermalvalue = thermalvalue


def _rtl92c_dm_write_ofdm_swing(rtlpriv: RtlPriv, ofdm_index: int, val_x: int, val_y: int, txiq_reg: int,
                                txafe_reg: int, x_bit: int, y_bit: int) -> None:
    """dm_common.c:952-986 (path A) and 1036-1074 (path B), which differ only in registers."""
    ele_d = (ofdmswing_table[ofdm_index] & 0xFFC00000) >> 22
    if val_x != 0:
        if val_x & 0x00000200:
            val_x = val_x | 0xFFFFFC00
        ele_a = ((val_x * ele_d) >> 8) & 0x000003FF
        if val_y & 0x00000200:
            val_y = val_y | 0xFFFFFC00
        ele_c = ((val_y * ele_d) >> 8) & 0x000003FF
        value32 = (ele_d << 22) | ((ele_c & 0x3F) << 16) | ele_a
        rtl_set_bbreg(rtlpriv, txiq_reg, C.MASKDWORD, value32)
        rtl_set_bbreg(rtlpriv, txafe_reg, C.MASKH4BITS, (ele_c & 0x000003C0) >> 6)
        rtl_set_bbreg(rtlpriv, C.ROFDM0_ECCATHRESHOLD, x_bit, ((val_x * ele_d) >> 7) & 0x01)
        rtl_set_bbreg(rtlpriv, C.ROFDM0_ECCATHRESHOLD, y_bit, ((val_y * ele_d) >> 7) & 0x01)
    else:
        rtl_set_bbreg(rtlpriv, txiq_reg, C.MASKDWORD, ofdmswing_table[ofdm_index])
        rtl_set_bbreg(rtlpriv, txafe_reg, C.MASKH4BITS, 0x00)
        rtl_set_bbreg(rtlpriv, C.ROFDM0_ECCATHRESHOLD, x_bit | y_bit, 0x00)


def rtl92c_dm_init_rate_adaptive_mask(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1170."""
    p_ra = rtlpriv.ra
    p_ra.ratr_state = C.DM_RATR_STA_INIT
    p_ra.pre_ratr_state = C.DM_RATR_STA_INIT
    rtlpriv.dm.useramask = rtlpriv.dm.dm_type == C.DM_TYPE_BYDRIVER


def rtl92c_dm_init_dynamic_bb_powersaving(rtlpriv: RtlPriv) -> None:
    dm_pstable = rtlpriv.dm_pstable
    dm_pstable.pre_ccastate = C.CCA_MAX
    dm_pstable.cur_ccasate = C.CCA_MAX
    dm_pstable.pre_rfstate = C.RF_MAX
    dm_pstable.cur_rfstate = C.RF_MAX
    dm_pstable.rssi_val_min = 0


def rtl92c_dm_init(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1311."""
    dm = rtlpriv.dm
    dm.dm_type = C.DM_TYPE_BYDRIVER
    dm.dm_flag = C.DYNAMIC_FUNC_DISABLE | C.DYNAMIC_FUNC_DIG
    dm.undec_sm_pwdb = -1
    dm.undec_sm_cck = -1
    dm.dm_initialgain_enable = True
    rtl_dm_diginit(rtlpriv, 0x20)
    dm.dm_flag |= C.HAL_DM_HIPWR_DISABLE
    rtl92c_dm_init_dynamic_txpower(rtlpriv)
    rtl92c_dm_init_edca_turbo(rtlpriv)
    rtl92c_dm_init_rate_adaptive_mask(rtlpriv)
    dm.dm_flag |= C.DYNAMIC_FUNC_SS
    rtl92c_dm_initialize_txpower_tracking(rtlpriv)
    rtl92c_dm_init_dynamic_bb_powersaving(rtlpriv)
    dm.ofdm_pkt_cnt = 0
    dm.dm_rssi_sel = C.RSSI_DEFAULT


def dm_restorepowerindex(rtlpriv: RtlPriv) -> None:
    """dm_common.c:139."""
    for index in range(6):
        rtlpriv.io.write8(power_index_reg[index], rtlpriv.dm.powerindex_backup[index])


def dm_writepowerindex(rtlpriv: RtlPriv, value: int) -> None:
    """dm_common.c:150."""
    for index in range(6):
        rtlpriv.io.write8(power_index_reg[index], value)


def rtl92c_dm_initial_gain_min_pwdb(rtlpriv: RtlPriv) -> int:
    """dm_common.c:173."""
    dm_digtable = rtlpriv.dm_digtable
    dm = rtlpriv.dm
    rssi_val_min = 0
    if (dm_digtable.curmultista_cstate == C.DIG_MULTISTA_CONNECT
            and dm_digtable.cursta_cstate == C.DIG_STA_CONNECT):
        if dm.entry_min_undec_sm_pwdb != 0:
            rssi_val_min = (dm.undec_sm_pwdb if dm.entry_min_undec_sm_pwdb > dm.undec_sm_pwdb
                            else dm.entry_min_undec_sm_pwdb)
        else:
            rssi_val_min = dm.undec_sm_pwdb
    elif dm_digtable.cursta_cstate in (C.DIG_STA_CONNECT, C.DIG_STA_BEFORE_CONNECT):
        rssi_val_min = dm.undec_sm_pwdb
    elif dm_digtable.curmultista_cstate == C.DIG_MULTISTA_CONNECT:
        rssi_val_min = dm.entry_min_undec_sm_pwdb
    if rssi_val_min > 100:
        rssi_val_min = 100
    return rssi_val_min & 0xFF


def rtl92c_dm_false_alarm_counter_statistics(rtlpriv: RtlPriv) -> None:
    """dm_common.c:201."""
    falsealm_cnt = rtlpriv.falsealm_cnt
    ret_value = rtl_get_bbreg(rtlpriv, C.ROFDM_PHYCOUNTER1, C.MASKDWORD)
    falsealm_cnt.cnt_parity_fail = (ret_value & 0xffff0000) >> 16
    ret_value = rtl_get_bbreg(rtlpriv, C.ROFDM_PHYCOUNTER2, C.MASKDWORD)
    falsealm_cnt.cnt_rate_illegal = ret_value & 0xffff
    falsealm_cnt.cnt_crc8_fail = (ret_value & 0xffff0000) >> 16
    ret_value = rtl_get_bbreg(rtlpriv, C.ROFDM_PHYCOUNTER3, C.MASKDWORD)
    falsealm_cnt.cnt_mcs_fail = ret_value & 0xffff
    ret_value = rtl_get_bbreg(rtlpriv, C.ROFDM0_FRAMESYNC, C.MASKDWORD)
    falsealm_cnt.cnt_fast_fsync_fail = ret_value & 0xffff
    falsealm_cnt.cnt_sb_search_fail = (ret_value & 0xffff0000) >> 16
    falsealm_cnt.cnt_ofdm_fail = (falsealm_cnt.cnt_parity_fail + falsealm_cnt.cnt_rate_illegal
                                  + falsealm_cnt.cnt_crc8_fail + falsealm_cnt.cnt_mcs_fail
                                  + falsealm_cnt.cnt_fast_fsync_fail + falsealm_cnt.cnt_sb_search_fail)

    rtl_set_bbreg(rtlpriv, C.RCCK0_FALSEALARMREPORT, C.BIT(14), 1)
    ret_value = rtl_get_bbreg(rtlpriv, C.RCCK0_FACOUNTERLOWER, C.MASKBYTE0)
    falsealm_cnt.cnt_cck_fail = ret_value
    ret_value = rtl_get_bbreg(rtlpriv, C.RCCK0_FACOUNTERUPPER, C.MASKBYTE3)
    falsealm_cnt.cnt_cck_fail += (ret_value & 0xff) << 8
    falsealm_cnt.cnt_all = (falsealm_cnt.cnt_parity_fail + falsealm_cnt.cnt_rate_illegal
                            + falsealm_cnt.cnt_crc8_fail + falsealm_cnt.cnt_mcs_fail + falsealm_cnt.cnt_cck_fail)

    rtl_set_bbreg(rtlpriv, C.ROFDM1_LSTF, 0x08000000, 1)
    rtl_set_bbreg(rtlpriv, C.ROFDM1_LSTF, 0x08000000, 0)
    rtl_set_bbreg(rtlpriv, C.RCCK0_FALSEALARMREPORT, 0x0000c000, 0)
    rtl_set_bbreg(rtlpriv, C.RCCK0_FALSEALARMREPORT, 0x0000c000, 2)


def rtl92c_dm_ctrl_initgain_by_fa(rtlpriv: RtlPriv) -> None:
    """dm_common.c:257; value_igi is a u8 local, cur_igvalue a u32."""
    dm_digtable = rtlpriv.dm_digtable
    cnt_all = rtlpriv.falsealm_cnt.cnt_all
    value_igi = dm_digtable.cur_igvalue & 0xFF
    if cnt_all < C.DM_DIG_FA_TH0:
        value_igi = (value_igi - 1) & 0xFF
    elif cnt_all < C.DM_DIG_FA_TH1:
        value_igi += 0
    elif cnt_all < C.DM_DIG_FA_TH2:
        value_igi = (value_igi + 1) & 0xFF
    elif cnt_all >= C.DM_DIG_FA_TH2:
        value_igi = (value_igi + 2) & 0xFF
    if value_igi > C.DM_DIG_FA_UPPER:
        value_igi = C.DM_DIG_FA_UPPER
    elif value_igi < C.DM_DIG_FA_LOWER:
        value_igi = C.DM_DIG_FA_LOWER
    if cnt_all > 10000:
        value_igi = C.DM_DIG_FA_UPPER
    dm_digtable.cur_igvalue = value_igi
    rtl92c_dm_write_dig(rtlpriv)


def rtl92c_dm_ctrl_initgain_by_rssi(rtlpriv: RtlPriv) -> None:
    """dm_common.c:284; 0x4fd is the BT state byte, unnamed in the source."""
    digtable = rtlpriv.dm_digtable
    cnt_all = rtlpriv.falsealm_cnt.cnt_all
    if cnt_all > 10000:
        digtable.large_fa_hit = (digtable.large_fa_hit + 1) & 0xFF
        if digtable.forbidden_igi < digtable.cur_igvalue:
            digtable.forbidden_igi = digtable.cur_igvalue & 0xFF
            digtable.large_fa_hit = 1
        if digtable.large_fa_hit >= 3:
            if (digtable.forbidden_igi + 1) > digtable.rx_gain_max:
                digtable.rx_gain_min = digtable.rx_gain_max
            else:
                digtable.rx_gain_min = (digtable.forbidden_igi + 1) & 0xFF
            digtable.recover_cnt = 3600
    elif digtable.recover_cnt != 0:
        digtable.recover_cnt -= 1
    elif digtable.large_fa_hit == 0:
        if (digtable.forbidden_igi - 1) < C.DM_DIG_MIN:
            digtable.forbidden_igi = C.DM_DIG_MIN
            digtable.rx_gain_min = C.DM_DIG_MIN
        else:
            digtable.forbidden_igi -= 1
            digtable.rx_gain_min = (digtable.forbidden_igi + 1) & 0xFF
    elif digtable.large_fa_hit == 3:
        digtable.large_fa_hit = 0
    if cnt_all < 250:
        isbt = rtlpriv.io.read8(0x4fd) & 0x01
        if not isbt:
            if cnt_all > digtable.fa_lowthresh:
                if (digtable.back_val - 2) < digtable.back_range_min:
                    digtable.back_val = digtable.back_range_min
                else:
                    digtable.back_val -= 2
            elif cnt_all < digtable.fa_lowthresh:
                if (digtable.back_val + 2) > digtable.back_range_max:
                    digtable.back_val = digtable.back_range_max
                else:
                    digtable.back_val += 2
        else:
            digtable.back_val = C.DM_DIG_BACKOFF_DEFAULT
    elif cnt_all > 1000:
        digtable.cur_igvalue = (digtable.pre_igvalue + 2) & 0xFFFFFFFF
    elif cnt_all > 750:
        digtable.cur_igvalue = (digtable.pre_igvalue + 1) & 0xFFFFFFFF
    elif cnt_all < 500:
        digtable.cur_igvalue = (digtable.pre_igvalue - 1) & 0xFFFFFFFF
    if digtable.cur_igvalue > digtable.rx_gain_max:
        digtable.cur_igvalue = digtable.rx_gain_max
    if digtable.cur_igvalue < digtable.rx_gain_min:
        digtable.cur_igvalue = digtable.rx_gain_min
    rtl92c_dm_write_dig(rtlpriv)


def rtl92c_dm_initial_gain_multi_sta(rtlpriv: RtlPriv) -> None:
    """dm_common.c:366; the C function-static initialized lives on rtlpriv->dm here."""
    dm_digtable = rtlpriv.dm_digtable
    dm = rtlpriv.dm
    rssi_strength = dm.entry_min_undec_sm_pwdb
    multi_sta = rtlpriv.mac80211.opmode == C.NL80211_IFTYPE_ADHOC
    if not multi_sta or dm_digtable.cursta_cstate == C.DIG_STA_DISCONNECT:
        dm.initial_gain_multi_sta_initialized = False
        dm_digtable.dig_ext_port_stage = C.DIG_EXT_PORT_STAGE_MAX
        return
    if not dm.initial_gain_multi_sta_initialized:
        dm.initial_gain_multi_sta_initialized = True
        dm_digtable.dig_ext_port_stage = C.DIG_EXT_PORT_STAGE_0
        dm_digtable.cur_igvalue = 0x20
        rtl92c_dm_write_dig(rtlpriv)
    if dm_digtable.curmultista_cstate == C.DIG_MULTISTA_CONNECT:
        if rssi_strength < dm_digtable.rssi_lowthresh and dm_digtable.dig_ext_port_stage != C.DIG_EXT_PORT_STAGE_1:
            if dm_digtable.dig_ext_port_stage == C.DIG_EXT_PORT_STAGE_2:
                dm_digtable.cur_igvalue = 0x20
                rtl92c_dm_write_dig(rtlpriv)
            dm_digtable.dig_ext_port_stage = C.DIG_EXT_PORT_STAGE_1
        elif rssi_strength > dm_digtable.rssi_highthresh:
            dm_digtable.dig_ext_port_stage = C.DIG_EXT_PORT_STAGE_2
            rtl92c_dm_ctrl_initgain_by_fa(rtlpriv)
    elif dm_digtable.dig_ext_port_stage != C.DIG_EXT_PORT_STAGE_0:
        dm_digtable.dig_ext_port_stage = C.DIG_EXT_PORT_STAGE_0
        dm_digtable.cur_igvalue = 0x20
        rtl92c_dm_write_dig(rtlpriv)


def rtl92c_dm_initial_gain_sta(rtlpriv: RtlPriv) -> None:
    """dm_common.c:417."""
    dm_digtable = rtlpriv.dm_digtable
    if (dm_digtable.presta_cstate == dm_digtable.cursta_cstate
            or dm_digtable.cursta_cstate in (C.DIG_STA_BEFORE_CONNECT, C.DIG_STA_CONNECT)):
        if dm_digtable.cursta_cstate != C.DIG_STA_DISCONNECT:
            dm_digtable.rssi_val_min = rtl92c_dm_initial_gain_min_pwdb(rtlpriv)
            if dm_digtable.rssi_val_min > 100:
                dm_digtable.rssi_val_min = 100
            rtl92c_dm_ctrl_initgain_by_rssi(rtlpriv)
    else:
        dm_digtable.rssi_val_min = 0
        dm_digtable.dig_ext_port_stage = C.DIG_EXT_PORT_STAGE_MAX
        dm_digtable.back_val = C.DM_DIG_BACKOFF_DEFAULT
        dm_digtable.cur_igvalue = 0x20
        dm_digtable.pre_igvalue = 0
        rtl92c_dm_write_dig(rtlpriv)


def rtl92c_dm_cck_packet_detection_thresh(rtlpriv: RtlPriv) -> None:
    """dm_common.c:445."""
    dm_digtable = rtlpriv.dm_digtable
    if dm_digtable.cursta_cstate == C.DIG_STA_CONNECT:
        dm_digtable.rssi_val_min = rtl92c_dm_initial_gain_min_pwdb(rtlpriv)
        if dm_digtable.rssi_val_min > 100:
            dm_digtable.rssi_val_min = 100
        threshold = 25 if dm_digtable.pre_cck_pd_state == C.CCK_PD_STAGE_LOWRSSI else 20
        if dm_digtable.rssi_val_min <= threshold:
            dm_digtable.cur_cck_pd_state = C.CCK_PD_STAGE_LOWRSSI
        else:
            dm_digtable.cur_cck_pd_state = C.CCK_PD_STAGE_HIGHRSSI
    else:
        dm_digtable.cur_cck_pd_state = C.CCK_PD_STAGE_MAX
    if dm_digtable.pre_cck_pd_state != dm_digtable.cur_cck_pd_state:
        if dm_digtable.cur_cck_pd_state in (C.CCK_PD_STAGE_LOWRSSI, C.CCK_PD_STAGE_MAX):
            rtl_set_bbreg(rtlpriv, C.RCCK0_CCA, C.MASKBYTE2, 0x83)
        else:
            rtl_set_bbreg(rtlpriv, C.RCCK0_CCA, C.MASKBYTE2, 0xcd)
        dm_digtable.pre_cck_pd_state = dm_digtable.cur_cck_pd_state


def rtl92c_dm_ctrl_initgain_by_twoport(rtlpriv: RtlPriv) -> None:
    """dm_common.c:485."""
    dm_digtable = rtlpriv.dm_digtable
    mac = rtlpriv.mac80211
    if mac.act_scanning:
        return
    if mac.link_state >= C.MAC80211_LINKED:
        dm_digtable.cursta_cstate = C.DIG_STA_CONNECT
    else:
        dm_digtable.cursta_cstate = C.DIG_STA_DISCONNECT
    dm_digtable.curmultista_cstate = C.DIG_MULTISTA_DISCONNECT
    rtl92c_dm_initial_gain_sta(rtlpriv)
    rtl92c_dm_initial_gain_multi_sta(rtlpriv)
    rtl92c_dm_cck_packet_detection_thresh(rtlpriv)
    dm_digtable.presta_cstate = dm_digtable.cursta_cstate


def rtl92c_dm_dig(rtlpriv: RtlPriv) -> None:
    """dm_common.c:508."""
    if not rtlpriv.dm.dm_initialgain_enable:
        return
    if not (rtlpriv.dm.dm_flag & C.DYNAMIC_FUNC_DIG):
        return
    rtl92c_dm_ctrl_initgain_by_twoport(rtlpriv)


def rtl92c_dm_write_dig(rtlpriv: RtlPriv) -> None:
    """dm_common.c:535; cur_igvalue is a u32 and wraps."""
    dm_digtable = rtlpriv.dm_digtable
    if rtlpriv.rtlhal.interface == C.INTF_USB and not dm_digtable.dig_enable_flag:
        dm_digtable.pre_igvalue = 0x17
        return
    dm_digtable.cur_igvalue = (dm_digtable.cur_igvalue - 1) & 0xFFFFFFFF
    if dm_digtable.cur_igvalue < C.DM_DIG_MIN:
        dm_digtable.cur_igvalue = C.DM_DIG_MIN
    if dm_digtable.pre_igvalue != dm_digtable.cur_igvalue:
        rtl_set_bbreg(rtlpriv, C.ROFDM0_XAAGCCORE1, 0x7f, dm_digtable.cur_igvalue)
        rtl_set_bbreg(rtlpriv, C.ROFDM0_XBAGCCORE1, 0x7f, dm_digtable.cur_igvalue)
        dm_digtable.pre_igvalue = dm_digtable.cur_igvalue


def rtl92c_dm_pwdb_monitor(rtlpriv: RtlPriv) -> None:
    """dm_common.c:571; the ADHOC/AP entry scan is a TODO in the source, so both extremes read 0."""
    if rtlpriv.mac80211.link_state != C.MAC80211_LINKED:
        return
    tmpentry_max_pwdb = 0
    tmpentry_min_pwdb = 0xff
    rtlpriv.dm.entry_max_undec_sm_pwdb = tmpentry_max_pwdb if tmpentry_max_pwdb != 0 else 0
    rtlpriv.dm.entry_min_undec_sm_pwdb = tmpentry_min_pwdb if tmpentry_min_pwdb != 0xff else 0


def rtl92c_dm_check_edca_turbo(rtlpriv: RtlPriv) -> None:
    """dm_common.c:615; the C function-statics last_* live on rtlpriv->dm here."""
    mac = rtlpriv.mac80211
    dm = rtlpriv.dm
    btcoexist = rtlpriv.btcoexist
    edca_be_ul = 0x5ea42b
    edca_be_dl = 0x5ea42b
    bt_change_edca = False
    if dm.last_bt_edca_ul != btcoexist.bt_edca_ul or dm.last_bt_edca_dl != btcoexist.bt_edca_dl:
        dm.current_turbo_edca = False
        dm.last_bt_edca_ul = btcoexist.bt_edca_ul
        dm.last_bt_edca_dl = btcoexist.bt_edca_dl
    if btcoexist.bt_edca_ul != 0:
        edca_be_ul = btcoexist.bt_edca_ul
        bt_change_edca = True
    if btcoexist.bt_edca_dl != 0:
        edca_be_dl = btcoexist.bt_edca_dl
        bt_change_edca = True
    if mac.link_state != C.MAC80211_LINKED:
        dm.current_turbo_edca = False
        return
    if not mac.ht_enable and not btcoexist.bt_coexistence:
        if not (edca_be_ul & 0xffff0000):
            edca_be_ul |= 0x005e0000
        if not (edca_be_dl & 0xffff0000):
            edca_be_dl |= 0x005e0000
    if bt_change_edca or (not dm.is_any_nonbepkts and not dm.disable_framebursting):
        cur_txok_cnt = rtlpriv.stats.txbytesunicast - dm.last_txok_cnt
        cur_rxok_cnt = rtlpriv.stats.rxbytesunicast - dm.last_rxok_cnt
        if cur_rxok_cnt > 4 * cur_txok_cnt:
            if not dm.is_cur_rdlstate or not dm.current_turbo_edca:
                rtlpriv.io.write32(C.REG_EDCA_BE_PARAM, edca_be_dl)
                dm.is_cur_rdlstate = True
        elif dm.is_cur_rdlstate or not dm.current_turbo_edca:
            rtlpriv.io.write32(C.REG_EDCA_BE_PARAM, edca_be_ul)
            dm.is_cur_rdlstate = False
        dm.current_turbo_edca = True
    elif dm.current_turbo_edca:
        rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_AC_PARAM, C.AC0_BE)
        dm.current_turbo_edca = False
    dm.is_any_nonbepkts = False
    dm.last_txok_cnt = rtlpriv.stats.txbytesunicast
    dm.last_rxok_cnt = rtlpriv.stats.rxbytesunicast


def rtl92c_dm_rf_saving(rtlpriv: RtlPriv, bforce_in_normal: bool) -> None:
    """dm_common.c:1198; 0xa74 and 0x818 are unnamed in the source."""
    dm_pstable = rtlpriv.dm_pstable
    if not rtlpriv.reg_init:
        rtlpriv.reg_874 = (rtl_get_bbreg(rtlpriv, C.RFPGA0_XCD_RFINTERFACESW, C.MASKDWORD) & 0x1CC000) >> 14
        rtlpriv.reg_c70 = (rtl_get_bbreg(rtlpriv, C.ROFDM0_AGCPARAMETER1, C.MASKDWORD) & C.BIT(3)) >> 3
        rtlpriv.reg_85c = (rtl_get_bbreg(rtlpriv, C.RFPGA0_XCD_SWITCHCONTROL, C.MASKDWORD) & 0xFF000000) >> 24
        rtlpriv.reg_a74 = (rtl_get_bbreg(rtlpriv, 0xa74, C.MASKDWORD) & 0xF000) >> 12
        rtlpriv.reg_init = True
    if bforce_in_normal:
        dm_pstable.cur_rfstate = C.RF_NORMAL
    elif dm_pstable.rssi_val_min != 0:
        if dm_pstable.pre_rfstate == C.RF_NORMAL:
            dm_pstable.cur_rfstate = C.RF_SAVE if dm_pstable.rssi_val_min >= 30 else C.RF_NORMAL
        else:
            dm_pstable.cur_rfstate = C.RF_NORMAL if dm_pstable.rssi_val_min <= 25 else C.RF_SAVE
    else:
        dm_pstable.cur_rfstate = C.RF_MAX
    if dm_pstable.pre_rfstate != dm_pstable.cur_rfstate:
        if dm_pstable.cur_rfstate == C.RF_SAVE:
            rtl_set_bbreg(rtlpriv, C.RFPGA0_XCD_RFINTERFACESW, 0x1C0000, 0x2)
            rtl_set_bbreg(rtlpriv, C.ROFDM0_AGCPARAMETER1, C.BIT(3), 0)
            rtl_set_bbreg(rtlpriv, C.RFPGA0_XCD_SWITCHCONTROL, 0xFF000000, 0x63)
            rtl_set_bbreg(rtlpriv, C.RFPGA0_XCD_RFINTERFACESW, 0xC000, 0x2)
            rtl_set_bbreg(rtlpriv, 0xa74, 0xF000, 0x3)
            rtl_set_bbreg(rtlpriv, 0x818, C.BIT(28), 0x0)
            rtl_set_bbreg(rtlpriv, 0x818, C.BIT(28), 0x1)
        else:
            rtl_set_bbreg(rtlpriv, C.RFPGA0_XCD_RFINTERFACESW, 0x1CC000, rtlpriv.reg_874)
            rtl_set_bbreg(rtlpriv, C.ROFDM0_AGCPARAMETER1, C.BIT(3), rtlpriv.reg_c70)
            rtl_set_bbreg(rtlpriv, C.RFPGA0_XCD_SWITCHCONTROL, 0xFF000000, rtlpriv.reg_85c)
            rtl_set_bbreg(rtlpriv, 0xa74, 0xF000, rtlpriv.reg_a74)
            rtl_set_bbreg(rtlpriv, 0x818, C.BIT(28), 0x0)
        dm_pstable.pre_rfstate = dm_pstable.cur_rfstate


def rtl92c_dm_dynamic_bb_powersaving(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1268; a 92C (2T) takes the commented-out 1R-CCA path, i.e. nothing."""
    dm_pstable = rtlpriv.dm_pstable
    mac = rtlpriv.mac80211
    if mac.link_state == C.MAC80211_NOLINK and rtlpriv.dm.entry_min_undec_sm_pwdb == 0:
        dm_pstable.rssi_val_min = 0
    if mac.link_state == C.MAC80211_LINKED:
        if mac.opmode == C.NL80211_IFTYPE_ADHOC:
            dm_pstable.rssi_val_min = rtlpriv.dm.entry_min_undec_sm_pwdb
        else:
            dm_pstable.rssi_val_min = rtlpriv.dm.undec_sm_pwdb
    else:
        dm_pstable.rssi_val_min = rtlpriv.dm.entry_min_undec_sm_pwdb
    if not IS_92C_SERIAL(rtlpriv.rtlhal.version):
        rtl92c_dm_rf_saving(rtlpriv, False)


def rtl92c_dm_dynamic_txpower(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1336."""
    dm = rtlpriv.dm
    mac = rtlpriv.mac80211
    if not dm.dynamic_txpower_enable:
        return
    if dm.dm_flag & C.HAL_DM_HIPWR_DISABLE:
        dm.dynamic_txhighpower_lvl = C.TXHIGHPWRLEVEL_NORMAL
        return
    if mac.link_state < C.MAC80211_LINKED and dm.entry_min_undec_sm_pwdb == 0:
        dm.dynamic_txhighpower_lvl = C.TXHIGHPWRLEVEL_NORMAL
        dm.last_dtp_lvl = C.TXHIGHPWRLEVEL_NORMAL
        return
    if mac.link_state >= C.MAC80211_LINKED:
        if mac.opmode == C.NL80211_IFTYPE_ADHOC:
            undec_sm_pwdb = dm.entry_min_undec_sm_pwdb
        else:
            undec_sm_pwdb = dm.undec_sm_pwdb
    else:
        undec_sm_pwdb = dm.entry_min_undec_sm_pwdb
    if undec_sm_pwdb >= C.TX_POWER_NEAR_FIELD_THRESH_LVL2:
        dm.dynamic_txhighpower_lvl = C.TXHIGHPWRLEVEL_LEVEL2
    elif C.TX_POWER_NEAR_FIELD_THRESH_LVL1 <= undec_sm_pwdb < C.TX_POWER_NEAR_FIELD_THRESH_LVL2 - 3:
        dm.dynamic_txhighpower_lvl = C.TXHIGHPWRLEVEL_LEVEL1
    elif undec_sm_pwdb < C.TX_POWER_NEAR_FIELD_THRESH_LVL1 - 5:
        dm.dynamic_txhighpower_lvl = C.TXHIGHPWRLEVEL_NORMAL
    if dm.dynamic_txhighpower_lvl != dm.last_dtp_lvl:
        rtl92c_phy_set_txpower_level(rtlpriv, rtlpriv.phy.current_channel)
        if dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_NORMAL:
            dm_restorepowerindex(rtlpriv)
        elif dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_LEVEL1:
            dm_writepowerindex(rtlpriv, 0x14)
        elif dm.dynamic_txhighpower_lvl == C.TXHIGHPWRLEVEL_LEVEL2:
            dm_writepowerindex(rtlpriv, 0x10)
    dm.last_dtp_lvl = dm.dynamic_txhighpower_lvl


def rtl92c_dm_bt_coexist(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1721. rtl8192cu never sets btcoexist.bt_coexistence, so the BC4 body is off its graph."""
    if rtlpriv.btcoexist.bt_coexistence and rtlpriv.btcoexist.bt_coexist_type == C.BT_CSR_BC4:
        raise NotImplementedError("rtl92c BT_CSR_BC4 coexistence is not on the rtl8192cu graph")


def rtl92c_dm_watchdog(rtlpriv: RtlPriv) -> None:
    """dm_common.c:1416; rtl92c_dm_refresh_rate_adaptive_mask is commented out in the source."""
    ppsc = rtlpriv.psc
    fw_current_inpsmode = rtlpriv.cfg.ops.get_hw_reg(rtlpriv, C.HW_VAR_FW_PSMODE_STATUS)
    fw_ps_awake = rtlpriv.cfg.ops.get_hw_reg(rtlpriv, C.HW_VAR_FWLPS_RF_ON)
    if ppsc.p2p_ps_mode:
        fw_ps_awake = False
    if ppsc.rfpwr_state == C.ERFON and not fw_current_inpsmode and fw_ps_awake and not ppsc.rfchange_inprogress:
        rtl92c_dm_pwdb_monitor(rtlpriv)
        rtl92c_dm_dig(rtlpriv)
        rtl92c_dm_false_alarm_counter_statistics(rtlpriv)
        rtl92c_dm_dynamic_bb_powersaving(rtlpriv)
        rtl92c_dm_dynamic_txpower(rtlpriv)
        rtl92c_dm_check_txpower_tracking(rtlpriv)
        rtl92c_dm_bt_coexist(rtlpriv)
        rtl92c_dm_check_edca_turbo(rtlpriv)
