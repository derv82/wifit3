"""RTL8188FTV channel tuning (2.4 GHz, 20 MHz).

Cleanroom port of:

* `rtl8xxxu_read_rfreg`       — `core.c:867-905` (HSSI 3-wire READ on path A)
* `rtl8xxxu_write_rfreg`      — `core.c:907-919` (LSSI single-op WRITE on path A)
* `rtl8188f_spur_calibration` — `8188f.c:400-512` (notch + CSI mask control)
* `rtl8188fu_config_channel`  — `8188f.c:514-643` (RF channel, BW, RF BW filters)

The 40 MHz branch is not ported; the driver only supports 2.4 GHz 20 MHz.
"""
from __future__ import annotations

import time

from .constants import (
    FPGA_RF_MODE,
    OFDM0_X_AGC_CORE1_IGI_MASK,
    REG_FPGA0_ANALOG4,
    REG_FPGA0_PSD_FUNC,
    REG_FPGA0_PSD_REPORT,
    REG_FPGA0_RF_MODE,
    REG_OFDM0_XA_AGC_CORE1,
    REG_FPGA0_XB_RF_INT_OE,
    REG_FPGA1_RF_MODE,
    REG_OFDM0_RX_D_SYNC_PATH,
    REG_OFDM0_TX_PSDO_NOISE_WEIGHT,
    REG_OFDM0_XA_RX_AFE,
    REG_OFDM_RX_DFIR,
    REG_OFDM1_CFO_TRACKING,
    REG_OFDM1_CSI_FIX_MASK1,
    REG_OFDM1_CSI_FIX_MASK2,
    REG_S0S1_PATH_SWITCH,
    RF6052_REG_GAIN_CCA,
    RF6052_REG_MODE_AG,
    RF6052_REG_RX_BB2,
    RF6052_REG_RX_G2,
    RF6052_REG_RXG_MIX_SWBW,
)
from .phy import read_rfreg, write_rfreg
from .transport import RTL8188FTVTransport

# ---- spur calibration tables (8188f.c:402-426) ---------------------------

# Pre-spur frequencies per channel (nonzero → apply PSD notch).
_SPUR_FREQ: dict[int, int] = {
    5: 0xFCCD, 6: 0xFC4D, 7: 0xFFCD, 8: 0xFF4D,
    11: 0xFDCD, 13: 0xFCCD, 14: 0xFF9A,
}

_SPUR_REG_D40: dict[int, int] = {
    5: 0x06000000, 6: 0x00000600, 13: 0x06000000,
}

_SPUR_REG_D44: dict[int, int] = {11: 0x04000000}

_SPUR_REG_D4C: dict[int, int] = {
    7: 0x06000000, 8: 0x00000380, 14: 0x00180000,
}

_SPUR_THRESHOLD = 0x16


def _spur_calibration(t: RTL8188FTVTransport, channel: int) -> None:
    """Port of `rtl8188f_spur_calibration` (8188f.c:400-512).

    For channels with a nonzero spur frequency entry: notch-filters the PSD
    spur and optionally re-enables the CSI mask.  For all other channels (1-4,
    9-10, 12) the frequency entry is zero, the notch block is skipped, and
    the CSI mask is unconditionally disabled.
    """
    # Enable notch filter (REG_OFDM0_RX_D_SYNC_PATH bits 28:24 and bit 9)
    val32 = t.read32(REG_OFDM0_RX_D_SYNC_PATH)
    val32 |= 0x1F000000  # GENMASK(28, 24)
    t.write32(REG_OFDM0_RX_D_SYNC_PATH, val32)

    val32 = t.read32(REG_OFDM0_RX_D_SYNC_PATH)
    val32 |= 0x200  # BIT(9)
    t.write32(REG_OFDM0_RX_D_SYNC_PATH, val32)

    freq = _SPUR_FREQ.get(channel, 0)
    if channel <= 14 and freq > 0:
        reg948 = t.read32(REG_S0S1_PATH_SWITCH)
        hw_ctrl = bool(reg948 & (1 << 6))
        sw_ctrl = not hw_ctrl

        hw_ctrl_s1 = False
        sw_ctrl_s1 = False

        if hw_ctrl:
            val32 = t.read32(REG_FPGA0_XB_RF_INT_OE)
            val32 &= 0x00000078  # GENMASK(5, 3)
            hw_ctrl_s1 = val32 == (1 << 3)
        elif sw_ctrl:
            sw_ctrl_s1 = not bool(reg948 & (1 << 9))

        if hw_ctrl_s1 or sw_ctrl_s1:
            initial_gain = t.read32(REG_OFDM0_XA_AGC_CORE1)

            # Disable CCK block
            val32 = t.read32(REG_FPGA0_RF_MODE)
            val32 &= ~(1 << 24)  # ~FPGA_RF_MODE_CCK
            t.write32(REG_FPGA0_RF_MODE, val32)

            # Set initial gain on core1 (IGI → 0x30)
            val32 = (initial_gain & ~OFDM0_X_AGC_CORE1_IGI_MASK) | 0x30
            t.write32(REG_OFDM0_XA_AGC_CORE1, val32)

            # Disable 3-wire
            t.write32(REG_FPGA0_ANALOG4, 0xCCF000C0)

            # Setup PSD
            t.write32(REG_FPGA0_PSD_FUNC, freq)

            # Start PSD
            t.write32(REG_FPGA0_PSD_FUNC, 0x400000 | freq)

            time.sleep(0.030)

            do_notch = t.read32(REG_FPGA0_PSD_REPORT) >= _SPUR_THRESHOLD

            # Turn off PSD
            t.write32(REG_FPGA0_PSD_FUNC, freq)

            # Re-enable 3-wire
            t.write32(REG_FPGA0_ANALOG4, 0xCCC000C0)

            # Re-enable CCK block
            val32 = t.read32(REG_FPGA0_RF_MODE)
            val32 |= 1 << 24  # FPGA_RF_MODE_CCK
            t.write32(REG_FPGA0_RF_MODE, val32)

            # Restore initial gain
            t.write32(REG_OFDM0_XA_AGC_CORE1, initial_gain)

            if do_notch:
                t.write32(REG_OFDM1_CSI_FIX_MASK1, _SPUR_REG_D40[channel])
                t.write32(REG_OFDM1_CSI_FIX_MASK2, _SPUR_REG_D44[channel])
                t.write32(0x0D48, 0x0)
                t.write32(0x0D4C, _SPUR_REG_D4C[channel])

                # Enable CSI mask
                val32 = t.read32(REG_OFDM1_CFO_TRACKING)
                val32 |= 1 << 28
                t.write32(REG_OFDM1_CFO_TRACKING, val32)
                return

    # Disable CSI mask function
    val32 = t.read32(REG_OFDM1_CFO_TRACKING)
    val32 &= ~(1 << 28)
    t.write32(REG_OFDM1_CFO_TRACKING, val32)


def set_channel_2g_20mhz(t: RTL8188FTVTransport, channel: int) -> None:
    """Port of `rtl8188fu_config_channel` (8188f.c:514-643) for 20 MHz / 2.4 GHz.

    Sets the RF channel, runs spur calibration, configures BB bandwidth to
    20 MHz, and writes the RF TRX_BW + filter bandwidth registers.

    The 40 MHz branch is not ported.  This produces 36 ops against
    capture-1 for channel 1.
    """
    # Set channel in RF MODE_AG register
    val32 = read_rfreg(t, 0, RF6052_REG_MODE_AG)
    val32 &= ~0x03FF  # MODE_AG_CHANNEL_MASK
    val32 |= channel
    write_rfreg(t, 0, RF6052_REG_MODE_AG, val32)

    # Spur calibration (freq[1]==0 → notch block skipped, CSI mask disabled)
    _spur_calibration(t, channel)

    # ---- Set bandwidth mode to 20 MHz ------------------------------------

    # REG_FPGA0_RF_MODE: clear bit 0 (40 MHz), set nothing (20 MHz)
    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 &= ~FPGA_RF_MODE
    t.write32(REG_FPGA0_RF_MODE, val32)

    # REG_FPGA1_RF_MODE: same
    val32 = t.read32(REG_FPGA1_RF_MODE)
    val32 &= ~FPGA_RF_MODE
    t.write32(REG_FPGA1_RF_MODE, val32)

    # RXADC CLK: force bits 10:8 (20/40 MHz clock source)
    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 |= 0x00000700  # GENMASK(10, 8)
    t.write32(REG_FPGA0_RF_MODE, val32)

    # TXDAC CLK: bit14=1, bit12=1, bit13=0
    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 |= (1 << 14) | (1 << 12)
    val32 &= ~(1 << 13)
    t.write32(REG_FPGA0_RF_MODE, val32)

    # Small BW: clear bits 31:30
    val32 = t.read32(REG_OFDM0_TX_PSDO_NOISE_WEIGHT)
    val32 &= ~0xC0000000  # GENMASK(31, 30)
    t.write32(REG_OFDM0_TX_PSDO_NOISE_WEIGHT, val32)

    # ADC buffer clk (TX_PSDO_NOISE_WEIGHT): clear bit 29, set bit 28
    val32 = t.read32(REG_OFDM0_TX_PSDO_NOISE_WEIGHT)
    val32 &= ~(1 << 29)
    val32 |= 1 << 28
    t.write32(REG_OFDM0_TX_PSDO_NOISE_WEIGHT, val32)

    # ADC buffer clk (XA_RX_AFE): same pattern
    val32 = t.read32(REG_OFDM0_XA_RX_AFE)
    val32 &= ~(1 << 29)
    val32 |= 1 << 28
    t.write32(REG_OFDM0_XA_RX_AFE, val32)

    # DFIR: clear bit 19
    val32 = t.read32(REG_OFDM_RX_DFIR)
    val32 &= ~(1 << 19)
    t.write32(REG_OFDM_RX_DFIR, val32)

    # DFIR: set 20 MHz sampling (bit 21 = constant, bit 20 = 20 MHz, bit 22 = 40 MHz)
    val32 = t.read32(REG_OFDM_RX_DFIR)
    val32 &= ~0x00F00000  # GENMASK(23, 20)
    val32 |= 1 << 21
    val32 |= 1 << 20  # 20 MHz
    t.write32(REG_OFDM_RX_DFIR, val32)

    # ---- RF TRX_BW: channel + 20 MHz mode flag ---------------------------
    rf_bw = channel | 0x0C00  # MODE_AG_BW_20MHZ_8723B
    write_rfreg(t, 0, RF6052_REG_MODE_AG, rf_bw)

    # ---- FILTER BW & RC corner (ACPR) ------------------------------------
    write_rfreg(t, 0, RF6052_REG_RXG_MIX_SWBW, 0x00065)  # 20 MHz
    write_rfreg(t, 0, RF6052_REG_RX_BB2, 0x00000)        # 20 MHz

    # ---- RC corner (power) ------------------------------------------------
    write_rfreg(t, 0, RF6052_REG_GAIN_CCA, 0x00140)
    write_rfreg(t, 0, RF6052_REG_RX_G2, 0x01C6C)
