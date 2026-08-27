"""RTL8812AU dynamic mechanism — DIG (Dynamic Initial Gain) watchdog.

Ports the RX-relevant slice of `rtw_phy_dynamic_mechanism` (rtw88 phy.c:826) that
the kernel runs every RTW_WATCH_DOG_DELAY_TIME (HZ*2 = 2 s) and that our driver
did not run at all. `rtw_phy_dig` (phy.c:536) reads the false-alarm (FA) count
and walks the OFDM initial-gain index (IGI): low FA -> lower IGI (more sensitive),
high FA -> raise IGI (reject noise). Left un-run, IGI is frozen at the AGC-table
default; under a fast-hopping high-FA load the receiver drowns in false alarms at
a too-sensitive gain and goes deaf while the MAC/DMA sit idle-but-alive.

Monitor deviation [[feedback_monitor_mode_deviation]]: there are no associated
STAs, so we run the kernel's no-link / coverage path (`linked=false`): IGI
clamped to [DIG_CVRG_MIN=0x1c, DIG_CVRG_MAX=0x2a], FA thresholds 2000/4000/5000,
step {+4,+3,+2} then -2.

FA accounting + counter reset mirror `rtw88xxa_false_alarm_statistics`
(rtw88xxa.c:1664). IGI lives in REG_RXIGI_A/B — the two OFDM paths, since the
8812a is 2T2R (vs the 8814a's four). `rtw_phy_dig_write` writes every path the
same value.

Also ports `rtw8812a_do_lck` (the VCO LC re-lock from `rtw88xxa_phy_pwrtrack`,
rtw88xxa.c:1884) but runs it on a fixed cadence — **a deliberate deviation from
rtw88**. The kernel only runs do_lck when the thermal drift crosses the IQK
threshold (8 from the efuse baseline), but the VCO capture-range drift during
fast hopping stays *below* that, so the kernel's LCK never fires and the synth
silently drifts out of lock (the hop-death — FA=0/CRC=0 while RF18 still reads
the correct channel). rtw88 targets parked STA/AP where this never matters; the
out-of-tree hopping forks re-center the VCO, which is what we do here.

Intentionally omitted: `rtw_phy_dig_check_damping` (a linked-mode oscillation
guard keyed on a *changing* min_rssi — constant in the coverage path), the
TX-power-index re-program + `do_iqk` inside pwrtrack (TX path, not monitor RX),
and cfo/dpk/adaptivity tracking (not RX-relevant).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..rtw88_base.rf_sipi import RFREG_MASK, read_rf, write_rf_masked
from . import constants as C
from .transport import RTL8812AUTransport

logger = logging.getLogger(__name__)


def read_total_fa_cnt(transport: RTL8812AUTransport) -> int:
    """Port of rtw88xxa_false_alarm_statistics' FA accounting + counter reset.

    total_fa_cnt = ofdm_fa + (cck_fa when CCK demod is enabled), then reset the FA
    counters so the next 2 s window starts clean (rtw88xxa.c:1706-1711).
    """
    cck_enabled = bool(transport.read32(C.REG_RXPSEL) & C.BIT_RXPSEL_CCK_EN)
    cck_fa = transport.read16(C.REG_FA_CCK)
    ofdm_fa = transport.read16(C.REG_FA_OFDM)
    total_fa = ofdm_fa + (cck_fa if cck_enabled else 0)

    transport.write32_set(C.REG_FAS, 1 << 17)
    transport.write32_clr(C.REG_FAS, 1 << 17)
    transport.write32_clr(C.REG_CCK0_FAREPORT, 1 << 15)
    transport.write32_set(C.REG_CCK0_FAREPORT, 1 << 15)
    transport.write32_set(C.REG_CNTRST, 1 << 0)
    transport.write32_clr(C.REG_CNTRST, 1 << 0)
    return total_fa


def dig_write(transport: RTL8812AUTransport, igi: int) -> None:
    """rtw_phy_dig_write — write IGI to both OFDM paths (8812a 2T2R)."""
    for addr in (C.REG_RXIGI_A, C.REG_RXIGI_B):
        transport.write32_mask(addr, C.DIG_IGI_MASK, igi & C.DIG_IGI_MASK)


@dataclass
class DigState:
    """DIG history (history[0] = pre_igi; rest unused without damping)."""
    igi: int = C.DIG_CVRG_MIN
    history: list[int] = field(default_factory=lambda: [C.DIG_CVRG_MIN] * 4)


def dig_init(transport: RTL8812AUTransport) -> DigState:
    """Seed DIG history from the live IGI register, mirroring rtw_phy_init's read
    of chip->dig[0]. No write: the AGC table already set IGI during phy bring-up;
    the watchdog walks it from here."""
    igi = transport.read32(C.REG_RXIGI_A) & C.DIG_IGI_MASK
    logger.debug("DIG init: seeded IGI from REG_RXIGI_A = 0x%02x", igi)
    return DigState(igi=igi, history=[igi] * 4)


def dig_step(transport: RTL8812AUTransport, state: DigState, fa_cnt: int) -> None:
    """One DIG tick — no-link (coverage) path of rtw_phy_dig.

    cur_igi = pre_igi (+step on the first crossed FA threshold) - 2, clamped to
    the coverage bounds. Writes only when IGI changes (as the kernel does).
    """
    pre_igi = state.history[0]
    cur_igi = pre_igi

    # Test FA from the highest threshold first; the step is offset by -2
    # (compensated below) so a quiet band (fa < LOW) drifts IGI down toward max
    # sensitivity.
    if fa_cnt > C.DIG_CVRG_FA_TH_EXTRA_HIGH:
        cur_igi += 4
    elif fa_cnt > C.DIG_CVRG_FA_TH_HIGH:
        cur_igi += 3
    elif fa_cnt > C.DIG_CVRG_FA_TH_LOW:
        cur_igi += 2
    cur_igi -= 2

    # Coverage-mode boundary (linked=false): lower=DIG_CVRG_MIN, upper=clamp(
    # min+OFFSET, min, DIG_CVRG_MAX). min_rssi == dig_min here.
    lower = C.DIG_CVRG_MIN
    upper = min(C.DIG_CVRG_MAX, C.DIG_CVRG_MIN + C.DIG_RSSI_GAIN_OFFSET)
    cur_igi = max(lower, min(cur_igi, upper))

    state.history = [cur_igi] + state.history[:3]

    if cur_igi != pre_igi:
        dig_write(transport, cur_igi)
        state.igi = cur_igi
        logger.debug("DIG: fa=%d IGI 0x%02x -> 0x%02x", fa_cnt, pre_igi, cur_igi)


# ---------------------------------------------------------------------------
# Thermal power-track LCK (VCO re-lock) — rtw88xxa_phy_pwrtrack RX slice
# ---------------------------------------------------------------------------

class _ThermalEwma:
    """Port of the kernel's DECLARE_EWMA(thermal, 10, 4): precision=10,
    weight_rcp=4 — each new sample gets 1/16 weight."""

    __slots__ = ("internal",)

    def __init__(self) -> None:
        self.internal = 0

    def add(self, val: int) -> None:
        if self.internal:
            self.internal = ((self.internal * 15) + (val << 10)) >> 4
        else:
            self.internal = val << 10

    def read(self) -> int:
        return self.internal >> 10


@dataclass
class PwrTrackState:
    """Thermal telemetry + periodic-LCK cadence (rtw88xxa_phy_pwrtrack, decoupled
    from the need_iqk gate)."""
    thermal_meter_k: int = 0          # efuse cal reference — drift telemetry only
    avg: _ThermalEwma = field(default_factory=_ThermalEwma)
    ticks_since_lck: int = 0


def read_thermal(transport: RTL8812AUTransport) -> int:
    """RF thermal meter (rtw_read_rf RF_T_METER, mask 0xfc00)."""
    return read_rf(transport, C.RF_T_METER, C.RF_T_METER_MASK, path="a")


def pwrtrack_init(transport: RTL8812AUTransport, efuse_thermal: int) -> PwrTrackState:
    """Seed thermal tracking. Reference = the efuse factory (cold) cal temp, as
    the kernel does (`thermal_meter_k = efuse.thermal_meter`) — so drift is
    measured from *cold* and LCK fires promptly even on a warm-started chip.

    If the efuse meter is uncalibrated (0xFF), fall back to the first live
    reading (the kernel disables pwr-track entirely in that case; we keep it
    running off the live baseline since LCK is exactly what we're here for).
    """
    live = read_thermal(transport)
    if efuse_thermal == 0xFF:
        ref = live
        logger.debug("PwrTrack init: efuse thermal=0xff - using live=%d as reference", live)
    else:
        ref = efuse_thermal
        logger.debug("PwrTrack init: ref(efuse)=%d live=%d drift=%d (LCK @ >=%d)",
                    ref, live, abs(live - ref), C.PWRTRACK_IQK_THRESHOLD)
    state = PwrTrackState(thermal_meter_k=ref)
    state.avg.add(live)
    return state


def do_lck(transport: RTL8812AUTransport) -> None:
    """Port of rtw8812a_do_lck (rtw8812a.c:81) — LC calibration / VCO re-lock.

    Pauses TX, enters LCK mode (RF_LCK BIT14), triggers via RF_CFGCH bit15, waits
    ~150 ms for the cal to clear the busy flag, restores RF18, exits LCK.
    """
    cont_tx = transport.read32(C.REG_SINGLE_TONE_CONT_TX) & C.SINGLE_TONE_CONT_TX_MASK
    lc_cal = read_rf(transport, C.RF_CFGCH, RFREG_MASK, path="a")
    if not cont_tx:
        transport.write8(C.REG_TXPAUSE, 0xFF)
    write_rf_masked(transport, C.RF_LCK, C.RF_LCK_EN, 1, path="a")
    write_rf_masked(transport, C.RF_CFGCH, C.RF_CFGCH_LCK_TRIG, 1, path="a")
    time.sleep(C.LCK_SETTLE_MS / 1000.0)
    for _ in range(5):
        if read_rf(transport, C.RF_CFGCH, C.RF_CFGCH_LCK_TRIG, path="a") != 1:
            break
        time.sleep(0.010)
    else:
        logger.debug("LCK busy flag did not clear")
    write_rf_masked(transport, C.RF_CFGCH, RFREG_MASK, lc_cal, path="a")
    write_rf_masked(transport, C.RF_LCK, C.RF_LCK_EN, 0, path="a")
    if not cont_tx:
        transport.write8(C.REG_TXPAUSE, 0)
    write_rf_masked(transport, C.RF_CFGCH, RFREG_MASK, lc_cal, path="a")


def pwrtrack_step(transport: RTL8812AUTransport, state: PwrTrackState) -> bool:
    """One tick — read the thermal meter (telemetry) and run LCK (VCO re-lock) on
    a fixed cadence, DECOUPLED from the kernel's need_iqk gate.

    rtw88 only fires do_lck when the thermal drift reaches iqk_threshold (8) from
    the efuse baseline; the VCO drift during fast hopping stays below that, so the
    kernel's LCK never runs and the synth drifts out of lock. We re-center every
    LCK_PERIOD_TICKS ticks instead. Returns True if LCK ran this tick.
    """
    state.avg.add(read_thermal(transport))
    state.ticks_since_lck += 1
    if state.ticks_since_lck >= C.LCK_PERIOD_TICKS:
        state.ticks_since_lck = 0
        do_lck(transport)
        logger.debug("PwrTrack: periodic LCK (VCO re-lock); thermal avg=%d", state.avg.read())
        return True
    return False
