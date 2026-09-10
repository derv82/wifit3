"""RTL8822C halrf calibrations, run once at cold boot from ``halrf_init``.

Every write here is computed from a readback, so the values are never transcribed from a
capture: reproducing them depends on porting the arithmetic, not the recorded numbers.
[SRC hal/phydm/halrf/halrf.c:2900, hal/phydm/halrf/rtl8822c/halrf_8822c.c]
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .firmware import H2cState, fill_h2c_cmd
from .kfree import PowerTrimState, config_new_kfree
from .tssi import tssi_dck
from .dm import PhydmState, dm_init
from .txgapk import TxGapKState, save_all_tx_gain_table
from .phy import MASKDWORD, get_bb_reg, get_rf_reg, set_bb_reg, set_rf_reg
from .transport import RTL8822CUTransport

SN = 100                    # samples averaged per measurement [SRC halrf/halrf.h:471]

# halrf_dac_cal_8822c's register backup list [SRC halrf_8822c.c:829-833]
_BP_REG = (0x180C, 0x1810, 0x410C, 0x4110, 0x1C3C, 0x1C24, 0x1D70, 0x09B4,
           0x1A00, 0x1A14, 0x1D58, 0x1C38, 0x1E24, 0x1E28, 0x1860, 0x4160)
_BP_RFREG = (0x8F,)

_SAMPLE_OVERFLOW = 0x64     # halrf_compare_8822c rejects a sample above this magnitude


@dataclass
class DackState:
    """halrf_dack_info: the calibration result, kept so a second halrf_init restores it
    instead of re-running the full DACK. [SRC halrf/halrf_iqk.h:65]"""
    dack_en: bool = False
    msbk_d: list = field(default_factory=lambda: [[[0] * 15 for _ in range(2)] for _ in range(2)])
    dck_d: list = field(default_factory=lambda: [[[0] * 2 for _ in range(2)] for _ in range(2)])
    biask_d: list = field(default_factory=lambda: [[0] * 2 for _ in range(2)])


def _is_negative(value: int) -> bool:
    """Samples are 10-bit two's complement, so 0x200 and up is the negative half."""
    return value >= 0x200


def _compare(value: int) -> bool:
    """halrf_compare_8822c: reject a sample whose magnitude overflows.
    [SRC halrf_8822c.c:328]"""
    if _is_negative(value):
        return (0x400 - value) > _SAMPLE_OVERFLOW
    return value > _SAMPLE_OVERFLOW


def _sorts_after(v1: int, v2: int) -> bool:
    """halrf_bubble_8822c's ordering: negatives sort below positives, numeric within each
    half. [SRC halrf_8822c.c:270]"""
    if _is_negative(v1) == _is_negative(v2):
        return v1 > v2
    return not _is_negative(v1)


def _b_sort(iv: list[int], qv: list[int]) -> None:
    """halrf_b_sort_8822c: bubble-sort I and Q independently. [SRC halrf_8822c.c:285]"""
    for i in range(SN - 1):
        for j in range(SN - 1 - i):
            if _sorts_after(iv[j], iv[j + 1]):
                iv[j], iv[j + 1] = iv[j + 1], iv[j]
            if _sorts_after(qv[j], qv[j + 1]):
                qv[j], qv[j + 1] = qv[j + 1], qv[j]


def _minmax_compare(value: int, mn: int, mx: int) -> tuple[int, int]:
    """halrf_minmax_compare_8822c, under the same ordering as _sorts_after.
    [SRC halrf_8822c.c:299]"""
    if _is_negative(value):
        mn = min(mn, value) if _is_negative(mn) else value
        if _is_negative(mx):
            mx = max(mx, value)
    else:
        if not _is_negative(mn):
            mn = min(mn, value)
        mx = value if _is_negative(mx) else max(mx, value)
    return mn, mx


def _span(mn: int, mx: int) -> int:
    """The min-to-max distance, wrapping when the two straddle the sign boundary."""
    if _is_negative(mx) == _is_negative(mn):
        return mx - mn
    return mx + (0x400 - mn)


def _trimmed_mean(v: list[int]) -> int:
    """The 10-bit two's-complement mean of the middle 80 sorted samples.
    [SRC halrf_8822c.c:460-479]"""
    m = p = 0
    for value in v[10:SN - 10]:
        if value > 0x200:               # strict, so 0x200 itself counts as positive
            m += 0x400 - value
        else:
            p += value
    if p > m:
        return (p - m) // (SN - 20)
    t = (m - p) // (SN - 20)
    return 0x400 - t if t else 0


def _read_sample(t: RTL8822CUTransport) -> tuple[int, int]:
    value = get_bb_reg(t, 0x2DBC, 0x3FFFFF)
    return (value & 0x3FF000) >> 12, value & 0x3FF


def measure(t: RTL8822CUTransport) -> tuple[int, int]:
    """halrf_mode_8822c: collect SN in-range samples of the I/Q monitor, replace the extremes
    until their spread settles, and return the trimmed mean of each.
    [SRC halrf_8822c.c:342]"""
    iv: list[int] = []
    qv: list[int] = []
    for _ in range(10000):
        if len(iv) == SN:
            break
        i_sample, q_sample = _read_sample(t)
        if not (_compare(i_sample) or _compare(q_sample)):
            iv.append(i_sample)
            qv.append(q_sample)
    for _ in range(100):
        i_min = i_max = iv[0]
        q_min = q_max = qv[0]
        for i_sample, q_sample in zip(iv, qv):
            i_min, i_max = _minmax_compare(i_sample, i_min, i_max)
            q_min, q_max = _minmax_compare(q_sample, q_min, q_max)
        _b_sort(iv, qv)                 # sorted before the test, so the replacements below
        if _span(i_min, i_max) <= 5 and _span(q_min, q_max) <= 5:   # land on the extremes
            break
        iv[0], qv[0] = _read_sample(t)
        iv[SN - 1], qv[SN - 1] = _read_sample(t)
    return _trimmed_mean(iv), _trimmed_mean(qv)


def _poll(t: RTL8822CUTransport, address: int, mask: int, data: int) -> None:
    """halrf_polling_check_8822c: spin without a delay, and give up silently.
    [SRC halrf_8822c.c:807]"""
    for _ in range(100000):
        if get_bb_reg(t, address, mask) == data:
            return


@dataclass(frozen=True)
class _PathRegs:
    """The per-path register banks the DACK drives. Path A and path B run the same sequence
    against these two sets. [SRC halrf_8822c.c:869-1064 vs :1074-1265]"""
    rfe: int                # 0x1830 / 0x4130   RF interface
    gain: int               # 0x1860 / 0x4160
    trx: int                # 0x180c / 0x410c
    trx1: int               # 0x1810 / 0x4110
    adck: int               # 0x1868 / 0x4168   ADC compensation word
    i_ctrl: int             # 0x18b0 / 0x41b0   I-branch DAC control
    i_trig: int             # 0x18b8 / 0x41b8
    i_dck: int              # 0x18bc / 0x41bc
    i_dck1: int             # 0x18c0 / 0x41c0
    q_ctrl: int             # 0x18cc / 0x41cc
    q_trig: int             # 0x18d4 / 0x41d4
    q_dck: int              # 0x18d8 / 0x41d8
    q_dck1: int             # 0x18dc / 0x41dc
    i_msbk: int             # 0x2808 / 0x4508   MSBK-done readback
    q_msbk: int             # 0x2834 / 0x4534
    i_msbk_val: int         # 0x2810 / 0x4510   MSBK / bias-K table readback
    q_msbk_val: int         # 0x283c / 0x453c
    i_code: int             # 0x2824 / 0x4524   DAC-code readback
    q_code: int             # 0x2850 / 0x4550
    dbg_sel: int            # 0x1c3c value selecting this path's monitor
    fifo_sel: int           # 0x1b00 value selecting this path's FIFO


_PATH_A = _PathRegs(rfe=0x1830, gain=0x1860, trx=0x180C, trx1=0x1810, adck=0x1868,
                    i_ctrl=0x18B0, i_trig=0x18B8, i_dck=0x18BC, i_dck1=0x18C0,
                    q_ctrl=0x18CC, q_trig=0x18D4, q_dck=0x18D8, q_dck1=0x18DC,
                    i_msbk=0x2808, q_msbk=0x2834, i_msbk_val=0x2810, q_msbk_val=0x283C,
                    i_code=0x2824, q_code=0x2850,
                    dbg_sel=0x00088003, fifo_sel=0x00000008)
_PATH_B = _PathRegs(rfe=0x4130, gain=0x4160, trx=0x410C, trx1=0x4110, adck=0x4168,
                    i_ctrl=0x41B0, i_trig=0x41B8, i_dck=0x41BC, i_dck1=0x41C0,
                    q_ctrl=0x41CC, q_trig=0x41D4, q_dck=0x41D8, q_dck1=0x41DC,
                    i_msbk=0x4508, q_msbk=0x4534, i_msbk_val=0x4510, q_msbk_val=0x453C,
                    i_code=0x4524, q_code=0x4550,
                    dbg_sel=0x000A8003, fifo_sel=0x0000000A)


def _adck_loop(t: RTL8822CUTransport, r: _PathRegs) -> tuple[int, int, int]:
    """Converge the ADC offset: measure, negate into a compensation word, write it back and
    re-measure until the residual is under 5 LSB. [SRC halrf_8822c.c:877-911]"""
    adc_ic = adc_qc = 0
    temp = 0
    for _ in range(10):
        set_bb_reg(t, 0x1C3C, MASKDWORD, r.dbg_sel)
        set_bb_reg(t, 0x1C24, MASKDWORD, 0x00010002)
        ic, qc = measure(t)
        if ic:
            ic = 0x400 - ic
            adc_ic = ic
        if qc:
            qc = 0x400 - qc
            adc_qc = qc
        temp = (ic & 0x3FF) | ((qc & 0x3FF) << 10)
        set_bb_reg(t, r.adck, MASKDWORD, temp)
        set_bb_reg(t, 0x1C3C, MASKDWORD, r.dbg_sel | 0x100)
        ic, qc = measure(t)
        if _is_negative(ic):
            ic = 0x400 - ic
        if _is_negative(qc):
            qc = 0x400 - qc
        if ic < 5 and qc < 5:
            break
    return temp, adc_ic, adc_qc


def _dac_code(value: int) -> int:
    """Turn a measured DC offset into the 8-bit DAC trim code the hardware expects.
    [SRC halrf_8822c.c:974-991]"""
    if value:
        value = 0x400 - value
    if value < 0x300:
        return (value * 2 * 6 // 5 + 0x80) & 0xFFFFFFFF
    return (0x7F - (0x400 - value) * 2 * 6 // 5) & 0xFFFFFFFF


def _dack_loop(t: RTL8822CUTransport, r: _PathRegs, temp: int, adc_ic: int, adc_qc: int) -> None:
    """Converge the DAC offset: settle the DAC, measure the residual, convert it to an I/Q
    trim code, program it and confirm it read back, then re-measure through the ADC
    compensation word until the residual is under 5 LSB. [SRC halrf_8822c.c:920-1064]"""
    for _ in range(10):
        set_bb_reg(t, r.adck, MASKDWORD, temp)
        set_bb_reg(t, r.trx, MASKDWORD, 0xDFF00220)
        if r is _PATH_A:                        # path B re-arms neither of these
            set_bb_reg(t, r.gain, MASKDWORD, 0xF0040FF0)
            set_bb_reg(t, 0x1C38, MASKDWORD, 0xFFFFFFFF)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02D508C5)
        set_bb_reg(t, 0x09B4, MASKDWORD, 0xDB66DB00)
        set_bb_reg(t, r.i_ctrl, MASKDWORD, 0x0A11FB88)
        set_bb_reg(t, r.i_dck, MASKDWORD, 0x0008FF81)
        set_bb_reg(t, r.i_dck1, MASKDWORD, 0x0003D208)
        set_bb_reg(t, r.q_ctrl, MASKDWORD, 0x0A11FB88)
        set_bb_reg(t, r.q_dck, MASKDWORD, 0x0008FF81)
        set_bb_reg(t, r.q_dck1, MASKDWORD, 0x0003D208)
        set_bb_reg(t, r.i_trig, MASKDWORD, 0x60000000)
        time.sleep(0.002)
        set_bb_reg(t, r.i_dck, MASKDWORD, 0x000AFF8D)
        time.sleep(0.002)
        set_bb_reg(t, r.i_ctrl, MASKDWORD, 0x0A11FB89)
        set_bb_reg(t, r.q_ctrl, MASKDWORD, 0x0A11FB89)
        time.sleep(0.001)
        set_bb_reg(t, r.i_trig, MASKDWORD, 0x62000000)
        set_bb_reg(t, r.q_trig, MASKDWORD, 0x62000000)
        time.sleep(0.001)
        _poll(t, r.i_msbk, 0x7FFF80, 0xFFFF)
        _poll(t, r.q_msbk, 0x7FFF80, 0xFFFF)
        set_bb_reg(t, r.i_trig, MASKDWORD, 0x02000000)
        time.sleep(0.001)
        set_bb_reg(t, r.i_dck, MASKDWORD, 0x0008FF87)
        set_bb_reg(t, 0x09B4, MASKDWORD, 0xDB6DB600)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02D508C5)
        set_bb_reg(t, r.i_dck, MASKDWORD, 0x0008FF87)
        set_bb_reg(t, r.gain, MASKDWORD, 0xF0000000)
        set_bb_reg(t, r.i_dck, 0xF0000000, 0x0)
        set_bb_reg(t, r.i_dck1, 0xF, 0x8)
        set_bb_reg(t, r.q_dck, 0xF0000000, 0x0)
        set_bb_reg(t, r.q_dck1, 0xF, 0x8)
        set_bb_reg(t, 0x1B00, MASKDWORD, r.fifo_sel)
        t.write8(0x1BCC, 0x3F)
        set_bb_reg(t, r.trx, MASKDWORD, 0xDFF00220)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02D508C5)
        set_bb_reg(t, 0x1C3C, MASKDWORD, r.dbg_sel | 0x100)
        ic, qc = measure(t)
        ic, qc = _dac_code(ic), _dac_code(qc)
        set_bb_reg(t, r.trx, MASKDWORD, 0xDFF00220)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02D508C5)
        set_bb_reg(t, 0x09B4, MASKDWORD, 0xDB66DB00)
        set_bb_reg(t, r.i_ctrl, MASKDWORD, 0x0A11FB88)
        set_bb_reg(t, r.i_dck, MASKDWORD, 0xC008FF81)
        set_bb_reg(t, r.i_dck1, MASKDWORD, 0x0003D208)
        set_bb_reg(t, r.i_dck, 0xF0000000, ic & 0xF)
        set_bb_reg(t, r.i_dck1, 0xF, (ic & 0xF0) >> 4)
        set_bb_reg(t, r.q_ctrl, MASKDWORD, 0x0A11FB88)
        set_bb_reg(t, r.q_dck, MASKDWORD, 0xE008FF81)
        set_bb_reg(t, r.q_dck1, MASKDWORD, 0x0003D208)
        set_bb_reg(t, r.q_dck, 0xF0000000, qc & 0xF)
        set_bb_reg(t, r.q_dck1, 0xF, (qc & 0xF0) >> 4)
        set_bb_reg(t, r.i_trig, MASKDWORD, 0x60000000)
        time.sleep(0.002)
        set_bb_reg(t, r.i_dck, 0xE, 0x6)
        time.sleep(0.002)
        set_bb_reg(t, r.i_ctrl, MASKDWORD, 0x0A11FB89)
        set_bb_reg(t, r.q_ctrl, MASKDWORD, 0x0A11FB89)
        time.sleep(0.001)
        set_bb_reg(t, r.i_trig, MASKDWORD, 0x62000000)
        set_bb_reg(t, r.q_trig, MASKDWORD, 0x62000000)
        time.sleep(0.001)
        _poll(t, r.i_code, 0x07F80000, ic)
        _poll(t, r.q_code, 0x07F80000, qc)
        set_bb_reg(t, r.i_trig, MASKDWORD, 0x02000000)
        time.sleep(0.001)
        set_bb_reg(t, r.i_dck, 0xE, 0x3)
        set_bb_reg(t, 0x09B4, MASKDWORD, 0xDB6DB600)
        temp1 = ((adc_ic + 0x10) & 0x3FF) | (((adc_qc + 0x10) & 0x3FF) << 10)
        set_bb_reg(t, r.adck, MASKDWORD, temp1)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02D508C5)
        set_bb_reg(t, r.gain, MASKDWORD, 0xF0000000)
        ic, qc = measure(t)
        ic = ic - 0x10 if ic >= 0x10 else 0x400 - (0x10 - ic)
        qc = qc - 0x10 if qc >= 0x10 else 0x400 - (0x10 - qc)
        if _is_negative(ic):
            ic = 0x400 - ic
        if _is_negative(qc):
            qc = 0x400 - qc
        if ic < 5 and qc < 5:
            break


def _dck_backup(t: RTL8822CUTransport, dack: DackState) -> None:
    """halrf_dck_backup_8822c. [SRC halrf_8822c.c:513]"""
    for path, r in enumerate((_PATH_A, _PATH_B)):
        dack.dck_d[path][0][0] = get_bb_reg(t, r.i_dck, 0xF0000000)
        dack.dck_d[path][0][1] = get_bb_reg(t, r.i_dck1, 0xF)
        dack.dck_d[path][1][0] = get_bb_reg(t, r.q_dck, 0xF0000000)
        dack.dck_d[path][1][1] = get_bb_reg(t, r.q_dck1, 0xF)


def _dack_backup(t: RTL8822CUTransport, dack: DackState) -> None:
    """halrf_dack_backup_8822c: read out the per-path MSBK, DCK and bias-K tables so a later
    halrf_init can restore them without re-calibrating. [SRC halrf_8822c.c:528]"""
    temp1 = get_bb_reg(t, 0x1860)
    temp2 = get_bb_reg(t, 0x4160)
    temp3 = get_bb_reg(t, 0x09B4)
    set_bb_reg(t, 0x09B4, MASKDWORD, 0xDB66DB00)
    for path, r in enumerate((_PATH_A, _PATH_B)):
        set_bb_reg(t, r.rfe, 1 << 30, 0)
        set_bb_reg(t, r.gain, 0xFC000000, 0x3C)
        for branch, (ctrl, msbk) in enumerate(((r.i_ctrl, r.i_msbk_val),
                                               (r.q_ctrl, r.q_msbk_val))):
            for i in range(0xF):
                set_bb_reg(t, ctrl, 0xF0000000, i)
                dack.msbk_d[path][branch][i] = get_bb_reg(t, msbk, 0x7FC0000)
    _dck_backup(t, dack)
    set_bb_reg(t, _PATH_A.rfe, 1 << 30, 1)
    set_bb_reg(t, _PATH_B.rfe, 1 << 30, 1)
    set_bb_reg(t, 0x1860, MASKDWORD, temp1)
    set_bb_reg(t, 0x4160, MASKDWORD, temp2)
    set_bb_reg(t, 0x09B4, MASKDWORD, temp3)
    # halrf_biask_backup_8822c truncates a 10-bit field to u8 before storing it; the restore
    # writes those 8 bits back into the 10-bit field. [SRC halrf_8822c.c:502-511]
    for path, r in enumerate((_PATH_A, _PATH_B)):
        dack.biask_d[path][0] = get_bb_reg(t, r.i_msbk_val, 0x1FF8) & 0xFF
        dack.biask_d[path][1] = get_bb_reg(t, r.q_msbk_val, 0x1FF8) & 0xFF


def dac_cal(t: RTL8822CUTransport, dack: DackState) -> None:
    """halrf_dac_cal_8822c: null the ADC and DAC DC offset on both paths, then save the
    result. Skipped on a warm chip, which restores the saved tables instead.
    [SRC halrf_8822c.c:821]"""
    if dack.dack_en:
        raise NotImplementedError("RTL8822CU: halrf_dack_restore_8822c is not ported")
    dack.dack_en = True

    backup = [get_bb_reg(t, address) for address in _BP_REG]
    backup_rf = [[get_rf_reg(t, path, address) for path in (0, 1)] for address in _BP_RFREG]

    set_bb_reg(t, 0x1D58, 0xFF8, 0x1FF)
    set_bb_reg(t, 0x1A00, 0x3, 0x2)
    set_bb_reg(t, 0x1A14, 0x300, 0x3)
    set_bb_reg(t, 0x1D70, MASKDWORD, 0x7E7E7E7E)
    set_bb_reg(t, 0x180C, 0x3, 0x0)
    set_bb_reg(t, 0x410C, 0x3, 0x0)
    set_bb_reg(t, 0x1B00, MASKDWORD, 0x00000008)
    t.write8(0x1BCC, 0x3F)
    set_bb_reg(t, 0x1B00, MASKDWORD, 0x0000000A)
    t.write8(0x1BCC, 0x3F)
    set_bb_reg(t, 0x1E24, 1 << 31, 0x0)
    set_bb_reg(t, 0x1E28, 0xF, 0x3)

    for path, r in enumerate((_PATH_A, _PATH_B)):
        set_bb_reg(t, r.rfe, 1 << 30, 0x0)
        if r is _PATH_B:
            set_bb_reg(t, r.rfe, MASKDWORD, 0x30DB8041)
        set_bb_reg(t, r.gain, MASKDWORD, 0xF0040FF0)
        set_bb_reg(t, r.trx, MASKDWORD, 0xDFF00220)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02DD08C4)
        set_bb_reg(t, r.trx, MASKDWORD, 0x10000260)
        set_rf_reg(t, 0, 0x00, 0xFFFFF, 0x10000)
        set_rf_reg(t, 1, 0x00, 0xFFFFF, 0x10000)
        temp, adc_ic, adc_qc = _adck_loop(t, r)
        set_bb_reg(t, 0x1C3C, MASKDWORD, 0x00000003)
        set_bb_reg(t, r.trx, MASKDWORD, 0x10000260)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02D508C4)
        set_rf_reg(t, path, 0x8F, 1 << 13, 0x1)     # release the IQ-path pull-low switch
        _dack_loop(t, r, temp, adc_ic, adc_qc)
        set_bb_reg(t, r.adck, MASKDWORD, 0x0)
        set_bb_reg(t, r.trx1, MASKDWORD, 0x02D508C4)
        set_bb_reg(t, r.i_dck, 0x1, 0x0)
        set_bb_reg(t, r.rfe, 1 << 30, 0x1)

    set_bb_reg(t, 0x1B00, MASKDWORD, 0x00000008)
    set_bb_reg(t, _PATH_B.rfe, 1 << 30, 0x1)
    t.write8(0x1BCC, 0x00)
    set_bb_reg(t, 0x1B00, MASKDWORD, 0x0000000A)
    t.write8(0x1BCC, 0x00)

    for address, value in zip(_BP_REG, backup):
        set_bb_reg(t, address, MASKDWORD, value)
    for address, values in zip(_BP_RFREG, backup_rf):
        for path in (0, 1):
            set_rf_reg(t, path, address, 0xFFFFF, values[path])
    _dack_backup(t, dack)


ODM_H2C_WIFI_CALIBRATION = 0x6D     # [SRC phydm_interface.h:44]


def rfk_handshake(t: RTL8822CUTransport, h2c: H2cState, *, before_k: bool) -> None:
    """halrf_rfk_handshake_8822c: ask the firmware to keep BT off the shared RF while a
    calibration runs, and wait for it to acknowledge in 0x49c[0]. Before a calibration it also
    waits out any BT-side IQK already in flight (0xa8[22:21]). [SRC halrf_8822c.c:1527]"""
    if before_k:
        for _ in range(30000):          # BT requesting (0xaa[6]) or running (0xaa[5]) an IQK
            if get_bb_reg(t, 0x00A8, (1 << 22) | (1 << 21)) == 0:
                break
            time.sleep(0.00002)
    fill_h2c_cmd(t, h2c, ODM_H2C_WIFI_CALIBRATION, bytes([1 if before_k else 0]))
    for _ in range(5000):
        if get_bb_reg(t, 0x049C, 1) == (1 if before_k else 0):
            break
        time.sleep(0.00002)


def rx_dck_trigger(t: RTL8822CUTransport, h2c: H2cState) -> None:
    """halrf_rx_dck_trigger: the 8822C RX-DCK body is compiled out, so only the handshake that
    brackets it reaches the hardware. [SRC halrf.c:2972, :3007]"""
    rfk_handshake(t, h2c, before_k=True)
    rfk_handshake(t, h2c, before_k=False)


def x2k_check(t: RTL8822CUTransport) -> None:
    """phy_x2_check_8822c: re-kick the X2 synthesizer if it is still reporting busy.
    [SRC halrf_8822c.c:1407]"""
    time.sleep(0.001)
    if get_rf_reg(t, 0, 0xB8, 1 << 15):
        set_rf_reg(t, 0, 0xB8, 0xFFFFF, 0xC4440)
        set_rf_reg(t, 0, 0xBA, 0xFFFFF, 0x6840D)
        set_rf_reg(t, 0, 0xB8, 0xFFFFF, 0x80440)
        time.sleep(0.001)


def _get_efuse_thermal_pwrtype(efuse, dm: PhydmState) -> None:
    """halrf_get_efuse_thermal_pwrtype_8822c: per path thermal reference from EFUSE 0xd0/0xd1 and
    the power track type from 0xc8[7:4] (0xf maps to 0). Shadow map reads, no wire ops.
    [SRC halrf_tssi_8822c.c:2221, called via halrf_tssi_get_efuse halrf.c:2924]"""
    m = efuse.logical_map
    dm.eeprom_thermal = (m[0xD0], m[0xD1])
    pg = m[0xC8]
    dm.power_track_type = 0x0 if ((pg >> 4) & 0xF) == 0xF else ((pg >> 4) & 0xF)


def halrf_init(t: RTL8822CUTransport, h2c: H2cState, dack: DackState, efuse,
               trim: PowerTrimState, gapk: TxGapKState, dm: PhydmState,
               channel: int = 0) -> None:
    """halrf_init: the RF-calibration block of odm_dm_init. halrf_aac_check is an empty switch
    for 8822C, its cases being 8822B/8821C only. [SRC halrf.c:2900]"""
    dac_cal(t, dack)
    rx_dck_trigger(t, h2c)
    x2k_check(t)
    config_new_kfree(t, efuse, trim)
    rfk_handshake(t, h2c, before_k=True)
    tssi_dck(t, channel)
    rfk_handshake(t, h2c, before_k=False)
    save_all_tx_gain_table(t, gapk)


def odm_dm_init(t: RTL8822CUTransport, h2c: H2cState, dack: DackState, efuse,
                trim: PowerTrimState, gapk: TxGapKState, dm: PhydmState,
                channel: int = 0) -> None:
    """odm_dm_init: the RF calibrations, then the phydm sub-inits. [SRC phydm.c:2025]"""
    halrf_init(t, h2c, dack, efuse, trim, gapk, channel)
    _get_efuse_thermal_pwrtype(efuse, dm)       # halrf_tssi_get_efuse, folded in from halrf_init
    dm_init(t, dm)
