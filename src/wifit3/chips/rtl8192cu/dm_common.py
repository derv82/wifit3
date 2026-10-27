"""rtlwifi rtl8192c/dm_common.c: dynamic-mechanism init and the TX power tracking trigger."""
from __future__ import annotations

from . import constants as C
from .core import rtl_dm_diginit
from .mac import IS_92C_SERIAL
from .phy_common import rtl92c_phy_ap_calibrate, rtl92c_phy_iq_calibrate, rtl92c_phy_lc_calibrate
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
