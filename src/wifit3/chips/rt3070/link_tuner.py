"""AGC / VGC link tuning for the RT3070 (BBP66 sensitivity).

Ported from ``rt2x00link.c`` + ``rt2800lib.c`` (``rt2800_get_default_vgc`` 5723,
``rt2800_set_vgc`` 5759, ``rt2800_reset_tuner`` 5781, ``rt2800_link_tuner`` 5787).

Two consumers:
  * ``reset_tuner`` — run on every channel config (``rt2x00link_reset_tuner`` →
    driver reset_tuner). The kernel zeroes ``qual->vgc_level`` first, so the default
    VGC is *always* re-written to BBP66; we model that (no stale-skip).
  * ``link_tuner`` — the periodic ~1 Hz worker. It only runs for STA/AP interfaces
    (``intf_sta_count``); a monitor-only interface never schedules it, so it emits
    nothing in the airmon capture. Ported faithfully for a future managed-mode port.

This card is 2.4 GHz only (RF3020), so ``get_default_vgc`` always takes the 2.4 GHz
RT3070 arm: ``0x1c + 2*lna_gain``. The 5 GHz arms are ported for a future band.
"""
from __future__ import annotations

from . import constants as C
from .constants import ChipInfo
from .transport import RT3070Transport


def get_default_vgc(chip: ChipInfo, lna_gain: int, band_2ghz: bool = True) -> int:
    """Default BBP66 VGC for the current band/LNA [SRC rt2800lib.c:5723-5757].

    #TODO untestable: the 5 GHz arm — this card is 2.4 GHz only."""
    if band_2ghz:
        # RT3070 (and the rt3/rt5 family this shares with) use 0x1c + 2*lna_gain;
        # the legacy 0x2e + lna_gain arm is for RT2860/RT2870-class silicon.
        if (chip.is_rt(C.RT3070) or chip.is_rt(C.RT3071) or chip.is_rt(C.RT3090)):
            return 0x1C + 2 * lna_gain
        return 0x2E + lna_gain
    return 0x32 + (lna_gain * 5) // 3   # #TODO untestable: 5 GHz, non-HT40 default


def reset_tuner(t: RT3070Transport, chip: ChipInfo, lna_gain: int) -> None:
    """Re-arm the link tuner to the default VGC [SRC rt2800lib.c:5781-5784
    rt2800_reset_tuner → rt2800_set_vgc]. ``rt2x00link_reset_tuner`` zeroes the
    cached vgc_level before this, so the write always happens on RF30xx (the
    non-``bbp_write_with_rx_chain`` arm)."""
    t.bbp_write(66, get_default_vgc(chip, lna_gain))


def link_tuner(t: RT3070Transport, chip: ChipInfo, lna_gain: int, rssi: int,
               vgc_level: int) -> int:
    """Periodic ~1 Hz AGC step [SRC rt2800lib.c:5787-5830 rt2800_link_tuner].

    Returns the new vgc_level. Only the default (non-RT3572/3593/3883/5592) arm
    applies to this card: bump VGC by 0x10 when RSSI beats -80 dBm. Writes BBP66
    only when the level changes (``rt2800_set_vgc`` guard).

    Not exercised in monitor mode (``intf_sta_count == 0`` ⇒ never scheduled),
    so it is a no-op in the airmon capture; ported for a future managed-mode port.
    """
    vgc = get_default_vgc(chip, lna_gain)
    if rssi > -80:
        vgc += 0x10
    if vgc != vgc_level:
        t.bbp_write(66, vgc)
    return vgc
