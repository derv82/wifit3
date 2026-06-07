"""RTL8188EUS runtime DIG/AGC watchdog (M12) — vendor faithful, no-link path.

Ports the phydm DIG watchdog [SRC] phydm_dig.c for the always-monitor (never-linked) case:
hold + read the false-alarm (FA) counters, reset them, step the initial gain index (IGI)
toward fewer false alarms, clamp to the no-link bounds, and write it back. Run every ~2 s
(the kernel DIG cadence). This adapts the M7 InitHalDm DIG *seed* (IGI 0x20) to the live RF
environment — the per-AP reception the re-port exists to win (the seed alone reads *below*
the mainline port; only the adapting watchdog can step the IGI down to a more sensitive
floor). It only reads FA counters and writes the RX gain (no TX), so it is passive.

8188e is 11N (1T1R), so this differs from the 11AC siblings: FA is `cnt_all` = the six OFDM
sub-counters (fast_fsync + sb_search_fail + parity_fail + rate_illegal + crc8_fail + mcs_fail,
from 0xCF0/0xDA0/0xDA4/0xDA8) plus the CCK FA (0xA5C low byte | 0xA58 high byte), read under a
hold and cleared by the 11N reset sequence [SRC] odm_false_alarm_counter_statistics /
phydm_false_alarm_counter_reg_{hold,reset}. The single IGI lives at 0xC50.

No-link DIG parameters [SRC] phydm_dig.c:
  fa_th  = {2000, 4000, 5000}   odm_fa_threshold_check (!linked, !dfs)
  step   = {+2, +1, -2}         phydm_dig_igi_start_value (!linked)
  bounds = [0x1c, 0x2a]         DIG_MIN_COVERAGE .. DIG_MAX_OF_MIN_BALANCE_MODE
The IGI is clamped to [0x1c, 0x2a], so an over/under-count only nudges gain within a safe
band — it can never drive RX deaf. Not pcap-verifiable (runtime/environment); validated by
the beacon-watch A/B (DIG on vs off, vs mainline).
"""
from __future__ import annotations

from typing import NamedTuple

from . import bb

_REG_IGI = 0x0C50              # ODM_REG_IGI_A_11N
_IGI_MASK = 0x7F

# 11N FA counters [SRC] phydm_regdefine11n.h.
_REG_OFDM_FA_TYPE1 = 0x0CF0   # [15:0] fast_fsync, [31:16] sb_search_fail
_REG_OFDM_FA_TYPE2 = 0x0DA0   # [31:16] parity_fail (low half = ofdm_cca, unused)
_REG_OFDM_FA_TYPE3 = 0x0DA4   # [15:0] rate_illegal, [31:16] crc8_fail
_REG_OFDM_FA_TYPE4 = 0x0DA8   # [15:0] mcs_fail
_REG_CCK_FA_LSB = 0x0A5C      # byte0 = cck_fail low
_REG_CCK_FA_MSB = 0x0A58      # byte3 = cck_fail high

_FA_TH = (2000, 4000, 5000)
_IGI_MIN = 0x1C
_IGI_MAX = 0x2A
WATCHDOG_PERIOD_S = 2.0        # kernel DIG-watchdog cadence


class DigTick(NamedTuple):
    """One watchdog iteration's outcome (for the driver's debug log). A working 2 s window
    reads hundreds-to-low-thousands of FA that bounce; a counter that is not being reset
    climbs monotonically toward the 16-bit ceiling."""
    igi: int
    fa_cnt: int
    ofdm_fa: int
    cck_fa: int


def _hold_fa_cnt(t) -> None:
    """``phydm_false_alarm_counter_reg_hold`` (11N) — freeze the counters before reading."""
    bb.set_bb_reg(t, 0x0C00, 1 << 31, 1)   # hold page-C OFDM counter
    bb.set_bb_reg(t, 0x0D00, 1 << 31, 1)   # hold page-D OFDM counter
    bb.set_bb_reg(t, 0x0A2C, 1 << 12, 1)   # hold CCK CCA counter
    bb.set_bb_reg(t, 0x0A2C, 1 << 14, 1)   # hold CCK FA counter


def _read_fa_cnt(t):
    """``odm_false_alarm_counter_statistics`` (11N) — cnt_all = the six OFDM FA sub-counters
    + CCK FA. Returns ``(cnt_all, ofdm_fa, cck_fa)``."""
    v1 = t.read32(_REG_OFDM_FA_TYPE1)
    ofdm = (v1 & 0xFFFF) + ((v1 >> 16) & 0xFFFF)            # fast_fsync + sb_search_fail
    ofdm += (t.read32(_REG_OFDM_FA_TYPE2) >> 16) & 0xFFFF   # parity_fail
    v3 = t.read32(_REG_OFDM_FA_TYPE3)
    ofdm += (v3 & 0xFFFF) + ((v3 >> 16) & 0xFFFF)           # rate_illegal + crc8_fail
    ofdm += t.read32(_REG_OFDM_FA_TYPE4) & 0xFFFF           # mcs_fail
    cck = (t.read32(_REG_CCK_FA_LSB) & 0xFF) | (((t.read32(_REG_CCK_FA_MSB) >> 24) & 0xFF) << 8)
    return ofdm + cck, ofdm, cck


def _reset_fa_cnt(t) -> None:
    """``phydm_false_alarm_counter_reg_reset`` (11N) — clear the counters + un-hold/resume."""
    bb.set_bb_reg(t, 0x0C0C, 1 << 31, 1)
    bb.set_bb_reg(t, 0x0C0C, 1 << 31, 0)        # reset OFDM FA
    bb.set_bb_reg(t, 0x0D00, 1 << 27, 1)
    bb.set_bb_reg(t, 0x0D00, 1 << 27, 0)
    bb.set_bb_reg(t, 0x0D00, 1 << 31, 0)        # update + resume page-C/D OFDM counter
    bb.set_bb_reg(t, 0x0D00, 1 << 31, 0)
    bb.set_bb_reg(t, 0x0A2C, (1 << 13) | (1 << 12), 0)
    bb.set_bb_reg(t, 0x0A2C, (1 << 13) | (1 << 12), 2)   # reset + resume CCK CCA
    bb.set_bb_reg(t, 0x0A2C, (1 << 15) | (1 << 14), 0)
    bb.set_bb_reg(t, 0x0A2C, (1 << 15) | (1 << 14), 2)   # reset + resume CCK FA
    bb.set_bb_reg(t, 0x0F14, 1 << 16, 1)
    bb.set_bb_reg(t, 0x0F14, 1 << 16, 0)        # reset CRC32 counter


def _new_igi_by_fa(igi: int, fa_cnt: int) -> int:
    """[SRC] phydm_dig_new_igi_by_fa with the not-linked step {+2, +1, -2}."""
    if fa_cnt > _FA_TH[2]:
        return igi + 2
    if fa_cnt > _FA_TH[1]:
        return igi + 1
    if fa_cnt < _FA_TH[0]:
        return igi - 2
    return igi


def watchdog_tick(t) -> DigTick:
    """One DIG watchdog iteration: read IGI, hold+read+reset FA, pick a new IGI by FA,
    clamp to the no-link range, and (only if changed) write it via odm_write_dig (0xC50)."""
    igi = t.read32(_REG_IGI) & _IGI_MASK
    _hold_fa_cnt(t)
    fa_cnt, ofdm_fa, cck_fa = _read_fa_cnt(t)
    _reset_fa_cnt(t)
    new_igi = max(_IGI_MIN, min(_IGI_MAX, _new_igi_by_fa(igi, fa_cnt)))
    if new_igi != igi:
        bb.set_bb_reg(t, _REG_IGI, _IGI_MASK, new_igi)
    return DigTick(new_igi, fa_cnt, ofdm_fa, cck_fa)
