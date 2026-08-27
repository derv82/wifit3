"""MT76x2U PHY init + channel programming primitives.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors:
  - mt76x02_phy.c::mt76x02_phy_set_rxpath / mt76x02_phy_set_txdac
  - mt76x02_phy.c::mt76x02_phy_set_band   / mt76x02_phy_set_bw
  - mt76x2/mcu.c::mt76x2_mcu_set_channel  / mt76x2_mcu_init_gain
  - mt76x2/mcu.c::mt76x2_mcu_load_cr
"""
from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass

from .constants import (
    MT_AUTO_RSP_CFG,
    MT_AUTO_RSP_EN,
    MT_BB_PA_MODE_CFG0,
    MT_BB_PA_MODE_CFG1,
    MT_BBP_AGC_GAIN_MASK,
    MT_BBP_AGC_GAIN_SHIFT,
    MT_BBP_AGC_LNA_HIGH_GAIN_MASK,
    MT_BBP_AGC_LNA_HIGH_GAIN_SHIFT,
    MT_BBP_AGC_R0,
    MT_BBP_AGC_R0_BW_MASK,
    MT_BBP_AGC_R0_BW_SHIFT,
    MT_BBP_AGC_R0_CTRL_CHAN_MASK,
    MT_BBP_AGC_R0_CTRL_CHAN_SHIFT,
    MT_BBP_AGC_R2,
    MT_BBP_AGC_R26,
    MT_BBP_AGC_R35,
    MT_BBP_AGC_R37,
    MT_BBP_AGC_R4,
    MT_BBP_AGC_R5,
    MT_BBP_AGC_R8,
    MT_BBP_AGC_R9,
    MT_BBP_CORE_R1,
    MT_BBP_CORE_R1_BW_MASK,
    MT_BBP_CORE_R1_BW_SHIFT,
    MT_BBP_CORE_R34,
    MT_BBP_RXO_R14,
    MT_BBP_RXO_R18,
    MT_BBP_TXBE_R5,
    MT_ED_CCA_TIMER,
    MT_RX_STAT_1,
    MT_RX_STAT_1_CCA_ERRORS_MASK,
    MT_TX_ALC_CFG_0,
    MT_TX_ALC_CFG_0_CH_INIT_0_MASK,
    MT_TX_ALC_CFG_0_CH_INIT_1_MASK,
    MT_TX_ALC_CFG_0_CH_INIT_1_SHIFT,
    MT_TX_PWR_CFG_0,
    MT_TX_PWR_CFG_1,
    MT_TX_PWR_CFG_2,
    MT_TX_PWR_CFG_3,
    MT_TX_PWR_CFG_4,
    MT_TX_PWR_CFG_7,
    MT_TX_PWR_CFG_8,
    MT_TX_PWR_CFG_9,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_ENABLE_TX,
    MT_RF_PA_MODE_ADJ0,
    MT_RF_PA_MODE_ADJ1,
    MT_RF_PA_MODE_CFG0,
    MT_RF_PA_MODE_CFG1,
    MT_TX0_RF_GAIN_CORR,
    MT_TX1_RF_GAIN_CORR,
    MT_TX_ALC_CFG_2,
    MT_TX_ALC_CFG_3,
    MT_TX_ALC_CFG_4,
    MT_TX_BAND_CFG,
    MT_TX_BAND_CFG_2G,
    MT_TX_BAND_CFG_5G,
    MT_TX_BAND_CFG_UPPER_40M,
    MT_TX_CFACK_EN,
    MT_TX_LINK_CFG,
    MT_TX_PIN_CFG,
    MT_TX_PIN_CFG_RXANT,
    MT_TX_PIN_CFG_TXANT,
    MT_TX_PIN_RFTR_EN,
    MT_TX_PIN_TRSW_EN,
    MT_TX_SW_CFG0,
    MT_TX_SW_CFG1,
    MT_TXOP_CTRL_CFG,
    MT_TXOP_ED_CCA_EN,
    MT_TXOP_HLDR_ET,
    MT_TXOP_HLDR_TX40M_BLK_EN,
    MT_XIFS_TIME_CFG,
    MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
    MT_XIFS_TIME_CFG_OFDM_SIFS_SHIFT,
)
from .eeprom import EE_NIC_CONF_0, EE_NIC_CONF_1, read_u16
from .mcu import (
    CMD_INIT_GAIN_OP,
    CMD_LOAD_CR,
    CMD_SWITCH_CHANNEL_OP,
    McuChannel,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# We can't constants.MT_BBP_TXBE_R0 because that constant doesn't exist in our
# constants module. Define it locally.
MT_BBP_TXBE_R0_VAL = 0x2700  # MT_BBP_TXBE_BASE + 0 * 4


def phy_set_rxpath(transport: MT76x2UTransport, chainmask: int) -> None:
    """[SRC] mt76x02_phy.c:12 — chainmask-dependent BBP AGC R0 toggle."""
    val = transport.read32(MT_BBP_AGC_R0)
    val &= ~(1 << 4)
    if (chainmask & 0xF) == 2:
        val |= 1 << 3
    else:
        val &= ~(1 << 3)
    transport.write32(MT_BBP_AGC_R0, val)
    # Force a follow-up read (memory barrier in kernel).
    _ = transport.read32(MT_BBP_AGC_R0)


def phy_set_txdac(transport: MT76x2UTransport, chainmask: int) -> None:
    """[SRC] mt76x02_phy.c:34 — chainmask-dependent BBP TXBE R5 toggle."""
    txpath = (chainmask >> 8) & 0xF
    if txpath == 2:
        transport.rmw32(MT_BBP_TXBE_R5, 0x3, 0x3)
    else:
        transport.rmw32(MT_BBP_TXBE_R5, 0x3, 0)


def phy_set_txpower_regs(transport: MT76x2UTransport, band_2g: bool,
                         ext_pa: bool) -> None:
    """`mt76x2_phy_set_txpower_regs` — [SRC] mt76x2/phy.c:45.

    Programs per-band PA / RF gain / TX-ALC config. Skipping these (the
    original port did, with a "skip TX-power config" comment in chan.py)
    leaves the chip TX'ing at whatever PA defaults it powered up with —
    enough for occasional Auth/Assoc round-trip but insufficient for
    sustained data injection. Without it, an AWUS036ACM gets deauthed
    by a typical AP every ~10 s as a "weak/dead" client (3 assocs/30s);
    AR9271 with proper PA stays associated the full session (1 assoc/30s).

    Values verified against ``driver_captures/captures_mt76x2u/capture-1.pcap``
    (airmon-ng start phase, frames 2781-3924).
    """
    if band_2g:
        pa_mode_0 = 0x010055FF
        pa_mode_1 = 0x00550055
        transport.write32(MT_TX_ALC_CFG_2, 0x35160A00)
        transport.write32(MT_TX_ALC_CFG_3, 0x35160A06)
        if ext_pa:
            transport.write32(MT_RF_PA_MODE_ADJ0, 0x0000EC00)
            transport.write32(MT_RF_PA_MODE_ADJ1, 0x0000EC00)
        else:
            transport.write32(MT_RF_PA_MODE_ADJ0, 0xF4000200)
            transport.write32(MT_RF_PA_MODE_ADJ1, 0xFA000200)
    else:
        # 5 GHz
        pa_mode_0 = 0x0000FFFF
        pa_mode_1 = 0x00FF00FF
        if ext_pa:
            transport.write32(MT_TX_ALC_CFG_2, 0x2F0F0400)
            transport.write32(MT_TX_ALC_CFG_3, 0x2F0F0476)
        else:
            transport.write32(MT_TX_ALC_CFG_2, 0x1B0F0400)
            transport.write32(MT_TX_ALC_CFG_3, 0x1B0F0476)
        pa_mode_adj = 0x04000000 if ext_pa else 0
        transport.write32(MT_RF_PA_MODE_ADJ0, pa_mode_adj)
        transport.write32(MT_RF_PA_MODE_ADJ1, pa_mode_adj)

    transport.write32(MT_BB_PA_MODE_CFG0, pa_mode_0)
    transport.write32(MT_BB_PA_MODE_CFG1, pa_mode_1)
    transport.write32(MT_RF_PA_MODE_CFG0, pa_mode_0)
    transport.write32(MT_RF_PA_MODE_CFG1, pa_mode_1)

    if ext_pa:
        val = 0x3C3C023C if band_2g else 0x363C023C
        transport.write32(MT_TX0_RF_GAIN_CORR, val)
        transport.write32(MT_TX1_RF_GAIN_CORR, val)
        transport.write32(MT_TX_ALC_CFG_4, 0x00001818)
    elif band_2g:
        val = 0x0F3C3C3C
        transport.write32(MT_TX0_RF_GAIN_CORR, val)
        transport.write32(MT_TX1_RF_GAIN_CORR, val)
        transport.write32(MT_TX_ALC_CFG_4, 0x00000606)
    else:
        transport.write32(MT_TX0_RF_GAIN_CORR, 0x383C023C)
        transport.write32(MT_TX1_RF_GAIN_CORR, 0x24282E28)
        transport.write32(MT_TX_ALC_CFG_4, 0)


def phy_configure_tx_delay(transport: MT76x2UTransport, ext_pa: bool,
                           bw: int) -> None:
    """`mt76x2_configure_tx_delay` — [SRC] mt76x2/phy.c:184.

    Per-band TX timing (TX_SW_CFG0/1 + OFDM SIFS). `_mac_fixup_xtal` sets
    OFDM SIFS to 13; the kernel raises it to 15 here, so this overrides.
    ``bw`` matches kernel (0 = 20 MHz, 1 = 40 MHz).
    """
    if ext_pa:
        cfg0 = 0x000B0C01 if bw else 0x00101101
        cfg1 = 0x00011414
    else:
        cfg0 = 0x000B0B01 if bw else 0x00101001
        cfg1 = 0x00021414
    transport.write32(MT_TX_SW_CFG0, cfg0)
    transport.write32(MT_TX_SW_CFG1, cfg1)
    transport.rmw32(
        MT_XIFS_TIME_CFG,
        MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
        (15 << MT_XIFS_TIME_CFG_OFDM_SIFS_SHIFT)
        & MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
    )


def _adjust_high_lna_gain(transport: MT76x2UTransport, reg: int,
                          offset: int) -> None:
    """`mt76x2_adjust_high_lna_gain` — [SRC] mt76x2/phy.c:12-21.

    Reads the LNA_HIGH_GAIN field (bits 21:16) of one of the BBP AGC
    registers, subtracts ``offset / 2``, writes back. The kernel uses C
    integer division (truncation towards zero), so a negative odd offset
    rounds towards zero, not towards -inf.
    """
    cur = transport.read32(reg)
    gain = (cur & MT_BBP_AGC_LNA_HIGH_GAIN_MASK) >> MT_BBP_AGC_LNA_HIGH_GAIN_SHIFT
    # Sign-extend the 6-bit field for signed arithmetic.
    if gain & 0x20:
        gain -= 0x40
    gain -= int(offset / 2)   # C-style truncation towards zero
    gain &= 0x3F
    new = (cur & ~MT_BBP_AGC_LNA_HIGH_GAIN_MASK) | (gain << MT_BBP_AGC_LNA_HIGH_GAIN_SHIFT)
    transport.write32(reg, new)


def _adjust_agc_gain(transport: MT76x2UTransport, reg: int, offset: int) -> None:
    """`mt76x2_adjust_agc_gain` — [SRC] mt76x2/phy.c:23-31.

    Reads the AGC_GAIN field (bits 14:8), adds ``offset``, writes back.
    """
    cur = transport.read32(reg)
    gain = (cur & MT_BBP_AGC_GAIN_MASK) >> MT_BBP_AGC_GAIN_SHIFT
    if gain & 0x40:
        gain -= 0x80
    gain += offset
    gain &= 0x7F
    new = (cur & ~MT_BBP_AGC_GAIN_MASK) | (gain << MT_BBP_AGC_GAIN_SHIFT)
    transport.write32(reg, new)


def apply_gain_adj(transport: MT76x2UTransport,
                   high_gain: tuple[int, int]) -> None:
    """`mt76x2_apply_gain_adj` — [SRC] mt76x2/phy.c:33-42.

    Adjusts BBP AGC registers 4/5 (high-LNA gain) and 8/9 (AGC gain) by
    the EEPROM-derived per-chain offsets. Required for proper RX
    sensitivity — without it, the chip's LNA + AGC stay at MCU-set
    defaults which don't account for this card's EEPROM-stored gain
    calibration. Symptom of skipping: poor RX from short distances (the
    LNA is over-saturated or under-driven for this specific board).
    """
    g0, g1 = high_gain
    _adjust_high_lna_gain(transport, MT_BBP_AGC_R4, g0)
    _adjust_high_lna_gain(transport, MT_BBP_AGC_R5, g1)
    _adjust_agc_gain(transport, MT_BBP_AGC_R8, g0)
    _adjust_agc_gain(transport, MT_BBP_AGC_R9, g1)


@dataclass
class Mt76x2CalState:
    """Mirror of the subset of kernel ``mt76x02_dev.cal`` that wifit3
    cares about. Held on the driver; mutated in place by
    ``update_channel_gain`` and ``tssi_compensate`` on every recalibration
    tick."""
    agc_gain_init: tuple[int, int] = (0, 0)   # AGC reg 8/9 base gain at init
    agc_gain_cur: tuple[int, int] = (0, 0)    # current per-chain agc gain
    agc_gain_adjust: int = 0                  # VGA gain step
    agc_lowest_gain: bool = False
    low_gain: int = -1                        # -1 = uninit; else 0/1/2
    avg_rssi_all: int = 0
    false_cca: int = 0
    # TSSI state
    tssi_cal_done: bool = False
    tssi_comp_pending: bool = False
    dpd_cal_done: bool = False
    channel_cal_done: bool = False


def _tx_power_mask(v1: int, v2: int, v3: int, v4: int) -> int:
    """`mt76x02_tx_power_mask` — [SRC] mt76x02_phy.c:50-60. Packs four 6-bit
    rate-power values into one u32 (one byte per slot, only low 6 bits)."""
    m = (1 << 6) - 1
    return (
        ((v1 & m) << 0)
        | ((v2 & m) << 8)
        | ((v3 & m) << 16)
        | ((v4 & m) << 24)
    ) & 0xFFFFFFFF


def _rate_power_all_iter(rate_power: dict):
    """Iterate every per-rate power value (the kernel uses
    `mt76x02_rate_power.all[]` which is a union over cck/ofdm/ht/vht).
    Order matches the kernel struct so min/max return the right value."""
    for v in rate_power["cck"]:
        yield v
    for v in rate_power["ofdm"]:
        yield v
    for v in rate_power["ht"]:
        yield v
    for v in rate_power["vht"]:
        yield v


def _add_rate_power_offset(rate_power: dict, offset: int) -> None:
    """`mt76x02_add_rate_power_offset` — [SRC] mt76x02_phy.c:84-91."""
    for arr in (rate_power["cck"], rate_power["ofdm"],
                rate_power["ht"], rate_power["vht"]):
        for i in range(len(arr)):
            arr[i] += offset


def _limit_rate_power(rate_power: dict, limit: int) -> None:
    """`mt76x02_limit_rate_power` — [SRC] mt76x02_phy.c:74-82."""
    for arr in (rate_power["cck"], rate_power["ofdm"],
                rate_power["ht"], rate_power["vht"]):
        for i in range(len(arr)):
            if arr[i] > limit:
                arr[i] = limit


def _get_max_rate_power(rate_power: dict) -> int:
    """`mt76x02_get_max_rate_power` — [SRC] mt76x02_phy.c:62-71."""
    return max(_rate_power_all_iter(rate_power), default=0)


def _get_min_rate_power(rate_power: dict) -> int:
    """`mt76x2_get_min_rate_power` — [SRC] mt76x2/phy.c:118-135. Like min()
    but skips zero entries (kernel treats them as "rate not present")."""
    ret = 0
    for v in _rate_power_all_iter(rate_power):
        if v == 0:
            continue
        if ret == 0:
            ret = v
        elif v < ret:
            ret = v
    return ret


def phy_set_txpower_low(transport: MT76x2UTransport, rate_power: dict,
                        txp_0: int, txp_1: int) -> None:
    """`mt76x02_phy_set_txpower` — [SRC] mt76x02_phy.c:93-122. The low-level
    register writer used by the per-band `phy_set_txpower` orchestrator."""
    cur = transport.read32(MT_TX_ALC_CFG_0)
    cur &= ~MT_TX_ALC_CFG_0_CH_INIT_0_MASK
    cur |= txp_0 & MT_TX_ALC_CFG_0_CH_INIT_0_MASK
    cur &= ~MT_TX_ALC_CFG_0_CH_INIT_1_MASK
    cur |= ((txp_1 << MT_TX_ALC_CFG_0_CH_INIT_1_SHIFT)
            & MT_TX_ALC_CFG_0_CH_INIT_1_MASK)
    transport.write32(MT_TX_ALC_CFG_0, cur)

    cck = rate_power["cck"]
    ofdm = rate_power["ofdm"]
    ht = rate_power["ht"]
    vht = rate_power["vht"]
    transport.write32(MT_TX_PWR_CFG_0,
                      _tx_power_mask(cck[0], cck[2], ofdm[0], ofdm[2]))
    transport.write32(MT_TX_PWR_CFG_1,
                      _tx_power_mask(ofdm[4], ofdm[6], ht[0], ht[2]))
    transport.write32(MT_TX_PWR_CFG_2,
                      _tx_power_mask(ht[4], ht[6], ht[8], ht[10]))
    transport.write32(MT_TX_PWR_CFG_3,
                      _tx_power_mask(ht[12], ht[14], ht[0], ht[2]))
    transport.write32(MT_TX_PWR_CFG_4,
                      _tx_power_mask(ht[4], ht[6], 0, 0))
    transport.write32(MT_TX_PWR_CFG_7,
                      _tx_power_mask(ofdm[7], vht[0], ht[7], vht[1]))
    transport.write32(MT_TX_PWR_CFG_8,
                      _tx_power_mask(ht[14], 0, vht[0], vht[1]))
    transport.write32(MT_TX_PWR_CFG_9,
                      _tx_power_mask(ht[7], 0, vht[0], vht[1]))


def phy_set_txpower(transport: MT76x2UTransport, rate_power: dict,
                    power_info: dict, txpower_conf: int = 60) -> None:
    """`mt76x2_phy_set_txpower` — [SRC] mt76x2/phy.c:137-181. Orchestrator:

      1. read EEPROM power-info + rate-power (caller does this; passes us
         the dicts) — both are populated per channel + per band.
      2. add target_power + BW delta to every rate slot.
      3. clamp to `txpower_conf` (kernel: dev->txpower_conf; regulatory).
      4. compute per-chain init power values (txp_0/txp_1) with clamping
         to [0, 0x2f].
      5. subtract base_power from rate slots so the rate table is centered.
      6. write to TX_ALC_CFG_0 + TX_PWR_CFG_0..9.

    20 MHz only — BW40/BW80 deltas are ignored (we only ever tune 20 MHz).
    """
    target_power_in = power_info["target_power"]
    chain0 = power_info["chain"][0]
    chain1 = power_info["chain"][1]

    # Step 2: add target_power offset to every rate slot (20 MHz: delta=0).
    _add_rate_power_offset(rate_power, target_power_in)

    # Step 3: clamp to regulatory limit.
    _limit_rate_power(rate_power, txpower_conf)

    # Step 4: per-chain init power computation. Kernel uses min(non-zero)
    # of the rate table as the base.
    base_power = _get_min_rate_power(rate_power)
    delta = base_power - target_power_in
    txp_0 = chain0["target_power"] + chain0["delta"] + delta
    txp_1 = chain1["target_power"] + chain1["delta"] + delta

    gain = min(txp_0, txp_1)
    if gain < 0:
        base_power -= gain
        txp_0 -= gain
        txp_1 -= gain
    elif gain > 0x2F:
        base_power -= gain - 0x2F
        txp_0 = 0x2F
        txp_1 = 0x2F

    # Step 5: center the rate table on the chosen base.
    _add_rate_power_offset(rate_power, -base_power)

    # Diagnostic — log the computed values so we can spot bad EEPROM /
    # bad math in the field. INFO level so it shows up in the standard log.
    logger.debug(
        "MT7612U: TX power: target_in=%d chain0(tp=%d,d=%d) chain1(tp=%d,d=%d) "
        "base=%d txp_0=0x%02x txp_1=0x%02x sample(cck0=%d ofdm0=%d ht0=%d)",
        target_power_in,
        chain0["target_power"], chain0["delta"],
        chain1["target_power"], chain1["delta"],
        base_power, txp_0 & 0x3F, txp_1 & 0x3F,
        rate_power["cck"][0], rate_power["ofdm"][0], rate_power["ht"][0],
    )

    # Step 6: write the 9 registers.
    phy_set_txpower_low(transport, rate_power, txp_0 & 0x3F, txp_1 & 0x3F)


def init_agc_gain(transport: MT76x2UTransport) -> tuple[int, int]:
    """`mt76x02_init_agc_gain` — [SRC] mt76x02_phy.c:193-204. Reads the
    AGC_GAIN field (bits 14:8) from BBP AGC 8 and 9, returns ``(gain_0,
    gain_1)``. These seed `dev->cal.agc_gain_init[0/1]` which the periodic
    update_channel_gain loop uses as the reference."""
    val8 = transport.read32(MT_BBP_AGC_R8)
    val9 = transport.read32(MT_BBP_AGC_R9)
    g0 = (val8 & MT_BBP_AGC_GAIN_MASK) >> MT_BBP_AGC_GAIN_SHIFT
    g1 = (val9 & MT_BBP_AGC_GAIN_MASK) >> MT_BBP_AGC_GAIN_SHIFT
    logger.debug(
        "MT7612U: init_agc_gain: AGC_R8=0x%08x → g0=0x%02x, AGC_R9=0x%08x → g1=0x%02x",
        val8, g0, val9, g1,
    )
    return (g0, g1)


def adjust_vga_gain(transport: MT76x2UTransport,
                    low_gain: int, agc_gain_adjust: int) -> tuple[int, bool, bool]:
    """`mt76x02_phy_adjust_vga_gain` — [SRC] mt76x02_phy.c:169-191.

    Returns ``(new_agc_gain_adjust, changed, agc_lowest_gain)``. The
    kernel reads the false-CCA error counter (RX_STAT_1) to drive an
    adaptive VGA gain step (±2). Used by `update_channel_gain` between
    band-gain re-tunes."""
    limit = 16 if low_gain > 0 else 4
    rx_stat_1 = transport.read32(MT_RX_STAT_1)
    false_cca = rx_stat_1 & MT_RX_STAT_1_CCA_ERRORS_MASK
    changed = False
    if false_cca > 800 and agc_gain_adjust < limit:
        agc_gain_adjust += 2
        changed = True
    elif (false_cca < 10 and agc_gain_adjust > 0) or (
        agc_gain_adjust >= limit and false_cca < 500
    ):
        agc_gain_adjust -= 2
        changed = True
    agc_lowest_gain = agc_gain_adjust >= limit
    return (agc_gain_adjust, changed, agc_lowest_gain)


def phy_set_gain_val(transport: MT76x2UTransport,
                     agc_gain_cur: tuple[int, int],
                     agc_gain_adjust: int,
                     has_ext_lna: bool,
                     band_2g: bool,
                     bw_40plus: bool) -> None:
    """`mt76x2_phy_set_gain_val` — [SRC] mt76x2/phy.c:244-272. Writes BBP
    AGC 8 + 9 with a width/LNA-dependent base value + per-chain gain. DFS
    adjust path (`mt76x02_phy_dfs_adjust_agc`) is omitted — we never tune
    to DFS channels."""
    g0 = agc_gain_cur[0] - agc_gain_adjust
    g1 = agc_gain_cur[1] - agc_gain_adjust

    base = 0x1836 << 16
    if not has_ext_lna and bw_40plus:
        base = 0x1E42 << 16
    if has_ext_lna and band_2g and not bw_40plus:
        base = 0x0F36 << 16
    base |= 0xF8

    transport.write32(
        MT_BBP_AGC_R8,
        base | ((g0 & 0x7F) << MT_BBP_AGC_GAIN_SHIFT),
    )
    transport.write32(
        MT_BBP_AGC_R9,
        base | ((g1 & 0x7F) << MT_BBP_AGC_GAIN_SHIFT),
    )


def update_channel_gain(
    transport: MT76x2UTransport,
    cal: Mt76x2CalState,
    *,
    band_2g: bool,
    bw_40plus: bool,
    has_ext_lna: bool,
    rssi_thresh: int,
    low_rssi_thresh: int,
    avg_rssi_all: int = -75,
) -> None:
    """`mt76x2_phy_update_channel_gain` — [SRC] mt76x2/phy.c:274-348.

    The periodic RX gain adapt loop. Kernel runs this every ~1 s. Wifit3
    runs in monitor mode without an associated station, so we don't have a
    real ``avg_rssi`` to feed; the kernel fallback is -75 which we mirror
    by default. The function still re-issues the BBP gain writes the
    kernel does — the static defaults are not great for arbitrary RSSI.

    `bw_40plus` = True only when chandef.width >= 40 MHz. We're 20 MHz so
    this is always False — kept as a parameter for kernel-fidelity.
    """
    if avg_rssi_all == 0:
        avg_rssi_all = -75
    cal.avg_rssi_all = avg_rssi_all

    low_gain = (avg_rssi_all > rssi_thresh) + (avg_rssi_all > low_rssi_thresh)
    gain_change = (
        cal.low_gain < 0 or ((cal.low_gain & 2) ^ (low_gain & 2))
    )
    cal.low_gain = low_gain

    if not gain_change:
        new_adj, changed, lowest = adjust_vga_gain(
            transport, low_gain, cal.agc_gain_adjust
        )
        cal.agc_gain_adjust = new_adj
        cal.agc_lowest_gain = lowest
        if changed:
            phy_set_gain_val(
                transport, cal.agc_gain_cur, cal.agc_gain_adjust,
                has_ext_lna, band_2g, bw_40plus,
            )
        return

    if bw_40plus:
        transport.write32(MT_BBP_RXO_R14, 0x00560211)
        val = transport.read32(MT_BBP_AGC_R26) & ~0xF
        val |= 0x3 if low_gain == 2 else 0x5
        transport.write32(MT_BBP_AGC_R26, val)
    else:
        transport.write32(MT_BBP_RXO_R14, 0x00560423)

    low_gain_delta = 10 if has_ext_lna else 14

    agc_37 = 0x2121262C
    if band_2g:
        agc_35 = 0x11111516
    elif low_gain == 2:
        agc_35 = agc_37 = 0x08080808
    elif bw_40plus:
        agc_35 = 0x10101014
    else:
        agc_35 = 0x11111116

    if low_gain == 2:
        transport.write32(MT_BBP_RXO_R18, 0xF000A990)
        transport.write32(MT_BBP_AGC_R35, 0x08080808)
        transport.write32(MT_BBP_AGC_R37, 0x08080808)
        gain_delta = low_gain_delta
        cal.agc_gain_adjust = 0
    else:
        transport.write32(MT_BBP_RXO_R18, 0xF000A991)
        gain_delta = 0
        cal.agc_gain_adjust = low_gain_delta

    transport.write32(MT_BBP_AGC_R35, agc_35)
    transport.write32(MT_BBP_AGC_R37, agc_37)

    g0 = cal.agc_gain_init[0] - gain_delta
    g1 = cal.agc_gain_init[1] - gain_delta
    cal.agc_gain_cur = (g0, g1)
    phy_set_gain_val(
        transport, cal.agc_gain_cur, cal.agc_gain_adjust,
        has_ext_lna, band_2g, bw_40plus,
    )

    # Clear false-CCA counter so the next adjust_vga_gain reads fresh data.
    transport.read32(MT_RX_STAT_1)


async def tssi_compensate(
    transport: MT76x2UTransport,
    mcu_channel,
    cal: Mt76x2CalState,
    *,
    power_info: dict,
    band_2g: bool,
    ext_pa: bool,
    channel: int,
    ed_tx_blocked: bool = False,
) -> None:
    """`mt76x2_phy_tssi_compensate` — [SRC] mt76x2/phy.c:203-242.

    Two-phase: first call triggers MCU TSSI sample (cal_mode=BIT(0)), sets
    ``tssi_comp_pending``; next call checks BBP CORE 34 bit 4 (TSSI sample
    ready) and submits the compensation MCU command (cal_mode=BIT(1)) with
    the per-chain TSSI slopes + offsets. If the chip uses internal PA and
    no DPD yet, MCU_CAL_DPD is fired after a 10 ms settle.
    """
    from .mcu import mcu_calibrate, mcu_tssi_comp
    from .mcu import MCU_CAL_DPD as _MCU_CAL_DPD   # imported lazily to avoid cycle

    if not cal.tssi_cal_done:
        return

    if not cal.tssi_comp_pending:
        # Trigger phase.
        await mcu_tssi_comp(mcu_channel, pa_mode=0, cal_mode=1,
                            slope0=0, slope1=0, offset0=0, offset1=0)
        cal.tssi_comp_pending = True
        return

    # Compensation phase.
    if transport.read32(MT_BBP_CORE_R34) & (1 << 4):
        return   # MCU not ready yet; wait for next tick

    cal.tssi_comp_pending = False
    pa_mode = 1 if ext_pa else 0
    chain0 = power_info["chain"][0]
    chain1 = power_info["chain"][1]
    await mcu_tssi_comp(
        mcu_channel,
        pa_mode=pa_mode,
        cal_mode=2,
        slope0=chain0["tssi_slope"],
        slope1=chain1["tssi_slope"],
        offset0=chain0["tssi_offset"],
        offset1=chain1["tssi_offset"],
    )

    if pa_mode or cal.dpd_cal_done or ed_tx_blocked:
        return

    await asyncio.sleep(0.015)   # kernel usleep_range(10000, 20000)
    await mcu_calibrate(mcu_channel, _MCU_CAL_DPD, channel)
    cal.dpd_cal_done = True


def edcca_tx_enable(transport: MT76x2UTransport, enable: bool) -> None:
    """`mt76x02_edcca_tx_enable` — [SRC] mt76x02_mac.c:1075.

    Enables/disables the TX engine, auto-response, and **PA-LNA + antenna
    pin drive** via MT_TX_PIN_CFG. The TX_PIN_CFG write is the load-bearing
    piece — without it, no antenna pins are driven and TX power collapses
    to whatever boot defaults give (insufficient for sustained data
    injection against a typical AP). [SRC] mt76x02_regs.h:392-396.
    """
    if enable:
        transport.rmw32(
            MT_MAC_SYS_CTRL,
            MT_MAC_SYS_CTRL_ENABLE_TX,
            MT_MAC_SYS_CTRL_ENABLE_TX,
        )
        transport.rmw32(MT_AUTO_RSP_CFG, MT_AUTO_RSP_EN, MT_AUTO_RSP_EN)
        data = transport.read32(MT_TX_PIN_CFG)
        data |= (
            MT_TX_PIN_CFG_TXANT
            | MT_TX_PIN_CFG_RXANT
            | MT_TX_PIN_RFTR_EN
            | MT_TX_PIN_TRSW_EN
        )
        transport.write32(MT_TX_PIN_CFG, data)
    else:
        transport.rmw32(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX, 0)
        transport.rmw32(MT_AUTO_RSP_CFG, MT_AUTO_RSP_EN, 0)
        transport.rmw32(MT_TX_PIN_CFG, MT_TX_PIN_CFG_TXANT, 0)
        transport.rmw32(MT_TX_PIN_CFG, MT_TX_PIN_CFG_RXANT, 0)


def edcca_init(transport: MT76x2UTransport) -> None:
    """`mt76x02_edcca_init` — [SRC] mt76x02_mac.c:1100. Wifit3 takes the
    `ed_monitor=false` branch (we don't run kernel's EDCCA monitoring loop):

      - SET   MT_TX_LINK_CFG.MT_TX_CFACK_EN
      - CLEAR MT_TXOP_CTRL_CFG.MT_TXOP_ED_CCA_EN
      - WRITE MT_BBP(AGC, 2) = 0x00007070  (is_mt76x2 branch)
      - SET   MT_TXOP_HLDR_ET.MT_TXOP_HLDR_TX40M_BLK_EN
      - call  edcca_tx_enable(True)          ← critical TX_PIN_CFG write
      - read-clear MT_ED_CCA_TIMER
    """
    transport.rmw32(MT_TX_LINK_CFG, MT_TX_CFACK_EN, MT_TX_CFACK_EN)
    transport.rmw32(MT_TXOP_CTRL_CFG, MT_TXOP_ED_CCA_EN, 0)
    transport.write32(MT_BBP_AGC_R2, 0x00007070)
    transport.rmw32(
        MT_TXOP_HLDR_ET,
        MT_TXOP_HLDR_TX40M_BLK_EN,
        MT_TXOP_HLDR_TX40M_BLK_EN,
    )
    edcca_tx_enable(transport, enable=True)
    transport.read32(MT_ED_CCA_TIMER)


def phy_set_band(transport: MT76x2UTransport, band_5g: bool,
                 primary_upper: bool = False) -> None:
    """[SRC] mt76x02_phy.c:150."""
    if not band_5g:
        # 2.4 GHz: set 2G, clear 5G.
        cur = transport.read32(MT_TX_BAND_CFG)
        cur = (cur | MT_TX_BAND_CFG_2G) & ~MT_TX_BAND_CFG_5G
    else:
        # 5 GHz: clear 2G, set 5G.
        cur = transport.read32(MT_TX_BAND_CFG)
        cur = (cur | MT_TX_BAND_CFG_5G) & ~MT_TX_BAND_CFG_2G

    if primary_upper:
        cur |= MT_TX_BAND_CFG_UPPER_40M
    else:
        cur &= ~MT_TX_BAND_CFG_UPPER_40M
    transport.write32(MT_TX_BAND_CFG, cur)


def phy_set_bw_20mhz(transport: MT76x2UTransport, ctrl: int = 0) -> None:
    """[SRC] mt76x02_phy.c:124 — width=20MHz default branch.

    For 20MHz: core_val=0, agc_val=1. `ctrl` = upper/lower extension marker
    (irrelevant for 20MHz but the kernel writes it anyway).
    """
    core_val = 0
    agc_val = 1

    transport.rmw32(MT_BBP_CORE_R1,
                    MT_BBP_CORE_R1_BW_MASK,
                    (core_val << MT_BBP_CORE_R1_BW_SHIFT) & MT_BBP_CORE_R1_BW_MASK)
    transport.rmw32(MT_BBP_AGC_R0,
                    MT_BBP_AGC_R0_BW_MASK,
                    (agc_val << MT_BBP_AGC_R0_BW_SHIFT) & MT_BBP_AGC_R0_BW_MASK)
    transport.rmw32(MT_BBP_AGC_R0,
                    MT_BBP_AGC_R0_CTRL_CHAN_MASK,
                    (ctrl << MT_BBP_AGC_R0_CTRL_CHAN_SHIFT) & MT_BBP_AGC_R0_CTRL_CHAN_MASK)
    transport.rmw32(MT_BBP_TXBE_R0_VAL,
                    0x3,
                    ctrl & 0x3)


# ---------------------------------------------------------------------------
# MCU-side commands.
# ---------------------------------------------------------------------------
async def mcu_load_cr(mcu: McuChannel, cr_type: int = 2,
                      temp_level: int = 0, channel: int = 0) -> bool:
    """`mt76x2_mcu_load_cr` — [SRC] mt76x2/mcu.c:47. Called at init as
    `mt76x2_mcu_load_cr(MT_RF_BBP_CR, 0, 0)` ([SRC] usb_init.c:180), where
    the `mcu_cr_mode` enum gives MT_RF_CR=0, MT_BBP_CR=1, MT_RF_BBP_CR=2.

    Payload struct (8 bytes): { u8 cr_mode; u8 temp; u8 ch; u8 _pad;
    __le32 cfg }, where the chip selects the CR table from
    `cfg = BIT(31) | ((NIC_CONF_0 >> 8) & 0xff) | ((NIC_CONF_1 << 8) & 0xff00)`
    read live from EEPROM. Sent with wait_resp=true.
    """
    nic_conf_0 = read_u16(mcu.transport, EE_NIC_CONF_0)
    nic_conf_1 = read_u16(mcu.transport, EE_NIC_CONF_1)
    cfg = (1 << 31) | ((nic_conf_0 >> 8) & 0x00FF) | ((nic_conf_1 << 8) & 0xFF00)
    payload = struct.pack("<BBBBI", cr_type, temp_level, channel, 0,
                          cfg & 0xFFFFFFFF)
    return await mcu.send(CMD_LOAD_CR, payload, wait_resp=True,
                          resp_timeout_ms=1000)


async def mcu_set_channel(mcu: McuChannel, channel: int, bw: int,
                          bw_index: int, scan: bool, chainmask: int) -> bool:
    """[SRC] mt76x2/mcu.c:15. CMD_SWITCH_CHANNEL_OP, sent TWICE per switch.

    The chip needs two SWITCH_CHANNEL_OP commands: first the channel
    without the extension-channel info (`ext_chan=0`), then after a
    5-10 ms settle the same struct with `ext_chan = 0xe0 + bw_index`.
    Both wait_resp=true.

    Payload struct (8 bytes, 4-byte aligned):
       u8  idx           — channel number
       u8  scan          — 0/1
       u8  bw            — 0=20, 1=40, 2=80
       u8  _pad0
       __le16 chainmask
       u8  ext_chan      — 0 on the first send, 0xe0 + bw_index on the second
       u8  _pad1
    """
    def _pack(ext_chan: int) -> bytes:
        return struct.pack(
            "<BBBBHBB",
            channel & 0xFF,
            1 if scan else 0,
            bw & 0xFF,
            0,
            chainmask & 0xFFFF,
            ext_chan & 0xFF,
            0,
        )

    if not await mcu.send(CMD_SWITCH_CHANNEL_OP, _pack(0), wait_resp=True,
                          resp_timeout_ms=1000):
        return False
    await asyncio.sleep(0.005)   # kernel usleep_range(5000, 10000)
    return await mcu.send(CMD_SWITCH_CHANNEL_OP, _pack(0xE0 + bw_index),
                          wait_resp=True, resp_timeout_ms=1000)


async def mcu_init_gain(mcu: McuChannel, channel: int,
                        gain: int = 0, force: bool = True) -> bool:
    """[SRC] mt76x2/mcu.c:75. CMD_INIT_GAIN_OP, wait_resp=true.

    Payload: __le32 channel (BIT(31) if force) + __le32 gain.
    """
    chan_field = channel | (1 << 31) if force else channel
    payload = struct.pack("<II", chan_field & 0xFFFFFFFF, gain & 0xFFFFFFFF)
    return await mcu.send(CMD_INIT_GAIN_OP, payload, wait_resp=True,
                          resp_timeout_ms=1000)
