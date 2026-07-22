"""MT76x2U high-level channel tune (20MHz, 2.4 GHz + 5 GHz UNII-1/UNII-3).

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Port of `mt76x2u_phy_set_channel` (mt76x2/usb_phy.c:60) and its
called helpers. Every kernel write to a chip register in the channel-tune
+ calibrate path is mirrored here — the TX-side programming (per-rate TX
power, PA mode, antenna-pin enable) is load-bearing for injection, not a
monitor-mode-RX nicety that can be skipped.

Order matches kernel `mt76x2u_phy_set_channel`:

  1. `mt76x2_phy_set_txpower_regs(band)`       — PA mode regs (phy.py)
  2. `mt76x2_configure_tx_delay(band, bw)`     — TX timing (phy.py)
  3. `mt76x2_phy_set_txpower()`                — per-rate TX power tables
     (driver reads EEPROM rate_power + power_info, hands them in)
  4. `mt76x02_phy_set_band(band)`              — TX_BAND_CFG
  5. `mt76x02_phy_set_bw(20MHz)`               — BBP CORE/AGC R0 BW
  6. `MT_EXT_CCA_CFG` priorities               — rmw
  7. `mt76x2_mcu_set_channel`                  — MCU channel switch
  8. `mt76x2_mcu_init_gain`                    — MCU init gain
  9. LDPC RX enable (E3+)                      — BBP RXO 13 bit 10
 10. `MCU_CAL_R` (one-time, bt_rcal_valid)
 11. `MCU_CAL_RXDCOC(channel)`                 — every switch
 12. `MCU_CAL_RC` (one-time)
 13. BBP post-MCU writes (AGC 61/7/11/2 +      — direct writes
     TXOP_CTRL_CFG + BBP TXO R4 / RXO R13)
 14. `mt76x2u_phy_channel_calibrate`:
       - 5 GHz: MCU_CAL_LC
       - MCU_CAL_TX_LOFT(band)
       - MCU_CAL_TXIQ(band)
       - MCU_CAL_RXIQC_FI(band)
       - MCU_CAL_TEMP_SENSOR
       - MCU_CAL_TX_SHAPING
       - `mt76x2_apply_gain_adj(high_gain)`    — phy.py
       - `mt76x02_edcca_init` (incl PA-LNA TX_PIN_CFG enable) — phy.py
 15. `mt76x02_init_agc_gain`                   — seed cal.agc_gain_init
 16. TSSI init (tssi_enabled gate):            — TX_ALC_CFG_1/2 + MCU_CAL_TSSI
 17. (scheduled separately on the driver) periodic recal task —
     `update_channel_gain` + `tssi_compensate` every ~1 s.

Permanently skipped (kernel paths wifit3 has no analog for):

  - DFS-channel AGC adjust (`mt76x02_phy_dfs_adjust_agc`) — wifit3 doesn't
    support DFS channels.
  - VHT / 40 MHz / 80 MHz bandwidth-dependent paths — we tune 20 MHz only;
    code keeps the `bw_40plus` parameter for kernel-fidelity but always
    passes False.
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
    MT_TX_ALC_CFG_1,
    MT_TX_ALC_CFG_1_TEMP_COMP_MASK,
    MT_TX_ALC_CFG_2,
    MT_TX_ALC_CFG_2_TEMP_COMP_MASK,
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
    MCU_CAL_TEMP_SENSOR,
    MCU_CAL_TSSI,
    MCU_CAL_TX_LOFT,
    MCU_CAL_TX_SHAPING,
    MCU_CAL_TXIQ,
    McuChannel,
    mcu_calibrate,
)
from .phy import (
    Mt76x2CalState,
    apply_gain_adj,
    edcca_init,
    init_agc_gain,
    mcu_init_gain,
    mcu_set_channel,
    phy_configure_tx_delay,
    phy_set_band,
    phy_set_bw_20mhz,
    phy_set_txpower,
    phy_set_txpower_regs,
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
                            *,
                            cal: Mt76x2CalState,
                            rate_power: dict,
                            power_info: dict,
                            init_cal_done: bool = False,
                            bt_rcal_valid: bool = True,
                            ext_pa: bool = False,
                            high_gain: tuple[int, int] = (0, 0),
                            tssi_enabled_flag: bool = False,
                            txpower_conf: int = 60,
                            set_txpower_enabled: bool = True,
                            scan: bool = False) -> bool:
    """Tune to a 20MHz-bw channel. Caller is responsible for stopping +
    restarting MAC if calling from a running state — for the cold-bring-up
    sequence, this is called BEFORE mac_start.

    ``ext_pa`` = EEPROM-derived external-PA enable for the target band
    (``!nic_conf_0.pa_int_2g`` or ``!nic_conf_0.pa_int_5g``). Drives the
    PA-mode / TX-delay register values per kernel `mt76x2_phy_set_txpower_regs`.

    ``high_gain`` = per-chain RX LNA gain offsets from EEPROM (kernel's
    ``dev->cal.rx.high_gain[0/1]``, populated by ``mt76x2_read_rx_gain``).
    Passed to ``apply_gain_adj`` at the end so the BBP AGC registers are
    tuned for this specific card's calibration data.
    """
    band_5g = _is_5ghz(channel)
    bw = 0          # 20MHz
    bw_index = 0
    ext_chan = 0
    # The kernel's ch_group_index (40/80 MHz channel-group selection for the
    # EXT_CCA_CFG rmw + phy_set_band primary_upper) is always 0 at 20 MHz, so
    # it is not represented here.

    # Pre-MCU writes. The PA / TX-delay programming MUST land before the
    # MCU channel-switch command so the chip's TX engine has the right RF
    # config when it starts emitting on the new channel.
    phy_set_txpower_regs(transport, band_2g=not band_5g, ext_pa=ext_pa)
    phy_configure_tx_delay(transport, ext_pa=ext_pa, bw=bw)
    if set_txpower_enabled:
        phy_set_txpower(transport, rate_power=rate_power, power_info=power_info,
                        txpower_conf=txpower_conf)
    else:
        logger.info("MT7612U: phy_set_txpower SKIPPED (gate=off, falling back "
                    "to static initvals 0x3a3a3a3a in MT_TX_PWR_CFG_0..9)")
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

    return True


async def phy_channel_calibrate(
    transport: MT76x2UTransport, mcu: McuChannel, channel: int, *,
    cal: Mt76x2CalState, high_gain: tuple[int, int],
    ext_pa: bool, tssi_enabled_flag: bool,
) -> None:
    """Heavy per-channel RF calibration — kernel `mt76x2u_phy_channel_calibrate`
    ([SRC] mt76x2/usb_phy.c:10-40) plus the inline `init_agc_gain` + TSSI seed
    that follow it in `mt76x2u_phy_set_channel`.

    The kernel runs this inline on a settle (and `if (scan) return 0` skips it
    on every hop, [SRC] usb_phy.c:170). It can block there — it's a workqueue.
    We can't block the UI loop, so the periodic cal task ([SRC] cal_work /
    `mt76x2u_phy_calibrate`, usb_phy.c:42) runs it in the background once we've
    settled, self-gated on `cal.channel_cal_done`.

    Skipping TX_LOFT/TXIQ/TX_SHAPING degrades TX modulation accuracy; skipping
    apply_gain_adj leaves the BBP AGC at MCU defaults (poor near-field RX);
    skipping edcca_init leaves MT_TX_PIN_CFG undriven (catastrophic TX atten).
    """
    band_5g = _is_5ghz(channel)
    band_arg = 1 if band_5g else 0
    if band_5g:
        if not await mcu_calibrate(mcu, MCU_CAL_LC, 0):
            logger.warning("MCU_CAL_LC(5GHz) failed (continuing)")
    if not await mcu_calibrate(mcu, MCU_CAL_TX_LOFT, band_arg):
        logger.warning("MCU_CAL_TX_LOFT failed (continuing)")
    if not await mcu_calibrate(mcu, MCU_CAL_TXIQ, band_arg):
        logger.warning("MCU_CAL_TXIQ failed (continuing)")
    if not await mcu_calibrate(mcu, MCU_CAL_RXIQC_FI, band_arg):
        logger.warning("MCU_CAL_RXIQC_FI failed (continuing)")
    if not await mcu_calibrate(mcu, MCU_CAL_TEMP_SENSOR, 0):
        logger.warning("MCU_CAL_TEMP_SENSOR failed (continuing)")
    if not await mcu_calibrate(mcu, MCU_CAL_TX_SHAPING, 0):
        logger.warning("MCU_CAL_TX_SHAPING failed (continuing)")

    apply_gain_adj(transport, high_gain)
    edcca_init(transport)
    cal.channel_cal_done = True

    # `mt76x02_init_agc_gain` — [SRC] mt76x02_phy.c:193. Seeds the per-chain
    # AGC base gain from the BBP regs the cals just programmed. The periodic
    # update_channel_gain loop diffs against this baseline.
    cal.agc_gain_init = init_agc_gain(transport)
    cal.agc_gain_cur = cal.agc_gain_init
    cal.low_gain = -1   # force a full gain re-tune on first update tick

    # TSSI compensation init block — [SRC] mt76x2/usb_phy.c:176-196. Only
    # fires if the EEPROM advertises TSSI; programs the temp-comp seed in
    # TX_ALC_CFG_1/2 and triggers MCU_CAL_TSSI so the chip's MCU has a
    # baseline thermal reading for the periodic tssi_compensate loop.
    if tssi_enabled_flag:
        transport.rmw32(
            MT_TX_ALC_CFG_1,
            MT_TX_ALC_CFG_1_TEMP_COMP_MASK,
            0x38 & MT_TX_ALC_CFG_1_TEMP_COMP_MASK,
        )
        transport.rmw32(
            MT_TX_ALC_CFG_2,
            MT_TX_ALC_CFG_2_TEMP_COMP_MASK,
            0x38 & MT_TX_ALC_CFG_2_TEMP_COMP_MASK,
        )
        # MCU_CAL_TSSI flag encoding [SRC] usb_phy.c:186-192.
        flag = 0
        if band_5g:
            flag |= 1 << 0
        if ext_pa:
            flag |= 1 << 8
        if not await mcu_calibrate(mcu, MCU_CAL_TSSI, flag):
            logger.warning("MCU_CAL_TSSI failed (continuing)")
        else:
            cal.tssi_cal_done = True
