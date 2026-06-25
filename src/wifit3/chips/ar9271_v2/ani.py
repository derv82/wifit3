"""ANI (Adaptive Noise Immunity) init over WMI — ported from ani.c.

Only the bring-up path is ported here: ath9k_hw_ani_init resets the PHY-error counters and
enables the MIB counters. The runtime ANI adjustment algorithm (the periodic poll that retunes
OFDM/CCK immunity) is a separate, later concern.
"""
from __future__ import annotations

from . import reg as R
from .hw import AthHw


def update_mibstats(hw: AthHw) -> None:
    """ath9k_hw_update_mibstats [SRC] ani.c:107 — one REG_READ_MULTI of the five MIB counters
    (read-to-clear; the values accumulate driver-side, irrelevant to bring-up)."""
    hw.multi_read([R.AR_RTS_OK, R.AR_RTS_FAIL, R.AR_ACK_FAIL, R.AR_FCS_FAIL, R.AR_BEACON_CNT])


def ani_restart(hw: AthHw) -> None:
    """ath9k_ani_restart [SRC] ani.c:127 — zero the PHY-error counters, arm the timing masks."""
    hw.enable_write_buffer()
    hw.write(R.AR_PHY_ERR_1, 0)
    hw.write(R.AR_PHY_ERR_2, 0)
    hw.write(R.AR_PHY_ERR_MASK_1, R.AR_PHY_ERR_OFDM_TIMING)
    hw.write(R.AR_PHY_ERR_MASK_2, R.AR_PHY_ERR_CCK_TIMING)
    hw.write_flush()
    update_mibstats(hw)


def enable_mib_counters(hw: AthHw) -> None:
    """ath9k_enable_mib_counters [SRC] ani.c — refresh stats then enable the MIB hardware."""
    update_mibstats(hw)
    hw.enable_write_buffer()
    hw.write(R.AR_FILT_OFDM, 0)
    hw.write(R.AR_FILT_CCK, 0)
    hw.write(R.AR_MIBC,
             ~(R.AR_MIBC_COW | R.AR_MIBC_FMC | R.AR_MIBC_CMC | R.AR_MIBC_MCS) & 0x0f)
    hw.write(R.AR_PHY_ERR_MASK_1, R.AR_PHY_ERR_OFDM_TIMING)
    hw.write(R.AR_PHY_ERR_MASK_2, R.AR_PHY_ERR_CCK_TIMING)
    hw.write_flush()


def ani_init(hw: AthHw) -> None:
    """ath9k_hw_ani_init [SRC] ani.c — the only ANI work in cold bring-up (sw config + the two
    register helpers); the trigger thresholds are driver state, not wire ops."""
    ani_restart(hw)
    enable_mib_counters(hw)
