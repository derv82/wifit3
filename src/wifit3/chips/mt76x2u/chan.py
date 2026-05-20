"""MT76x2U high-level channel tune (20MHz, 2.4 GHz + 5 GHz UNII-1/UNII-3).

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors `mt76x2u_phy_set_channel` (mt76x2/usb_phy.c:60) with the following
deliberate simplifications appropriate for monitor-mode RX:

  - Skip TX-power configuration (`mt76x2_phy_set_txpower_regs/_set_txpower`).
  - Skip RX-gain table reads (`mt76x2_read_rx_gain`, `mt76x2_apply_gain_adj`).
  - Skip RXIQC/TX_LOFT/TX_SHAPING/TXIQ/RC/RXDCOC/LC/TEMP_SENSOR/R calibrations.
  - Skip TSSI compensation init.

If RX sensitivity is poor or the chip looks deaf at a specific channel,
these are the first things to add back.
"""
from __future__ import annotations

import asyncio
import logging

from .constants import (
    MT_BBP_AGC_R11,
    MT_BBP_AGC_R2,
    MT_BBP_AGC_R61,
    MT_BBP_AGC_R7,
    MT_BBP_RXO_R13,
    MT_BBP_TXO_R4_ADDR,
    MT_EXT_CCA_CFG,
    MT_EXT_CCA_CFG_CCA0_SHIFT,
    MT_EXT_CCA_CFG_CCA1_SHIFT,
    MT_EXT_CCA_CFG_CCA2_SHIFT,
    MT_EXT_CCA_CFG_CCA3_SHIFT,
    MT_EXT_CCA_CFG_CCA_MASK_SHIFT,
    MT_TXOP_CTRL_CFG,
    MT76XX_REV_E3,
)
from .eeprom import EE_VERSION   # noqa: F401  (kept for future use)
from .mcu import (
    MCU_CAL_LC,
    MCU_CAL_R,
    MCU_CAL_RC,
    MCU_CAL_RXDCOC,
    MCU_CAL_RXIQC_FI,
    McuChannel,
    mcu_calibrate,
)
from .phy import (
    mcu_init_gain,
    mcu_set_channel,
    phy_set_band,
    phy_set_bw_20mhz,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)


def _ext_cca_chan_group0() -> int:
    """`ext_cca_chan[0]` from mt76x2u_phy_set_channel — used for 20MHz / HT40+.

    bits: CCA0=0 CCA1=1 CCA2=2 CCA3=3 CCA_MASK=BIT(0).
    """
    return (
        (0 << MT_EXT_CCA_CFG_CCA0_SHIFT)
        | (1 << MT_EXT_CCA_CFG_CCA1_SHIFT)
        | (2 << MT_EXT_CCA_CFG_CCA2_SHIFT)
        | (3 << MT_EXT_CCA_CFG_CCA3_SHIFT)
        | ((1 << 0) << MT_EXT_CCA_CFG_CCA_MASK_SHIFT)
    )


CHANNELS_2G = list(range(1, 14))
# UNII-1 + UNII-3 (non-DFS). DFS bands (52..144) need radar detection support
# we don't ship.
CHANNELS_5G_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]


def _is_5ghz(channel: int) -> bool:
    return channel >= 36


async def set_channel_20mhz(transport: MT76x2UTransport, mcu: McuChannel,
                            channel: int, asic_rev: int, chainmask: int,
                            init_cal_done: bool = False,
                            bt_rcal_valid: bool = True) -> bool:
    """Tune to a 20MHz-bw channel. Caller is responsible for stopping +
    restarting MAC if calling from a running state — for the cold-bring-up
    sequence, this is called BEFORE mac_start.
    """
    band_5g = _is_5ghz(channel)
    bw = 0          # 20MHz
    bw_index = 0
    ext_chan = 0
    ch_group_index = 0
    scan = False

    # Pre-MCU writes.
    phy_set_band(transport, band_5g=band_5g, primary_upper=False)
    phy_set_bw_20mhz(transport, ctrl=ext_chan)

    # EXT_CCA_CFG (CCA priorities + mask).
    transport.rmw32(
        MT_EXT_CCA_CFG,
        # Mask of bits we're touching (mirrors kernel rmw):
        ((0x3 << MT_EXT_CCA_CFG_CCA0_SHIFT)
         | (0x3 << MT_EXT_CCA_CFG_CCA1_SHIFT)
         | (0x3 << MT_EXT_CCA_CFG_CCA2_SHIFT)
         | (0x3 << MT_EXT_CCA_CFG_CCA3_SHIFT)
         | (0xF << MT_EXT_CCA_CFG_CCA_MASK_SHIFT)),
        _ext_cca_chan_group0(),
    )

    # MCU CMDs: channel switch + init gain.
    if not await mcu_set_channel(mcu, channel, bw, bw_index, scan, chainmask):
        logger.error("mcu_set_channel(%d) failed", channel)
        return False
    # Brief settle (kernel: usleep_range(5000, 10000) between switch and init_gain).
    await asyncio.sleep(0.008)

    if not await mcu_init_gain(mcu, channel, gain=0, force=True):
        logger.error("mcu_init_gain(%d) failed", channel)
        return False

    # ---- Calibrations [SRC] mt76x2/usb_phy.c:147-159 ----------------------
    # ORDER matters: MCU_CAL_R (one-time) before RXDCOC; RXDCOC every switch;
    # MCU_CAL_RC (one-time) after RXDCOC.
    if not init_cal_done and bt_rcal_valid:
        if not await mcu_calibrate(mcu, MCU_CAL_R, 0):
            logger.warning("MCU_CAL_R failed (continuing)")
    if not await mcu_calibrate(mcu, MCU_CAL_RXDCOC, channel):
        logger.warning("MCU_CAL_RXDCOC(%d) failed (continuing)", channel)
    if not init_cal_done:
        if not await mcu_calibrate(mcu, MCU_CAL_RC, 0):
            logger.warning("MCU_CAL_RC failed (continuing)")

    # ---- Post-MCU BBP writes — [SRC] mt76x2/usb_phy.c:161 -----------------
    if asic_rev >= MT76XX_REV_E3:
        transport.rmw32(MT_BBP_RXO_R13, 1 << 10, 1 << 10)  # LDPC RX enable

    transport.write32(MT_BBP_AGC_R61, 0xff64a4e2)
    transport.write32(MT_BBP_AGC_R7, 0x08081010)
    transport.write32(MT_BBP_AGC_R11, 0x00000404)
    transport.write32(MT_BBP_AGC_R2, 0x00007070)
    transport.write32(MT_TXOP_CTRL_CFG, 0x04101b3f)

    transport.rmw32(MT_BBP_TXO_R4_ADDR, 1 << 25, 1 << 25)
    transport.rmw32(MT_BBP_RXO_R13, 1 << 8, 1 << 8)

    # ---- Per-channel sensitivity cals (mt76x2u_phy_channel_calibrate) -----
    # [SRC] mt76x2/usb_phy.c:10-40 — only the RX-relevant ones; we skip
    # TX_LOFT, TXIQ, TEMP_SENSOR, TX_SHAPING (those affect TX accuracy).
    if band_5g:
        if not await mcu_calibrate(mcu, MCU_CAL_LC, 0):
            logger.warning("MCU_CAL_LC(5GHz) failed (continuing)")
    if not await mcu_calibrate(mcu, MCU_CAL_RXIQC_FI, 1 if band_5g else 0):
        logger.warning("MCU_CAL_RXIQC_FI(band_5g=%s) failed (continuing)",
                       band_5g)

    return True
