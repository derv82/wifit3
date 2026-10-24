"""Port of rtl8xxxu 8192f.c (v6.19.14) plus the two 8188f.c helpers it calls."""
from __future__ import annotations

import time

from . import constants as C
from .core import (
    LED_OFF,
    LED_ON,
    RF_A,
    RF_B,
    Rtl8xxxuFileops,
    Rtl8xxxuPriv,
    rtl8xxxu_config_endpoints_no_sie,
    rtl8xxxu_firmware_self_reset,
    rtl8xxxu_flush_fifo,
    rtl8xxxu_gen2_usb_quirks,
    rtl8xxxu_identify_vendor_1bit,
    rtl8xxxu_init_phy_regs,
    rtl8xxxu_init_phy_rf,
    rtl8xxxu_read8,
    rtl8xxxu_read16,
    rtl8xxxu_read32,
    rtl8xxxu_read_efuse8,
    rtl8xxxu_read_rfreg,
    rtl8xxxu_write8,
    rtl8xxxu_write8_clear,
    rtl8xxxu_write8_set,
    rtl8xxxu_write16,
    rtl8xxxu_write16_clear,
    rtl8xxxu_write16_set,
    rtl8xxxu_write32,
    rtl8xxxu_write32_clear,
    rtl8xxxu_write32_mask,
    rtl8xxxu_write32_set,
    rtl8xxxu_write_rfreg,
    rtl8xxxu_write_rfreg_mask,
    u32_get_bits,
    u32_replace_bits,
)
from .iqk import rtl8192fu_phy_iq_calibrate
from .tables import (
    RTL8192F_AGC_TABLE,
    RTL8192F_MAC_INIT_TABLE,
    RTL8192FU_PHY_INIT_TABLE,
    RTL8192FU_RADIOA_INIT_TABLE,
    RTL8192FU_RADIOB_INIT_TABLE,
)

XTAL1 = C.GENMASK(6, 1)
XTAL0 = C.GENMASK(30, 25)


def _signed4(value: int) -> int:
    value &= 0x0F
    return value - 16 if value & 0x08 else value


def rtl8192fu_identify_chip(priv: Rtl8xxxuPriv) -> None:
    priv.chip_name = "8192FU"
    priv.rf_paths = 2
    priv.rx_paths = 2
    priv.tx_paths = 2

    sys_cfg = rtl8xxxu_read32(priv, C.REG_SYS_CFG)
    priv.chip_cut = u32_get_bits(sys_cfg, C.SYS_CFG_CHIP_VERSION_MASK)
    if sys_cfg & C.SYS_CFG_TRP_VAUX_EN:
        raise OSError("Unsupported test chip")

    val32 = rtl8xxxu_read32(priv, C.REG_MULTI_FUNC_CTRL)
    priv.has_wifi = bool(u32_get_bits(val32, C.MULTI_WIFI_FUNC_EN))
    priv.has_bluetooth = bool(u32_get_bits(val32, C.MULTI_BT_FUNC_EN))
    priv.has_gps = bool(u32_get_bits(val32, C.MULTI_GPS_FUNC_EN))
    priv.is_multi_func = True

    rtl8xxxu_identify_vendor_1bit(priv, sys_cfg & C.SYS_CFG_VENDOR_ID)

    val32 = rtl8xxxu_read32(priv, C.REG_GPIO_OUTSTS)
    priv.rom_rev = u32_get_bits(val32, C.GPIO_RF_RL_ID)

    rtl8xxxu_config_endpoints_no_sie(priv)


def rtl8188f_channel_to_group(channel: int) -> tuple[int, int]:
    """8188f.c:338."""
    if channel < 3:
        group = 0
    elif channel < 6:
        group = 1
    elif channel < 9:
        group = 2
    elif channel < 12:
        group = 3
    else:
        group = 4
    cck_group = 5 if channel == 14 else group
    return group, cck_group


def rtl8192f_set_tx_power(priv: Rtl8xxxuPriv, channel: int, ht40: bool) -> None:
    group, cck_group = rtl8188f_channel_to_group(channel)

    cck = priv.cck_tx_power_index_A[cck_group]
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_A_CCK1_MCS32, 0x00007F00, cck)
    val32 = (cck << 16) | (cck << 8) | cck
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_CCK11_A_CCK2_11, 0x7F7F7F00, val32)

    ofdmbase = (priv.ht40_1s_tx_power_index_A[group] + priv.ofdm_tx_power_diff[RF_A][0]) & 0xFF
    ofdm = ofdmbase | ofdmbase << 8 | ofdmbase << 16 | ofdmbase << 24
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_A_RATE18_06, 0x7F7F7F7F, ofdm)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_A_RATE54_24, 0x7F7F7F7F, ofdm)

    mcsbase = priv.ht40_1s_tx_power_index_A[group]
    if ht40:
        mcsbase += priv.ht40_tx_power_diff[RF_A][0]
    else:
        mcsbase += priv.ht20_tx_power_diff[RF_A][0]
    mcsbase &= 0xFF
    mcs = mcsbase | mcsbase << 8 | mcsbase << 16 | mcsbase << 24
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_A_MCS03_MCS00, 0x7F7F7F7F, mcs)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_A_MCS07_MCS04, 0x7F7F7F7F, mcs)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_A_MCS11_MCS08, 0x7F7F7F7F, mcs)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_A_MCS15_MCS12, 0x7F7F7F7F, mcs)

    if priv.tx_paths == 1:
        return

    cck = priv.cck_tx_power_index_B[cck_group]
    val32 = (cck << 16) | (cck << 8) | cck
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_CCK1_55_MCS32, 0x7F7F7F00, val32)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_CCK11_A_CCK2_11, 0x0000007F, cck)

    ofdmbase = (priv.ht40_1s_tx_power_index_B[group] + priv.ofdm_tx_power_diff[RF_B][1]) & 0xFF
    ofdm = ofdmbase | ofdmbase << 8 | ofdmbase << 16 | ofdmbase << 24
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_RATE18_06, 0x7F7F7F7F, ofdm)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_RATE54_24, 0x7F7F7F7F, ofdm)

    mcsbase = priv.ht40_1s_tx_power_index_B[group]
    if ht40:
        mcsbase += priv.ht40_tx_power_diff[RF_B][1]
    else:
        mcsbase += priv.ht20_tx_power_diff[RF_B][1]
    mcsbase &= 0xFF
    mcs = mcsbase | mcsbase << 8 | mcsbase << 16 | mcsbase << 24
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_MCS03_MCS00, 0x7F7F7F7F, mcs)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_MCS07_MCS04, 0x7F7F7F7F, mcs)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_MCS11_MCS08, 0x7F7F7F7F, mcs)
    rtl8xxxu_write32_mask(priv, C.REG_TX_AGC_B_MCS15_MCS12, 0x7F7F7F7F, mcs)


def rtl8192f_revise_cck_tx_psf(priv: Rtl8xxxuPriv, channel: int) -> None:
    if channel == 13:
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER1, 0xF8FE0001)
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER2, 0x64B80C1C)
        rtl8xxxu_write16(priv, C.REG_CCK0_DEBUG_PORT, 0x8810)
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER3, 0x01235667)
    elif channel == 14:
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER1, 0xE82C0001)
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER2, 0x0000B81C)
        rtl8xxxu_write16(priv, C.REG_CCK0_DEBUG_PORT, 0x0000)
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER3, 0x00003667)
    else:
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER1, 0xE82C0001)
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER2, 0x64B80C1C)
        rtl8xxxu_write16(priv, C.REG_CCK0_DEBUG_PORT, 0x8810)
        rtl8xxxu_write32(priv, C.REG_CCK0_TX_FILTER3, 0x01235667)


def rtl8192fu_config_kfree(priv: Rtl8xxxuPriv, channel: int) -> None:
    bb_gain = [C.EFUSE_UNDEFINED, C.EFUSE_UNDEFINED, C.EFUSE_UNDEFINED]
    bb_gain_path_mask = (0x0F, 0xF0)

    channel_idx = 0
    if 1 <= channel <= 3:
        channel_idx = 0
    if 4 <= channel <= 9:
        channel_idx = 1
    if 10 <= channel <= 14:
        channel_idx = 2

    for index, offset in ((1, 0x1EE), (0, 0x1EC), (2, 0x1EA)):
        value = rtl8xxxu_read_efuse8(priv, offset)
        if value is not None:
            bb_gain[index] = value

    if bb_gain[1] == C.EFUSE_UNDEFINED:
        return
    if bb_gain[0] == C.EFUSE_UNDEFINED:
        bb_gain[0] = bb_gain[1]
    if bb_gain[2] == C.EFUSE_UNDEFINED:
        bb_gain[2] = bb_gain[1]

    for rfpath in range(RF_A, priv.rf_paths):
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_UNKNOWN_55, C.BIT(5), 1)
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_GAIN_CTRL, C.BIT(18), 1)
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_GAIN_CCA, C.BIT(7), 1)
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_WE_LUT, C.BIT(7), 1)

        bb_gain_for_path = bb_gain[channel_idx] & bb_gain_path_mask[rfpath]
        bb_gain_for_path >>= (bb_gain_path_mask[rfpath] & -bb_gain_path_mask[rfpath]).bit_length() - 1

        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_TXPA_G3, 0x70000, channel_idx * 2)
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_TXPA_G3, 0x3F, bb_gain_for_path)
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_TXPA_G3, 0x70000, channel_idx * 2 + 1)
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_TXPA_G3, 0x3F, bb_gain_for_path)

        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_GAIN_CCA, C.BIT(7), 0)
        rtl8xxxu_write_rfreg_mask(priv, rfpath, C.RF6052_REG_WE_LUT, C.BIT(7), 0)


def rtl8192fu_config_channel(priv: Rtl8xxxuPriv, channel: int) -> None:
    """NL80211_CHAN_WIDTH_20 only: ht40, sec_ch_above and subchannel stay 0 (8192f.c:584)."""
    ht40 = False
    subchannel = 0

    val32 = rtl8xxxu_read_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG)

    rtl8192f_revise_cck_tx_psf(priv, channel)

    val32 &= ~(C.BIT(18) | C.BIT(17))
    val32 = u32_replace_bits(val32, channel, 0xFF)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG, val32)
    if priv.rf_paths > 1:
        rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_MODE_AG, val32)

    rtl8192fu_config_kfree(priv, channel)

    rtl8xxxu_write8(priv, C.REG_DATA_SUBCHANNEL, subchannel)

    rtl8xxxu_write32_clear(priv, C.REG_OFDM0_TX_PSDO_NOISE_WEIGHT, C.GENMASK(31, 30))

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_RF_MODE, C.FPGA_RF_MODE, ht40)
    rtl8xxxu_write32_mask(priv, C.REG_FPGA1_RF_MODE, C.FPGA_RF_MODE, ht40)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_RF_MODE, C.GENMASK(10, 8), 4)
    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_RF_MODE, C.BIT(13) | C.BIT(12), 2)
    rtl8xxxu_write32_mask(priv, C.REG_ANTDIV_PARA1, C.BIT(27) | C.BIT(26), 2)

    rtl8xxxu_write32_set(priv, C.REG_FPGA0_RF_MODE, C.FPGA_RF_MODE_CCK)

    val32 = rtl8xxxu_read_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG)
    val32 &= ~C.MODE_AG_BW_MASK
    val32 |= C.MODE_AG_BW_20MHZ_8723B
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG, val32)
    if priv.rf_paths > 1:
        rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_MODE_AG, val32)

    rtl8xxxu_write32_mask(priv, C.REG_TAP_UPD_97F, C.BIT(21) | C.BIT(20), 2)
    rtl8xxxu_write32_mask(priv, C.REG_DOWNSAM_FACTOR, C.BIT(29) | C.BIT(28), 2)
    rtl8xxxu_write32_mask(priv, C.REG_RX_DFIR_MOD_97F, 0x1FF, 0x1A3)


def rtl8192fu_init_aggregation(priv: Rtl8xxxuPriv) -> None:
    agg_ctrl = rtl8xxxu_read8(priv, C.REG_TRXDMA_CTRL)
    agg_ctrl &= ~C.TRXDMA_CTRL_RXDMA_AGG_EN

    agg_rx = rtl8xxxu_read32(priv, C.REG_RXDMA_AGG_PG_TH)
    agg_rx &= ~C.RXDMA_USB_AGG_ENABLE
    agg_rx &= ~0xFF0F

    rtl8xxxu_write8(priv, C.REG_TRXDMA_CTRL, agg_ctrl)
    rtl8xxxu_write32(priv, C.REG_RXDMA_AGG_PG_TH, agg_rx)


def _efuse_tx_power(raw: bytes, offset: int) -> tuple[list[int], list[int], int, list[tuple[int, int, int]]]:
    """struct rtl8192eu_efuse_tx_power: cck_base[6], ht40_base[5], ht20_ofdm_1s_diff, pwr_diff[3]."""
    cck_base = list(raw[offset:offset + 6])
    ht40_base = list(raw[offset + 6:offset + 11])
    ht20_ofdm_1s_diff_b = _signed4(raw[offset + 11] >> 4)
    pwr_diff = []
    for i in range(3):
        low = raw[offset + 12 + 2 * i]
        high = raw[offset + 13 + 2 * i]
        pwr_diff.append((_signed4(low), _signed4(low >> 4), _signed4(high)))  # (ht20, ht40, ofdm)
    return cck_base, ht40_base, ht20_ofdm_1s_diff_b, pwr_diff


def rtl8192fu_parse_efuse(priv: Rtl8xxxuPriv) -> None:
    raw = bytes(priv.efuse_wifi)
    if int.from_bytes(raw[0:2], "little") != 0x8129:
        raise ValueError("RTL8192FU EFUSE rtl_id != 0x8129")

    priv.mac_addr = raw[0x107:0x10D]

    cck_a, ht40_a, ht20_1s_a, diff_a = _efuse_tx_power(raw, 0x10)
    cck_b, ht40_b, ht20_1s_b, diff_b = _efuse_tx_power(raw, 0x3A)
    priv.cck_tx_power_index_A = cck_a
    priv.cck_tx_power_index_B = cck_b
    priv.ht40_1s_tx_power_index_A = ht40_a
    priv.ht40_1s_tx_power_index_B = ht40_b

    priv.ht20_tx_power_diff = [(ht20_1s_a, ht20_1s_b)] + [(diff_a[i][0], diff_b[i][0]) for i in range(3)]
    priv.ht40_tx_power_diff = [(0, 0)] + [(diff_a[i][1], diff_b[i][1]) for i in range(3)]
    priv.ofdm_tx_power_diff = [(0, 0)] + [(diff_a[i][2], diff_b[i][2]) for i in range(3)]

    priv.default_crystal_cap = raw[0xB9] & 0x3F
    priv.rfe_type = raw[0xCA] & 0x1F


def rtl8192fu_init_phy_bb(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write16_set(priv, C.REG_SYS_FUNC, C.SYS_FUNC_BBRSTB | C.SYS_FUNC_BB_GLB_RSTN)
    rtl8xxxu_write8(priv, C.REG_RF_CTRL, C.RF_ENABLE | C.RF_RSTB | C.RF_SDMRSTB)
    rtl8xxxu_write8(priv, C.REG_LDOHCI12_CTRL, 0xF)
    rtl8xxxu_write8(priv, C.REG_SYS_SWR_CTRL2 + 1, 0xE9)
    rtl8xxxu_init_phy_regs(priv, RTL8192FU_PHY_INIT_TABLE)
    rtl8xxxu_init_phy_regs(priv, RTL8192F_AGC_TABLE)


def rtl8192fu_init_phy_rf(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_init_phy_rf(priv, RTL8192FU_RADIOA_INIT_TABLE, RF_A)
    rtl8xxxu_init_phy_rf(priv, RTL8192FU_RADIOB_INIT_TABLE, RF_B)


def rtl8188f_phy_lc_calibrate(priv: Rtl8xxxuPriv) -> None:
    """8188f.c:790."""
    lstf = rtl8xxxu_read32(priv, C.REG_OFDM1_LSTF)
    if lstf & C.OFDM_LSTF_MASK:
        rtl8xxxu_write32(priv, C.REG_OFDM1_LSTF, lstf & ~C.OFDM_LSTF_MASK)
    else:
        rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0xFF)

    rf_amode = rtl8xxxu_read_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG, rf_amode | 0x08000)
    for _ in range(100):
        if (rtl8xxxu_read_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG) & 0x08000) == 0:
            break
        time.sleep(10e-3)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_MODE_AG, rf_amode)

    if lstf & C.OFDM_LSTF_MASK:
        rtl8xxxu_write32(priv, C.REG_OFDM1_LSTF, lstf)
    else:
        rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0x00)


def rtl8192f_phy_lc_calibrate(priv: Rtl8xxxuPriv) -> None:
    backup_mask = C.BIT(31) | C.BIT(30)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_TX_PSDO_NOISE_WEIGHT)
    backup = u32_get_bits(val32, backup_mask)
    val32 = u32_replace_bits(val32, 0, backup_mask)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TX_PSDO_NOISE_WEIGHT, val32)

    rtl8188f_phy_lc_calibrate(priv)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_TX_PSDO_NOISE_WEIGHT)
    val32 = u32_replace_bits(val32, backup, backup_mask)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TX_PSDO_NOISE_WEIGHT, val32)

    rtl8xxxu_write32_clear(priv, C.REG_FPGA0_RF_MODE, C.FPGA_RF_MODE_OFDM)
    rtl8xxxu_write32_set(priv, C.REG_FPGA0_RF_MODE, C.FPGA_RF_MODE_OFDM)


def _poll_aps_fsmco(priv: Rtl8xxxuPriv, done) -> None:
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if done(rtl8xxxu_read32(priv, C.REG_APS_FSMCO)):
            return
        time.sleep(10e-6)
    raise OSError("REG_APS_FSMCO poll timed out (-EBUSY)")


def rtl8192fu_disabled_to_emu(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write16_clear(priv, C.REG_APS_FSMCO, C.APS_FSMCO_HW_POWERDOWN | C.APS_FSMCO_HW_SUSPEND)
    rtl8xxxu_write32_clear(priv, C.REG_GPIO_INTM, C.BIT(16))
    rtl8xxxu_write16_clear(priv, C.REG_APS_FSMCO, C.APS_FSMCO_PCIE | C.APS_FSMCO_HW_SUSPEND)


def rtl8192fu_emu_to_active(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8_set(priv, C.REG_LDOA15_CTRL, C.LDOA15_ENABLE)
    rtl8xxxu_write32_clear(priv, C.REG_PAD_CTRL1, C.BIT(28))
    time.sleep(1e-3)
    rtl8xxxu_write8_clear(priv, C.REG_SYS_ISO_CTRL, C.SYS_ISO_ANALOG_IPS)

    val16 = C.APS_FSMCO_PCIE | C.APS_FSMCO_HW_SUSPEND | C.APS_FSMCO_SW_LPS
    rtl8xxxu_write16_clear(priv, C.REG_APS_FSMCO, val16)

    _poll_aps_fsmco(priv, lambda val32: val32 & C.BIT(17))

    rtl8xxxu_write32_set(priv, C.REG_APS_FSMCO, C.APS_FSMCO_WLON_RESET)

    _poll_aps_fsmco(priv, lambda val32: (val32 & (C.APS_FSMCO_MAC_ENABLE | C.APS_FSMCO_MAC_OFF)) == 0)

    rtl8xxxu_write32_set(priv, C.REG_AFE_MISC, C.BIT(18))
    rtl8xxxu_write16_clear(priv, C.REG_APS_FSMCO, C.APS_FSMCO_HW_POWERDOWN)
    rtl8xxxu_write16_clear(priv, C.REG_APS_FSMCO, C.APS_FSMCO_PCIE | C.APS_FSMCO_HW_SUSPEND)
    rtl8xxxu_write32_set(priv, C.REG_LDO_SW_CTRL, C.BIT(31))
    rtl8xxxu_write16_set(priv, C.REG_APS_FSMCO, C.APS_FSMCO_MAC_ENABLE)

    _poll_aps_fsmco(priv, lambda val32: (val32 & C.APS_FSMCO_MAC_ENABLE) == 0)

    rtl8xxxu_write8_set(priv, C.REG_AFE_MISC, C.AFE_MISC_WL_XTAL_CTRL)
    rtl8xxxu_write16_set(priv, C.REG_GPIO_INTM, C.GPIO_INTM_EDGE_TRIG_IRQ)
    rtl8xxxu_write16_clear(priv, C.REG_GPIO_IO_SEL_2, C.GPIO_IO_SEL_2_GPIO09_IRQ)
    rtl8xxxu_write16_clear(priv, C.REG_GPIO_IO_SEL_2, C.GPIO_IO_SEL_2_GPIO09_INPUT)
    rtl8xxxu_write8_set(priv, C.REG_HSIMR, C.BIT(0))
    rtl8xxxu_write8_clear(priv, C.REG_MULTI_FUNC_CTRL, C.MULTI_WIFI_HW_ROF_EN)
    rtl8xxxu_write8_set(priv, C.REG_RSV_CTRL, C.BIT(7))
    rtl8xxxu_write16_set(priv, C.REG_MULTI_FUNC_CTRL, C.BIT(14))
    rtl8xxxu_write8(priv, C.REG_RF_CTRL, 0)
    rtl8xxxu_write8(priv, C.REG_AFE_CTRL4 + 3, 0)
    rtl8xxxu_write8(priv, C.REG_RF_CTRL, C.RF_SDMRSTB | C.RF_RSTB | C.RF_ENABLE)
    rtl8xxxu_write8(priv, C.REG_AFE_CTRL4 + 3, C.RF_SDMRSTB | C.RF_RSTB | C.RF_ENABLE)
    rtl8xxxu_write8_set(priv, C.REG_RSVD_1, C.BIT(5))
    rtl8xxxu_write8(priv, C.REG_RSVD_4, 0xCC)
    rtl8xxxu_write8_clear(priv, C.REG_AFE_XTAL_CTRL, C.BIT(4) | C.BIT(3))
    rtl8xxxu_write32(priv, C.REG_GPIO_A0, 0xFFFFFFFF)
    rtl8xxxu_write8(priv, C.REG_GPIO_B0, 0xFF)
    rtl8xxxu_write8_clear(priv, C.REG_RSV_CTRL, C.BIT(7))


def rtl8192fu_active_to_emu(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8_clear(priv, C.REG_SYS_FUNC, C.SYS_FUNC_BBRSTB)
    rtl8xxxu_write16_clear(priv, C.REG_GPIO_INTM, C.GPIO_INTM_EDGE_TRIG_IRQ)
    rtl8xxxu_write32_set(priv, C.REG_APS_FSMCO, C.APS_FSMCO_WLON_RESET)
    rtl8xxxu_write16_set(priv, C.REG_APS_FSMCO, C.APS_FSMCO_MAC_OFF)

    _poll_aps_fsmco(priv, lambda val32: (val32 & C.APS_FSMCO_MAC_OFF) == 0)

    rtl8xxxu_write8_set(priv, C.REG_SYS_ISO_CTRL, C.SYS_ISO_ANALOG_IPS)
    rtl8xxxu_write8_clear(priv, C.REG_LDOA15_CTRL, C.LDOA15_ENABLE)


def rtl8192fu_emu_to_disabled(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 3, 0x20)
    val16 = rtl8xxxu_read16(priv, C.REG_APS_FSMCO)
    val16 &= ~C.APS_FSMCO_PCIE
    val16 |= C.APS_FSMCO_HW_SUSPEND
    rtl8xxxu_write16(priv, C.REG_APS_FSMCO, val16)
    rtl8xxxu_write32_set(priv, C.REG_GPIO_INTM, C.BIT(16))


def rtl8192fu_active_to_lps(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0xFF)

    retry = 100
    while True:
        if not rtl8xxxu_read32(priv, C.REG_SCH_TX_CMD):
            break
        time.sleep(10e-6)
        retry -= 1
        if retry < 0:
            break
    if not retry:
        raise OSError("Failed to flush TX queue")

    rtl8xxxu_write8_clear(priv, C.REG_SYS_FUNC, C.SYS_FUNC_BBRSTB)
    time.sleep(2e-6)
    rtl8xxxu_write8_clear(priv, C.REG_SYS_FUNC, C.SYS_FUNC_BB_GLB_RSTN)

    val16 = rtl8xxxu_read16(priv, C.REG_CR)
    val16 &= 0xFF00
    val16 |= C.CR_HCI_RXDMA_ENABLE | C.CR_HCI_TXDMA_ENABLE
    val16 &= ~C.CR_SECURITY_ENABLE
    rtl8xxxu_write16(priv, C.REG_CR, val16)

    rtl8xxxu_write8_set(priv, C.REG_DUAL_TSF_RST, C.DUAL_TSF_TX_OK)


def rtl8192fu_power_on(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8(priv, C.REG_USB_ACCESS_TIMEOUT, 0x80)
    rtl8192fu_disabled_to_emu(priv)
    rtl8192fu_emu_to_active(priv)

    rtl8xxxu_write16(priv, C.REG_CR, 0)
    val16 = rtl8xxxu_read16(priv, C.REG_CR)
    val16 |= C.CR_HCI_TXDMA_ENABLE | C.CR_HCI_RXDMA_ENABLE | \
        C.CR_TXDMA_ENABLE | C.CR_RXDMA_ENABLE | \
        C.CR_PROTOCOL_ENABLE | C.CR_SCHEDULE_ENABLE | \
        C.CR_SECURITY_ENABLE | C.CR_CALTIMER_ENABLE
    rtl8xxxu_write16(priv, C.REG_CR, val16)


def rtl8192fu_power_off(priv: Rtl8xxxuPriv) -> None:
    """Return values are ignored, as in the C (8192f.c:1857)."""
    rtl8xxxu_flush_fifo(priv)
    rtl8xxxu_write8_clear(priv, C.REG_TX_REPORT_CTRL, C.TX_REPORT_CTRL_TIMER_ENABLE)
    rtl8xxxu_write8(priv, C.REG_CR, 0x00)

    try:
        rtl8192fu_active_to_lps(priv)
    except OSError:
        pass

    if rtl8xxxu_read8(priv, C.REG_MCU_FW_DL) & C.MCU_FW_RAM_SEL:
        rtl8xxxu_firmware_self_reset(priv)

    rtl8xxxu_write16_clear(priv, C.REG_SYS_FUNC, C.SYS_FUNC_CPU_ENABLE)
    rtl8xxxu_write8(priv, C.REG_MCU_FW_DL, 0x00)

    try:
        rtl8192fu_active_to_emu(priv)
    except OSError:
        pass
    rtl8192fu_emu_to_disabled(priv)


def rtl8192f_reset_8051(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8_clear(priv, C.REG_RSV_CTRL, C.BIT(1))
    rtl8xxxu_write8_clear(priv, C.REG_RSV_CTRL + 1, C.BIT(0))
    rtl8xxxu_write16_clear(priv, C.REG_SYS_FUNC, C.SYS_FUNC_CPU_ENABLE)
    rtl8xxxu_write8_clear(priv, C.REG_RSV_CTRL, C.BIT(1))
    rtl8xxxu_write8_set(priv, C.REG_RSV_CTRL + 1, C.BIT(0))
    rtl8xxxu_write16_set(priv, C.REG_SYS_FUNC, C.SYS_FUNC_CPU_ENABLE)


def rtl8192f_enable_rf(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8(priv, C.REG_RF_CTRL, C.RF_ENABLE | C.RF_RSTB | C.RF_SDMRSTB)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_TRX_PATH_ENABLE)
    val32 &= ~(C.OFDM_RF_PATH_RX_MASK | C.OFDM_RF_PATH_TX_MASK)
    val32 |= C.OFDM_RF_PATH_RX_A | C.OFDM_RF_PATH_RX_B | C.OFDM_RF_PATH_TX_A | C.OFDM_RF_PATH_TX_B
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, val32)

    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0x00)


def rtl8192f_disable_rf(priv: Rtl8xxxuPriv) -> None:
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_TRX_PATH_ENABLE)
    val32 &= ~C.OFDM_RF_PATH_TX_MASK
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, val32)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_AC, 0)


def rtl8192f_usb_quirks(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_gen2_usb_quirks(priv)
    val16 = rtl8xxxu_read16(priv, C.REG_CR)
    val16 |= C.CR_MAC_TX_ENABLE | C.CR_MAC_RX_ENABLE
    rtl8xxxu_write16(priv, C.REG_CR, val16)


def rtl8192f_set_crystal_cap(priv: Rtl8xxxuPriv, crystal_cap: int) -> None:
    cfo = priv.cfo_tracking
    if crystal_cap == cfo.crystal_cap:
        return

    xtal1 = rtl8xxxu_read32(priv, C.REG_AFE_PLL_CTRL)
    xtal0 = rtl8xxxu_read32(priv, C.REG_AFE_XTAL_CTRL)
    xtal1 = u32_replace_bits(xtal1, crystal_cap, XTAL1)
    xtal0 = u32_replace_bits(xtal0, crystal_cap, XTAL0)
    rtl8xxxu_write32(priv, C.REG_AFE_PLL_CTRL, xtal1)
    rtl8xxxu_write32(priv, C.REG_AFE_XTAL_CTRL, xtal0)

    cfo.crystal_cap = crystal_cap


def rtl8192fu_led_brightness_set(priv: Rtl8xxxuPriv, brightness: int) -> None:
    rtl8xxxu_write32(priv, C.REG_SW_GPIO_SHARE_CTRL_0, 0x20080)
    rtl8xxxu_write32(priv, C.REG_SW_GPIO_SHARE_CTRL_1, 0x1B0000)

    ledcfg = rtl8xxxu_read32(priv, C.REG_LEDCFG0)
    ledcfg = u32_replace_bits(ledcfg, C.LED_GPIO_ENABLE, C.LEDCFG0_LED2EN)
    ledcfg = u32_replace_bits(ledcfg, C.LED_IO_MODE_OUTPUT, C.LEDCFG0_LED0_IO_MODE)
    ledcfg = u32_replace_bits(ledcfg, C.LED_IO_MODE_OUTPUT, C.LEDCFG0_LED1_IO_MODE)

    mode_value = {
        LED_OFF: (C.LED_MODE_SW_CTRL, C.LED_SW_OFF),
        LED_ON: (C.LED_MODE_SW_CTRL, C.LED_SW_ON),
        C.RTL8XXXU_HW_LED_CONTROL: (C.LED_MODE_TX_OR_RX_EVENTS, C.LED_SW_OFF),
    }.get(brightness)
    if mode_value is not None:
        mode, value = mode_value
        ledcfg = u32_replace_bits(ledcfg, mode, C.LEDCFG0_LED0CM)
        ledcfg = u32_replace_bits(ledcfg, value, C.LEDCFG0_LED0SV)
        ledcfg = u32_replace_bits(ledcfg, mode, C.LEDCFG0_LED1CM)
        ledcfg = u32_replace_bits(ledcfg, value, C.LEDCFG0_LED1SV)

    rtl8xxxu_write32(priv, C.REG_LEDCFG0, ledcfg)


rtl8192fu_fops = Rtl8xxxuFileops(
    identify_chip=rtl8192fu_identify_chip,
    parse_efuse=rtl8192fu_parse_efuse,
    power_on=rtl8192fu_power_on,
    power_off=rtl8192fu_power_off,
    reset_8051=rtl8192f_reset_8051,
    init_phy_bb=rtl8192fu_init_phy_bb,
    init_phy_rf=rtl8192fu_init_phy_rf,
    phy_lc_calibrate=rtl8192f_phy_lc_calibrate,
    phy_iq_calibrate=rtl8192fu_phy_iq_calibrate,
    config_channel=rtl8192fu_config_channel,
    init_aggregation=rtl8192fu_init_aggregation,
    enable_rf=rtl8192f_enable_rf,
    disable_rf=rtl8192f_disable_rf,
    usb_quirks=rtl8192f_usb_quirks,
    set_tx_power=rtl8192f_set_tx_power,
    set_crystal_cap=rtl8192f_set_crystal_cap,
    led_classdev_brightness_set=rtl8192fu_led_brightness_set,
    mactable=RTL8192F_MAC_INIT_TABLE,
    writeN_block_size=254,
    trxff_boundary=0x3F3F,
    pbp_rx=C.PBP_PAGE_SIZE_256,
    pbp_tx=C.PBP_PAGE_SIZE_256,
    total_page_num=C.TX_TOTAL_PAGE_NUM_8192F,
    page_num_hi=C.TX_PAGE_NUM_HI_PQ_8192F,
    page_num_lo=C.TX_PAGE_NUM_LO_PQ_8192F,
    page_num_norm=C.TX_PAGE_NUM_NORM_PQ_8192F,
    ampdu_max_time=0x5E,
    ustime_tsf_edca=0x50,
    max_aggr_num=0x1F1F,
    has_tx_report=True,
    gen2_thermal_meter=True,
    needs_full_init=True,
    init_reg_rxfltmap=True,
    init_reg_pkt_life_time=True,
    init_reg_hmtfr=True,
)
