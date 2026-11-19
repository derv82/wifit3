"""RTL8192EU chip-specific fops functions (rtl8xxxu_8192e.c v6.6).

Holds the rtl8192eu_*/rtl8192e_* members of rtl8192eu_fops (8192e.c:1737). The shared gen2 members
and the register helpers live in core.py; IQ calibration in iqk.py.
"""
from __future__ import annotations

import time

from . import constants as C
from . import tables as T
from .core import (
    RF_A,
    RF_B,
    Rtl8xxxuFileops,
    Rtl8xxxuPriv,
    _ffs,
    rtl8723a_phy_lc_calibrate,
    rtl8723a_set_crystal_cap,
    rtl8xxxu_auto_llt_table,
    rtl8xxxu_config_endpoints_no_sie,
    rtl8xxxu_config_endpoints_sie,
    rtl8xxxu_firmware_self_reset,
    rtl8xxxu_flush_fifo,
    rtl8xxxu_gen2_channel_to_group,
    rtl8xxxu_gen2_config_channel,
    rtl8xxxu_gen2_disable_rf,
    rtl8xxxu_gen2_report_connect,
    rtl8xxxu_gen2_report_rssi,
    rtl8xxxu_gen2_update_rate_mask,
    rtl8xxxu_gen2_usb_quirks,
    rtl8xxxu_identify_vendor_2bits,
    rtl8xxxu_init_phy_regs,
    rtl8xxxu_init_rf_regs,
    rtl8xxxu_read8,
    rtl8xxxu_read16,
    rtl8xxxu_read32,
    rtl8xxxu_read_efuse,
    rtl8xxxu_reset_8051,
    rtl8xxxu_write8,
    rtl8xxxu_write16,
    rtl8xxxu_write32,
)
from .iqk import rtl8192eu_phy_iq_calibrate
from .rx import rtl8192e_cck_rssi, rtl8723au_rx_parse_phystats, rtl8xxxu_parse_rxdesc24
from .tx import rtl8xxxu_fill_txdesc_v2

RTL8723B_TX_COUNT = 4  # rtl8xxxu.h
_EFUSE_RTL_ID = 0x8129  # rtl8192eu_parse_efuse rejects any other id (8192e.c:1540)

# struct rtl8192eu_efuse byte offsets (rtl8xxxu.h)
_OFF_RTL_ID = 0x00
_OFF_TX_POWER_A = 0x10
_OFF_TX_POWER_B = 0x3a
_OFF_XTAL_K = 0xb9
_OFF_MAC_ADDR = 0xd7
# struct rtl8192eu_efuse_tx_power sub-offsets, relative to a tx_power base
_TP_CCK_BASE = 0x00      # cck_base[6]
_TP_HT40_BASE = 0x06     # ht40_base[5]
_TP_HT20_OFDM_1S_DIFF = 0x0b  # struct rtl8723au_idx (1 byte)
_TP_PWR_DIFF = 0x0c      # struct rtl8723bu_pwr_idx pwr_diff[3] (2 bytes each)


def _s4(nibble: int) -> int:
    """A signed 4-bit field (int:4 in the efuse bitfields)."""
    nibble &= 0xf
    return nibble - 0x10 if nibble & 0x8 else nibble


def _u32_get_bits(value: int, mask: int) -> int:
    return (value & mask) >> _ffs(mask)


def rtl8192eu_load_firmware(priv: Rtl8xxxuPriv) -> str:
    """8192e.c:1727. The firmware asset the driver uploads (rtlwifi/rtl8192eu_nic.bin)."""
    return "rtl8192eu_nic.bin"


def rtl8192eu_identify_chip(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1509. Raises for an unsupported test chip."""
    sys_cfg = rtl8xxxu_read32(priv, C.REG_SYS_CFG)
    priv.chip_cut = _u32_get_bits(sys_cfg, C.SYS_CFG_CHIP_VERSION_MASK)
    if sys_cfg & C.SYS_CFG_TRP_VAUX_EN:
        raise ValueError("Unsupported test chip")

    bonding = rtl8xxxu_read32(priv, C.REG_HPON_FSM) & C.HPON_FSM_BONDING_MASK
    if bonding == C.HPON_FSM_BONDING_1T2R:
        priv.chip_name = "8191EU"
        priv.tx_paths = 1
    else:
        priv.chip_name = "8192EU"
        priv.tx_paths = 2
    priv.rf_paths = 2
    priv.rx_paths = 2
    priv.has_wifi = True

    rtl8xxxu_identify_vendor_2bits(priv, sys_cfg & C.SYS_CFG_VENDOR_EXT_MASK)

    val32 = rtl8xxxu_read32(priv, C.REG_GPIO_OUTSTS)
    priv.rom_rev = _u32_get_bits(val32, C.GPIO_RF_RL_ID)

    rtl8xxxu_config_endpoints_sie(priv)
    if not priv.ep_tx_count:   # fallback for devices without REG_NORMAL_SIE_EP_TX
        rtl8xxxu_config_endpoints_no_sie(priv)


def rtl8192eu_parse_efuse(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1536. Decode struct rtl8192eu_efuse from the 512-byte efuse map. Raises on a bad id."""
    efuse = priv.efuse_wifi
    if int.from_bytes(efuse[_OFF_RTL_ID:_OFF_RTL_ID + 2], "little") != _EFUSE_RTL_ID:
        raise ValueError("RTL8192EU efuse: unexpected rtl_id")

    priv.mac_addr = bytes(efuse[_OFF_MAC_ADDR:_OFF_MAC_ADDR + 6])

    a, b = _OFF_TX_POWER_A, _OFF_TX_POWER_B
    priv.cck_tx_power_index_A = list(efuse[a + _TP_CCK_BASE:a + _TP_CCK_BASE + 6])
    priv.cck_tx_power_index_B = list(efuse[b + _TP_CCK_BASE:b + _TP_CCK_BASE + 6])
    priv.ht40_1s_tx_power_index_A = list(efuse[a + _TP_HT40_BASE:a + _TP_HT40_BASE + 5])
    priv.ht40_1s_tx_power_index_B = list(efuse[b + _TP_HT40_BASE:b + _TP_HT40_BASE + 5])

    ofdm = [(0, 0)] * RTL8723B_TX_COUNT
    ht20 = [(0, 0)] * RTL8723B_TX_COUNT
    ht40 = [(0, 0)] * RTL8723B_TX_COUNT
    # index 0: ht20 diff comes from ht20_ofdm_1s_diff.b; ht40[0] stays 0 (8192e.c:1561)
    ht20[0] = (_s4(efuse[a + _TP_HT20_OFDM_1S_DIFF] >> 4), _s4(efuse[b + _TP_HT20_OFDM_1S_DIFF] >> 4))
    for i in range(1, RTL8723B_TX_COUNT):
        a_lo, a_hi = efuse[a + _TP_PWR_DIFF + (i - 1) * 2], efuse[a + _TP_PWR_DIFF + (i - 1) * 2 + 1]
        b_lo, b_hi = efuse[b + _TP_PWR_DIFF + (i - 1) * 2], efuse[b + _TP_PWR_DIFF + (i - 1) * 2 + 1]
        # struct rtl8723bu_pwr_idx (LE): byte0 = ht20:4 | ht40:4, byte1 = ofdm:4 | cck:4
        ofdm[i] = (_s4(a_hi & 0xf), _s4(b_hi & 0xf))
        ht20[i] = (_s4(a_lo & 0xf), _s4(b_lo & 0xf))
        ht40[i] = (_s4(a_lo >> 4), _s4(b_lo >> 4))
    priv.ofdm_tx_power_diff = ofdm
    priv.ht20_tx_power_diff = ht20
    priv.ht40_tx_power_diff = ht40

    priv.default_crystal_cap = efuse[_OFF_XTAL_K] & 0x3f


# ---- Power sequencing (8192e.c) --------------------------------------------

def rtl8192e_crystal_afe_adjust(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1119. 40 MHz crystal source + 92e AFE PLL KVCO selection."""
    val8 = rtl8xxxu_read8(priv, C.REG_AFE_PLL_CTRL) & 0xfb
    rtl8xxxu_write8(priv, C.REG_AFE_PLL_CTRL, val8)
    val32 = rtl8xxxu_read32(priv, C.REG_AFE_CTRL4) & 0xfffffc7f
    rtl8xxxu_write32(priv, C.REG_AFE_CTRL4, val32)
    val8 = rtl8xxxu_read8(priv, C.REG_AFE_PLL_CTRL) & 0xbf
    rtl8xxxu_write8(priv, C.REG_AFE_PLL_CTRL, val8)
    val32 = rtl8xxxu_read32(priv, C.REG_AFE_CTRL4) & 0xffdfffff
    rtl8xxxu_write32(priv, C.REG_AFE_CTRL4, val32)


def rtl8192e_disabled_to_emu(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1156. Clear suspend-enable and power-down-enable."""
    val8 = rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 1) & ~(C.BIT(3) | C.BIT(4))
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 1, val8)


def rtl8192e_emu_to_active(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1167. Bring the power state from emulation to active. Raises on timeout."""
    val8 = rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 1) & ~C.BIT(7)   # disable HWPDN
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 1, val8)
    val8 = rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 1) & ~C.BIT(2)   # disable SW LPS
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 1, val8)
    val8 = rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 1) & ~(C.BIT(3) | C.BIT(4))  # disable WL suspend
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 1, val8)

    for _ in range(C.RTL8XXXU_MAX_REG_POLL):   # wait power ready 0x04[17]
        if rtl8xxxu_read32(priv, C.REG_APS_FSMCO) & C.BIT(17):
            break
        time.sleep(10e-6)
    else:
        raise ValueError("RTL8192EU emu_to_active: power-ready timed out")

    val8 = rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 2) | C.BIT(0)   # release WLON reset
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 2, val8)

    val32 = rtl8xxxu_read32(priv, C.REG_APS_FSMCO) | C.APS_FSMCO_MAC_ENABLE
    rtl8xxxu_write32(priv, C.REG_APS_FSMCO, val32)
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if (rtl8xxxu_read32(priv, C.REG_APS_FSMCO) & C.APS_FSMCO_MAC_ENABLE) == 0:
            break
        time.sleep(10e-6)
    else:
        raise ValueError("RTL8192EU emu_to_active: MAC-enable timed out")


def rtl8192eu_power_on(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1332. LDO/AFE bring-up, emu->active, then enable the MAC DMA/WMAC/SEC blocks."""
    val32 = rtl8xxxu_read32(priv, C.REG_SYS_CFG)
    if val32 & C.SYS_CFG_SPS_LDO_SEL:
        rtl8xxxu_write8(priv, C.REG_LDO_SW_CTRL, 0xc3)
    else:
        val32 = rtl8xxxu_read32(priv, C.REG_8192E_LDOV12_CTRL)
        val32 &= 0xff0fffff
        val32 |= 0x00500000
        rtl8xxxu_write32(priv, C.REG_8192E_LDOV12_CTRL, val32)
        rtl8xxxu_write8(priv, C.REG_LDO_SW_CTRL, 0x83)

    rtl8192e_crystal_afe_adjust(priv)
    rtl8192e_disabled_to_emu(priv)
    rtl8192e_emu_to_active(priv)

    rtl8xxxu_write16(priv, C.REG_CR, 0x0000)
    val16 = rtl8xxxu_read16(priv, C.REG_CR)
    val16 |= (C.CR_HCI_TXDMA_ENABLE | C.CR_HCI_RXDMA_ENABLE |
              C.CR_TXDMA_ENABLE | C.CR_RXDMA_ENABLE |
              C.CR_PROTOCOL_ENABLE | C.CR_SCHEDULE_ENABLE |
              C.CR_MAC_TX_ENABLE | C.CR_MAC_RX_ENABLE |
              C.CR_SECURITY_ENABLE | C.CR_CALTIMER_ENABLE)
    rtl8xxxu_write16(priv, C.REG_CR, val16)


def rtl8192eu_active_to_lps(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1211. Pause TX, wait the scheduler idle, reset BB + MAC TRX."""
    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0xff)
    for _ in range(101):   # poll REG_SCH_TX_CMD for no pending TX
        if not rtl8xxxu_read32(priv, C.REG_SCH_TX_CMD):
            break

    val8 = rtl8xxxu_read8(priv, C.REG_SYS_FUNC) & ~C.SYS_FUNC_BBRSTB
    rtl8xxxu_write8(priv, C.REG_SYS_FUNC, val8)
    time.sleep(2e-6)
    val8 = rtl8xxxu_read8(priv, C.REG_SYS_FUNC) & ~C.SYS_FUNC_BB_GLB_RSTN
    rtl8xxxu_write8(priv, C.REG_SYS_FUNC, val8)

    val16 = rtl8xxxu_read16(priv, C.REG_CR) & 0xff00
    val16 |= (C.CR_HCI_TXDMA_ENABLE | C.CR_HCI_RXDMA_ENABLE)
    rtl8xxxu_write16(priv, C.REG_CR, val16)
    val16 = rtl8xxxu_read16(priv, C.REG_CR) & ~C.CR_SECURITY_ENABLE
    rtl8xxxu_write16(priv, C.REG_CR, val16)

    val8 = rtl8xxxu_read8(priv, C.REG_DUAL_TSF_RST) | C.DUAL_TSF_TX_OK
    rtl8xxxu_write8(priv, C.REG_DUAL_TSF_RST, val8)


def rtl8192eu_active_to_emu(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1264. Turn off RF, DPDT select, disable the MAC via the HW state machine."""
    val8 = rtl8xxxu_read8(priv, C.REG_RF_CTRL) & ~C.RF_ENABLE
    rtl8xxxu_write8(priv, C.REG_RF_CTRL, val8)
    val8 = rtl8xxxu_read8(priv, C.REG_LEDCFG2) & ~C.LEDCFG2_DPDT_SELECT
    rtl8xxxu_write8(priv, C.REG_LEDCFG2, val8)
    val8 = rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 1) | C.BIT(1)
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 1, val8)
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if (rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 1) & C.BIT(1)) == 0:
            break
        time.sleep(10e-6)
    else:
        raise ValueError("RTL8192EU active_to_emu: disabling MAC timed out")


def rtl8192eu_emu_to_disabled(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1305. Enable WL suspend."""
    val8 = rtl8xxxu_read8(priv, C.REG_APS_FSMCO + 1) & ~(C.BIT(3) | C.BIT(4))
    val8 |= C.BIT(3)
    rtl8xxxu_write8(priv, C.REG_APS_FSMCO + 1, val8)


def rtl8192eu_power_off(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1370. Teardown: flush FIFO, RF off, LPS, reset firmware/MCU, back to disabled."""
    rtl8xxxu_flush_fifo(priv)

    val8 = rtl8xxxu_read8(priv, C.REG_TX_REPORT_CTRL) & ~C.TX_REPORT_CTRL_TIMER_ENABLE
    rtl8xxxu_write8(priv, C.REG_TX_REPORT_CTRL, val8)

    rtl8xxxu_write8(priv, C.REG_RF_CTRL, 0x00)   # turn off RF
    rtl8192eu_active_to_lps(priv)

    if rtl8xxxu_read8(priv, C.REG_MCU_FW_DL) & C.MCU_FW_RAM_SEL:
        rtl8xxxu_firmware_self_reset(priv)

    val16 = rtl8xxxu_read16(priv, C.REG_SYS_FUNC) & ~C.SYS_FUNC_CPU_ENABLE
    rtl8xxxu_write16(priv, C.REG_SYS_FUNC, val16)
    rtl8xxxu_write8(priv, C.REG_MCU_FW_DL, 0x00)

    rtl8xxxu_reset_8051(priv)
    rtl8192eu_active_to_emu(priv)
    rtl8192eu_emu_to_disabled(priv)


# ---- PHY / RF table init (8192e.c) -----------------------------------------

def rtl8192eu_init_phy_bb(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:674. Enable BB/RF, then run the PHY and AGC tables."""
    val16 = rtl8xxxu_read16(priv, C.REG_SYS_FUNC)
    val16 |= C.SYS_FUNC_BB_GLB_RSTN | C.SYS_FUNC_BBRSTB | C.SYS_FUNC_DIO_RF
    rtl8xxxu_write16(priv, C.REG_SYS_FUNC, val16)

    rtl8xxxu_write8(priv, C.REG_RF_CTRL, C.RF_ENABLE | C.RF_RSTB | C.RF_SDMRSTB)

    val16 = rtl8xxxu_read16(priv, C.REG_SYS_FUNC)
    val16 |= (C.SYS_FUNC_USBA | C.SYS_FUNC_USBD | C.SYS_FUNC_DIO_RF |
              C.SYS_FUNC_BB_GLB_RSTN | C.SYS_FUNC_BBRSTB)
    rtl8xxxu_write16(priv, C.REG_SYS_FUNC, val16)
    rtl8xxxu_write8(priv, C.REG_RF_CTRL, C.RF_ENABLE | C.RF_RSTB | C.RF_SDMRSTB)

    rtl8xxxu_init_phy_regs(priv, T.RTL8192EU_PHY_INIT_TABLE)

    if priv.hi_pa:
        rtl8xxxu_init_phy_regs(priv, T.RTL8XXX_AGC_8192EU_HIGHPA_TABLE)
    else:
        rtl8xxxu_init_phy_regs(priv, T.RTL8XXX_AGC_8192EU_STD_TABLE)


def rtl8192eu_init_phy_rf(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:710. Run the radio A and radio B init tables."""
    rtl8xxxu_init_rf_regs(priv, T.RTL8192EU_RADIOA_INIT_TABLE, RF_A)
    rtl8xxxu_init_rf_regs(priv, T.RTL8192EU_RADIOB_INIT_TABLE, RF_B)


# ---- RF enable + LED (8192e.c) ---------------------------------------------

LED_OFF = 0   # enum led_brightness
LED_ON = 1


def rtl8192e_enable_rf(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1442. RFE/antenna/PTA setup; clears TXPAUSE to fix 8192e TX."""
    val32 = rtl8xxxu_read32(priv, C.REG_RX_WAIT_CCA) | (C.BIT(22) | C.BIT(23))
    rtl8xxxu_write32(priv, C.REG_RX_WAIT_CCA, val32)

    val8 = rtl8xxxu_read8(priv, C.REG_GPIO_MUXCFG) | C.BIT(5)
    rtl8xxxu_write8(priv, C.REG_GPIO_MUXCFG, val8)

    rtl8xxxu_write8(priv, C.REG_WLAN_ACT_CONTROL_8723B, 0x04)   # WLAN action by PTA

    val32 = rtl8xxxu_read32(priv, C.REG_PWR_DATA) | C.PWR_DATA_EEPRPAD_RFE_CTRL_EN
    rtl8xxxu_write32(priv, C.REG_PWR_DATA, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_RFE_BUFFER) | (C.BIT(0) | C.BIT(1))
    rtl8xxxu_write32(priv, C.REG_RFE_BUFFER, val32)

    rtl8xxxu_write8(priv, C.REG_RFE_CTRL_ANTA_SRC, 0x77)

    val32 = rtl8xxxu_read32(priv, C.REG_LEDCFG0)
    val32 &= ~C.BIT(24)
    val32 |= C.BIT(23)
    rtl8xxxu_write32(priv, C.REG_LEDCFG0, val32)

    val8 = rtl8xxxu_read8(priv, C.REG_PAD_CTRL1) & ~C.BIT(0)   # external switch Main->S1, Aux->S0
    rtl8xxxu_write8(priv, C.REG_PAD_CTRL1, val8)

    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0x00)   # fix 8192e transmission failure


def rtl8192e_set_tx_power(priv: Rtl8xxxuPriv, channel: int, ht40: bool) -> None:
    """8192e.c:764. Program per-group CCK/OFDM/MCS TX power from the efuse indices."""
    tx_idx = 0
    group = rtl8xxxu_gen2_channel_to_group(channel)

    cck = priv.cck_tx_power_index_A[group] & 0xff
    val32 = rtl8xxxu_read32(priv, C.REG_TX_AGC_A_CCK1_MCS32) & 0xffff00ff
    val32 |= (cck << 8)
    rtl8xxxu_write32(priv, C.REG_TX_AGC_A_CCK1_MCS32, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_TX_AGC_B_CCK11_A_CCK2_11) & 0xff
    val32 |= ((cck << 8) | (cck << 16) | (cck << 24))
    rtl8xxxu_write32(priv, C.REG_TX_AGC_B_CCK11_A_CCK2_11, val32)

    ofdmbase = (priv.ht40_1s_tx_power_index_A[group] + priv.ofdm_tx_power_diff[tx_idx][0]) & 0xff
    ofdm = ofdmbase | ofdmbase << 8 | ofdmbase << 16 | ofdmbase << 24
    rtl8xxxu_write32(priv, C.REG_TX_AGC_A_RATE18_06, ofdm)
    rtl8xxxu_write32(priv, C.REG_TX_AGC_A_RATE54_24, ofdm)

    mcsbase = priv.ht40_1s_tx_power_index_A[group]
    diff = priv.ht40_tx_power_diff[tx_idx][0] if ht40 else priv.ht20_tx_power_diff[tx_idx][0]
    tx_idx += 1
    mcsbase = (mcsbase + diff) & 0xff
    mcs = mcsbase | mcsbase << 8 | mcsbase << 16 | mcsbase << 24
    for reg in (C.REG_TX_AGC_A_MCS03_MCS00, C.REG_TX_AGC_A_MCS07_MCS04,
                C.REG_TX_AGC_A_MCS11_MCS08, C.REG_TX_AGC_A_MCS15_MCS12):
        rtl8xxxu_write32(priv, reg, mcs)

    if priv.tx_paths > 1:
        cck = priv.cck_tx_power_index_B[group] & 0xff
        val32 = rtl8xxxu_read32(priv, C.REG_TX_AGC_B_CCK1_55_MCS32) & 0xff
        val32 |= ((cck << 8) | (cck << 16) | (cck << 24))
        rtl8xxxu_write32(priv, C.REG_TX_AGC_B_CCK1_55_MCS32, val32)

        val32 = rtl8xxxu_read32(priv, C.REG_TX_AGC_B_CCK11_A_CCK2_11) & 0xffffff00
        val32 |= cck
        rtl8xxxu_write32(priv, C.REG_TX_AGC_B_CCK11_A_CCK2_11, val32)

        ofdmbase = (priv.ht40_1s_tx_power_index_B[group] + priv.ofdm_tx_power_diff[tx_idx][1]) & 0xff
        ofdm = ofdmbase | ofdmbase << 8 | ofdmbase << 16 | ofdmbase << 24
        rtl8xxxu_write32(priv, C.REG_TX_AGC_B_RATE18_06, ofdm)
        rtl8xxxu_write32(priv, C.REG_TX_AGC_B_RATE54_24, ofdm)

        mcsbase = priv.ht40_1s_tx_power_index_B[group]
        diff = priv.ht40_tx_power_diff[tx_idx][1] if ht40 else priv.ht20_tx_power_diff[tx_idx][1]
        tx_idx += 1
        mcsbase = (mcsbase + diff) & 0xff
        mcs = mcsbase | mcsbase << 8 | mcsbase << 16 | mcsbase << 24
        for reg in (C.REG_TX_AGC_B_MCS03_MCS00, C.REG_TX_AGC_B_MCS07_MCS04,
                    C.REG_TX_AGC_B_MCS11_MCS08, C.REG_TX_AGC_B_MCS15_MCS12):
            rtl8xxxu_write32(priv, reg, mcs)


def rtl8192eu_led_brightness_set(priv: Rtl8xxxuPriv, brightness: int) -> None:
    """8192e.c:1486. LED_OFF, LED_ON, or RTL8XXXU_HW_LED_CONTROL via REG_LEDCFG1."""
    ledcfg = rtl8xxxu_read8(priv, C.REG_LEDCFG1)
    if brightness == LED_OFF:
        ledcfg &= ~C.LEDCFG1_HW_LED_CONTROL
        ledcfg |= C.LEDCFG1_LED_DISABLE
    elif brightness == LED_ON:
        ledcfg &= ~(C.LEDCFG1_HW_LED_CONTROL | C.LEDCFG1_LED_DISABLE)
    elif brightness == C.RTL8XXXU_HW_LED_CONTROL:
        ledcfg &= ~C.LEDCFG1_LED_DISABLE
        ledcfg |= C.LEDCFG1_HW_LED_CONTROL
    rtl8xxxu_write8(priv, C.REG_LEDCFG1, ledcfg)


# struct rtl8xxxu_fileops rtl8192eu_fops (8192e.c:1737)
rtl8192eu_fops = Rtl8xxxuFileops(
    identify_chip=rtl8192eu_identify_chip,
    parse_efuse=rtl8192eu_parse_efuse,
    load_firmware=rtl8192eu_load_firmware,
    power_on=rtl8192eu_power_on,
    power_off=rtl8192eu_power_off,
    read_efuse=rtl8xxxu_read_efuse,
    reset_8051=rtl8xxxu_reset_8051,
    llt_init=rtl8xxxu_auto_llt_table,
    init_phy_bb=rtl8192eu_init_phy_bb,
    init_phy_rf=rtl8192eu_init_phy_rf,
    phy_lc_calibrate=rtl8723a_phy_lc_calibrate,
    phy_iq_calibrate=rtl8192eu_phy_iq_calibrate,
    config_channel=rtl8xxxu_gen2_config_channel,
    parse_rx_desc=rtl8xxxu_parse_rxdesc24,
    parse_phystats=rtl8723au_rx_parse_phystats,
    enable_rf=rtl8192e_enable_rf,
    disable_rf=rtl8xxxu_gen2_disable_rf,
    usb_quirks=rtl8xxxu_gen2_usb_quirks,
    set_tx_power=rtl8192e_set_tx_power,
    update_rate_mask=rtl8xxxu_gen2_update_rate_mask,
    report_connect=rtl8xxxu_gen2_report_connect,
    report_rssi=rtl8xxxu_gen2_report_rssi,
    fill_txdesc=rtl8xxxu_fill_txdesc_v2,
    set_crystal_cap=rtl8723a_set_crystal_cap,
    cck_rssi=rtl8192e_cck_rssi,
    led_classdev_brightness_set=rtl8192eu_led_brightness_set,
    writeN_block_size=128,
    tx_desc_size=40,                # sizeof(struct rtl8xxxu_txdesc40)
    rx_desc_size=24,                # sizeof(struct rtl8xxxu_rxdesc24)
    has_s0s1=False,
    gen2_thermal_meter=True,
    needs_full_init=True,
    supports_ap=True,
    max_macid_num=128,
    adda_1t_init=0x0fc01616,
    adda_1t_path_on=0x0fc01616,
    adda_2t_path_on_a=0x0fc01616,
    adda_2t_path_on_b=0x0fc01616,
    trxff_boundary=0x3cff,
    mactable=T.RTL8192E_MAC_INIT_TABLE,
    total_page_num=C.TX_TOTAL_PAGE_NUM_8192E,
    page_num_hi=C.TX_PAGE_NUM_HI_PQ_8192E,
    page_num_lo=C.TX_PAGE_NUM_LO_PQ_8192E,
    page_num_norm=C.TX_PAGE_NUM_NORM_PQ_8192E,
)
