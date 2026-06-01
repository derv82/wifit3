"""RTL8822BU dynamic mechanism — DIG (Dynamic Initial Gain) watchdog.

Ports the RX-relevant slice of the kernel's `rtw_watch_dog_work`, which fires
every `RTW_WATCH_DOG_DELAY_TIME` (HZ*2 = **2 s**) and runs
`rtw_phy_dynamic_mechanism` -> `rtw_phy_dig`. DIG walks the OFDM receiver
initial-gain index (IGI) from the per-window false-alarm (FA) count: high FA
raises IGI (back off gain, reject noise/saturation), low FA lowers it (more
sensitive). Without it IGI stays at the AGC-table default for the whole
session, so RX quality is whatever the boot analog gain state happens to be —
either deaf to weak APs or saturating on a strong nearby one
(`phy_status pwdb` pinned high). [WIRE captures_rtw88_8822bu/capture-1: the
airmon-ng driver reads FA at 0xa5c/0xf48 and rewrites IGI at 0xc50/0xe50 on a
~2 s cadence, frames 19870-21021.]

Monitor deviation [[feedback_monitor_mode_deviation]]: there are no associated
STAs (`sta_cnt == 0`), so we run the kernel's **no-link / coverage** path of
`rtw_phy_dig` (`linked=false`): IGI clamped to [DIG_CVRG_MIN=0x1c,
DIG_CVRG_MAX=0x2a], FA thresholds 2000/4000/5000, step {+4,+3,+2} then -2.

Seed (kernel-faithful): `dig_init` reads dig[0] (0xc50) for the starting IGI,
exactly as `rtw_phy_init` seeds `igi_history[0]`. This is the AGC-table default;
the FA-driven watchdog then converges from there. (The 8814au port instead seeds
DIG_CVRG_MIN to fight a deaf-boot; the 8822b symptom is the opposite — gain too
high, hence saturation — so the faithful AGC-default seed lets the FA loop raise
IGI toward 0x2a and back the gain off.)

Intentionally omitted from the core algorithm:
  - `rtw_phy_dig_check_damping`: a linked-mode oscillation guard keyed on the
    performance FA thresholds (250/500) and a *changing* min_rssi. In the
    coverage path min_rssi is constant and the bounds/steps already keep IGI
    motion small and self-limiting, so it is a no-op here.
  - The CRC-ok / CCA stats reads (0xf04/0xf14/0xf10/0xf0c/0xf08/0xfcc) in
    `rtw8822b_false_alarm_statistics`: they feed rate adaptation / CCK-PD, not
    DIG. The counter *reset* below clears them along with the FA counters.
  - RSSI/rate/thermal/TX tracking (ra/cfo/dpk/pwr): not RX-relevant.

References:
    rtw8822b.c:1023   rtw8822b_false_alarm_statistics (FA regs + reset seq)
    rtw8822b.c:2097   rtw8822b_dig[]  (IGI paths, mask 0x7f; dig_cck NULL)
    rtw8822b.c:2542   .dig_min = 0x1c
    phy.c:236         rtw_phy_init    (igi_history[0] = read(dig[0]))
    phy.c:263         rtw_phy_dig_write
    phy.c:360-371     DIG_CVRG_* threshold/bound constants
    phy.c:536         rtw_phy_dig     (the per-tick algorithm)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .transport import RTL8822BUTransport

logger = logging.getLogger(__name__)

# --- DIG IGI write paths (rtw8822b_dig[], rtw8822b.c:2097) -----------------
# Two OFDM paths (rf_path_num=2), mask 0x7f. 8822b has dig_cck == NULL, so
# there is no CCK-IGI sub-write (unlike chips that carry a dig_cck reg).
REG_DIG_PATH = (0x0C50, 0x0E50)
DIG_IGI_MASK = 0x7F

# --- False-alarm counters (rtw8822b_false_alarm_statistics, rtw8822b.c:1023)
REG_CCK_DEMOD = 0x0808           # BIT(28) set => CCK demod enabled
BIT_CCK_EN = 1 << 28
REG_FA_CCK = 0x0A5C              # CCK false-alarm count   (read16)
REG_FA_OFDM = 0x0F48             # OFDM false-alarm count  (read16)

# --- Coverage-mode (no-link) algorithm constants (phy.c:365-371) -----------
# 8822b chip->dig_min == 0x1c (rtw8822b.c:2542), == DIG_CVRG_MIN here.
DIG_CVRG_MIN = 0x1C
DIG_CVRG_MAX = 0x2A
DIG_RSSI_GAIN_OFFSET = 15
DIG_CVRG_FA_TH_LOW = 2000
DIG_CVRG_FA_TH_HIGH = 4000
DIG_CVRG_FA_TH_EXTRA_HIGH = 5000


def read_total_fa_cnt(transport: RTL8822BUTransport) -> int:
    """Port of rtw8822b_false_alarm_statistics' FA accounting + counter reset.

    Returns total_fa_cnt = ofdm_fa + (cck_fa if CCK demod enabled), then resets
    the FA/CCA/CRC counters so the next 2 s window starts clean. The set/clr
    order differs per reset register, so the three toggles are written out
    verbatim rather than looped (rtw8822b.c:1063-1068).
    """
    cck_enabled = bool(transport.read32(REG_CCK_DEMOD) & BIT_CCK_EN)
    cck_fa = transport.read16(REG_FA_CCK)
    ofdm_fa = transport.read16(REG_FA_OFDM)
    total_fa = ofdm_fa + (cck_fa if cck_enabled else 0)

    # Reset the BB statistics counters for the next window.
    transport.write32_set(0x9A4, 1 << 17)
    transport.write32_clr(0x9A4, 1 << 17)
    transport.write32_clr(0xA2C, 1 << 15)
    transport.write32_set(0xA2C, 1 << 15)
    transport.write32_set(0xB58, 1 << 0)
    transport.write32_clr(0xB58, 1 << 0)
    return total_fa


def dig_write(transport: RTL8822BUTransport, igi: int) -> None:
    """rtw_phy_dig_write — write IGI to both OFDM paths (8822b dig_cck=NULL)."""
    for addr in REG_DIG_PATH:
        transport.write32_mask(addr, DIG_IGI_MASK, igi & DIG_IGI_MASK)


@dataclass
class DigState:
    """DIG history (history[0] = pre_igi; the rest are unused without the
    omitted damping guard, but kept to mirror the kernel's igi_history[4])."""
    igi: int = DIG_CVRG_MIN
    history: list[int] = field(default_factory=lambda: [DIG_CVRG_MIN] * 4)


def dig_init(transport: RTL8822BUTransport) -> DigState:
    """Seed DIG from the AGC-table default, mirroring rtw_phy_init.

    The kernel reads dig[0] (0xc50) into igi_history[0] and does not write IGI
    here — the AGC table already programmed it, and set_channel's toggle_igi
    re-latches the same value. We do the same: read the current IGI as the
    starting point and let the watchdog converge from there.
    """
    igi = transport.read32(REG_DIG_PATH[0]) & DIG_IGI_MASK
    logger.info("DIG init: seeded IGI from AGC default 0x%02x", igi)
    return DigState(igi=igi, history=[igi] * 4)


def dig_step(transport: RTL8822BUTransport, state: DigState, fa_cnt: int) -> None:
    """One DIG tick — no-link (coverage) path of rtw_phy_dig.

    cur_igi = pre_igi (+step on the first crossed FA threshold) - 2, clamped to
    the coverage bounds. Writes only when IGI changes (as the kernel does).
    """
    pre_igi = state.history[0]
    cur_igi = pre_igi

    # Test FA from the highest threshold first; the step is offset by -2
    # (compensated below) so a quiet band (fa < LOW) drifts IGI toward max
    # sensitivity while a noisy/saturating one raises it to back the gain off.
    if fa_cnt > DIG_CVRG_FA_TH_EXTRA_HIGH:
        cur_igi += 4
    elif fa_cnt > DIG_CVRG_FA_TH_HIGH:
        cur_igi += 3
    elif fa_cnt > DIG_CVRG_FA_TH_LOW:
        cur_igi += 2
    cur_igi -= 2

    # Coverage-mode boundary (linked=false): lower=DIG_CVRG_MIN,
    # upper=clamp(min+OFFSET, min, DIG_CVRG_MAX). min_rssi == dig_min here.
    lower = DIG_CVRG_MIN
    upper = min(DIG_CVRG_MAX, DIG_CVRG_MIN + DIG_RSSI_GAIN_OFFSET)
    cur_igi = max(lower, min(cur_igi, upper))

    # Record history (igi_bitmap/damping omitted — see module docstring).
    state.history = [cur_igi] + state.history[:3]

    if cur_igi != pre_igi:
        dig_write(transport, cur_igi)
        state.igi = cur_igi
        logger.debug("DIG: fa=%d IGI 0x%02x -> 0x%02x", fa_cnt, pre_igi, cur_igi)
