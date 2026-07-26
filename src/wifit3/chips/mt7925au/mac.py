"""MT7925AU periodic mac_work register reads (mt792x_mac.c).

The kernel's mt792x_mac_work runs on a timer: every tick it reads the survey
(channel-busy) counters and clears RXTIME; every 2nd tick it also reads the MIB
stats. mt792x_mac_reset_counters is a one-shot at __mt7925_start. These are pure
register reads (plus the RXTIME_CLR set); the values are consumed for stats, not
verified. Ported so the port reproduces the interleaved reads on the wire.

Addresses are band-0, grepped verbatim from mt792x_regs.h.
"""
from .transport import MT7925AUTransport
# ruff: noqa: F403, F405
from .constants import *


def update_survey(t: MT7925AUTransport, band: int = 0) -> None:
    """mt792x_phy_update_channel (mt792x_mac.c:221): busy/tx/rx/obss time reads, then
    clear the RXTIME accumulator in TIME0."""
    t.read_reg32(MT_MIB_SDR9(band))
    t.read_reg32(MT_MIB_SDR36(band))
    t.read_reg32(MT_MIB_SDR37(band))
    t.read_reg32(MT_WF_RMAC_MIB_AIRTIME14(band))
    t.set_bits(MT_WF_RMAC_MIB_TIME0(band), MT_WF_RMAC_MIB_RXTIME_CLR)


def update_mib_stats(t: MT7925AUTransport, band: int = 0) -> None:
    """mt792x_mac_update_mib_stats (mt792x_mac.c:77): 31 MIB counter reads, in order."""
    for reg in (MT_MIB_SDR3(band), MT_MIB_MB_BSDR3(band), MT_MIB_MB_BSDR2(band),
                MT_MIB_MB_BSDR0(band), MT_MIB_MB_BSDR1(band), MT_MIB_SDR12(band),
                MT_MIB_SDR14(band), MT_MIB_SDR15(band), MT_MIB_SDR32(band),
                MT_ETBF_TX_APP_CNT(band), MT_ETBF_RX_FB_CNT(band), MT_MIB_SDR5(band),
                MT_MIB_SDR22(band), MT_MIB_SDR23(band), MT_MIB_SDR31(band)):
        t.read_reg32(reg)
    for n in range(MT792x_MIB_TX_AMSDU_LEN):
        t.read_reg32(MT_PLE_AMSDU_PACK_MSDU_CNT(n))
    for n in range(4):
        t.read_reg32(MT_TX_AGG_CNT(band, n))
        t.read_reg32(MT_TX_AGG_CNT2(band, n))


def reset_counters(t: MT7925AUTransport, band: int = 0) -> None:
    """mt792x_mac_reset_counters (mt792x_mac.c:192): one-shot at __mt7925_start. Reads
    the TX-agg + survey counters, then clears the RXTIME accumulators in TIME0/AIRTIME0."""
    for n in range(4):
        t.read_reg32(MT_TX_AGG_CNT(band, n))
        t.read_reg32(MT_TX_AGG_CNT2(band, n))
    t.read_reg32(MT_MIB_SDR9(band))
    t.read_reg32(MT_MIB_SDR36(band))
    t.read_reg32(MT_MIB_SDR37(band))
    t.set_bits(MT_WF_RMAC_MIB_TIME0(band), MT_WF_RMAC_MIB_RXTIME_CLR)
    t.set_bits(MT_WF_RMAC_MIB_AIRTIME0(band), MT_WF_RMAC_MIB_RXTIME_CLR)
