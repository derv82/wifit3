"""RTL8814AU dynamic mechanism — DIG (Dynamic Initial Gain) watchdog.

Ports the RX-relevant slice of `rtw_phy_dynamic_mechanism` (rtw88 core phy.c),
which the kernel runs every `RTW_WATCH_DOG_DELAY_TIME` (HZ*2 = **2 s**) and which
our driver did not run at all. The OFDM receiver initial-gain index (IGI) is
walked from the false-alarm (FA) count: low FA -> lower IGI (more sensitive),
high FA -> raise IGI (reject noise). Left un-run, IGI sits at the AGC-table
default forever, so whether the RX hears depends on the boot analog gain state —
the ~50% deaf-boot lottery the 8x `phy_set_param` re-roll was papering over.

Monitor deviation [[feedback_monitor_mode_deviation]]: there are no associated
STAs, so `min_rssi` is unknown and we run the kernel's **no-link / coverage**
path (`linked=false`): IGI clamped to [DIG_CVRG_MIN=0x1c, DIG_CVRG_MAX=0x2a],
FA thresholds 2000/4000/5000, step {+4,+3,+2} then -2. We additionally seed IGI
at DIG_CVRG_MIN (max coverage, mirrors `rtw_phy_dig_set_max_coverage`) so a cold
boot starts maximally sensitive and only backs off under real FA load.

Intentionally omitted from the core algorithm:
  - `rtw_phy_dig_check_damping`: a linked-mode oscillation guard keyed on the
    performance FA thresholds (250/500) and a *changing* min_rssi. In the monitor
    coverage path min_rssi is constant and the bounds/steps already keep IGI
    motion small and self-limiting, so it is a no-op here.
  - RSSI/rate/thermal/TX tracking (ra/cfo/dpk/pwr/tx_path_div): not RX-relevant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import constants as C
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)


def read_total_fa_cnt(transport: RTL8814AUTransport) -> int:
    """Port of rtw8814a_false_alarm_statistics' FA accounting + counter reset.

    Returns total_fa_cnt = ofdm_fa + (cck_fa if CCK demod enabled), then resets
    the FA/CCA/CRC counters so the next 2 s window starts clean.
    """
    cck_enabled = bool(transport.read32(C.REG_RXPSEL) & C.BIT_RXPSEL_CCK_EN)
    cck_fa = transport.read16(C.REG_FA_CCK)
    ofdm_fa = transport.read16(C.REG_FA_OFDM)
    total_fa = ofdm_fa + (cck_fa if cck_enabled else 0)

    # Reset (tail of rtw8814a_false_alarm_statistics).
    transport.write32_set(C.REG_FAS, 1 << 17)
    transport.write32_clr(C.REG_FAS, 1 << 17)
    transport.write32_clr(C.REG_CCK0_FAREPORT, 1 << 15)
    transport.write32_set(C.REG_CCK0_FAREPORT, 1 << 15)
    transport.write32_set(C.REG_CNTRST, 1 << 0)
    transport.write32_clr(C.REG_CNTRST, 1 << 0)
    return total_fa


def dig_write(transport: RTL8814AUTransport, igi: int) -> None:
    """rtw_phy_dig_write — write IGI to all 4 OFDM paths (8814a dig_cck=NULL)."""
    for addr in C.REG_DIG_PATH:
        transport.write32_mask(addr, C.DIG_IGI_MASK, igi & C.DIG_IGI_MASK)


@dataclass
class DigState:
    """DIG history (igi_history[0] = pre_igi; rest unused without damping)."""
    igi: int = C.DIG_CVRG_MIN
    history: list[int] = field(default_factory=lambda: [C.DIG_CVRG_MIN] * 4)


def dig_init(transport: RTL8814AUTransport) -> DigState:
    """Seed DIG (mirrors rtw_phy_init's igi read) and set IGI to max coverage.

    The kernel seeds igi_history[0] from the dig[0] register (AGC-table default);
    for monitor we instead start at DIG_CVRG_MIN (most sensitive) so a deaf-prone
    boot opens with maximum coverage, then let the watchdog back off under FA.
    """
    igi = C.DIG_CVRG_MIN
    dig_write(transport, igi)
    logger.info("DIG init: IGI seeded to 0x%02x (max coverage)", igi)
    return DigState(igi=igi, history=[igi] * 4)


def dig_step(transport: RTL8814AUTransport, state: DigState, fa_cnt: int) -> None:
    """One DIG tick — no-link (coverage) path of rtw_phy_dig.

    cur_igi = pre_igi (+step on the first crossed FA threshold) - 2, clamped to
    the coverage bounds. Writes only when IGI changes (as the kernel does).
    """
    pre_igi = state.history[0]
    cur_igi = pre_igi

    # Test FA from the highest threshold first; step is offset by -2 (compensated
    # below) so a quiet band (fa < LOW) drifts IGI down toward max sensitivity.
    if fa_cnt > C.DIG_CVRG_FA_TH_EXTRA_HIGH:
        cur_igi += 4
    elif fa_cnt > C.DIG_CVRG_FA_TH_HIGH:
        cur_igi += 3
    elif fa_cnt > C.DIG_CVRG_FA_TH_LOW:
        cur_igi += 2
    cur_igi -= 2

    # Coverage-mode boundary (linked=false): lower=DIG_CVRG_MIN,
    # upper=clamp(min+OFFSET, min, DIG_CVRG_MAX). min_rssi == dig_min here.
    lower = C.DIG_CVRG_MIN
    upper = min(C.DIG_CVRG_MAX, C.DIG_CVRG_MIN + C.DIG_RSSI_GAIN_OFFSET)
    cur_igi = max(lower, min(cur_igi, upper))

    # Record history (igi_bitmap/damping omitted — see module docstring).
    state.history = [cur_igi] + state.history[:3]

    if cur_igi != pre_igi:
        dig_write(transport, cur_igi)
        state.igi = cur_igi
        logger.debug("DIG: fa=%d IGI 0x%02x -> 0x%02x", fa_cnt, pre_igi, cur_igi)
