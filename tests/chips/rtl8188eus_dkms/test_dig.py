"""Hardware-free regression for the RTL8188EUS (DKMS) DIG/AGC watchdog (M12).

Locks the 11N FA-counter sum, the no-link IGI step (+2/+1/-2 by fa_th 2000/4000/5000),
and the [0x1c, 0x2a] clamp. Runtime effect is validated by the beacon-watch A/B, not a
byte-for-byte differ (DIG is read-feedback / environment-dependent).
"""
from wifit3.chips.rtl8188eus_dkms import dig


class RegTx:
    """Stateful register fake; FA-counter regs served from a dict."""
    def __init__(self, regs):
        self.regs = dict(regs)
        self.w = []

    def read32(self, a):
        return self.regs.get(a, 0)

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w.append((a, v))


def test_read_fa_cnt_sums_ofdm_and_cck():
    regs = {
        0x0CF0: (5 << 16) | 3,      # sb_search=5, fast_fsync=3
        0x0DA0: (7 << 16) | 999,    # parity=7 (low half ofdm_cca ignored)
        0x0DA4: (11 << 16) | 2,     # crc8=11, rate_illegal=2
        0x0DA8: 4,                  # mcs=4
        0x0A5C: 0x10,               # cck low byte = 0x10
        0x0A58: 0x02 << 24,         # cck high byte = 0x02 -> +0x200
    }
    cnt_all, ofdm, cck = dig._read_fa_cnt(RegTx(regs))
    assert ofdm == 3 + 5 + 7 + 2 + 11 + 4         # 32
    assert cck == 0x10 | (0x02 << 8)              # 0x210 = 528
    assert cnt_all == ofdm + cck


def test_new_igi_by_fa_steps():
    assert dig._new_igi_by_fa(0x20, 6000) == 0x22   # > fa_th[2] -> +2
    assert dig._new_igi_by_fa(0x20, 4500) == 0x21   # > fa_th[1] -> +1
    assert dig._new_igi_by_fa(0x20, 1000) == 0x1E   # < fa_th[0] -> -2
    assert dig._new_igi_by_fa(0x20, 3000) == 0x20   # in band -> hold


def test_watchdog_tick_clamps_and_writes_igi():
    # Low FA -> IGI steps down; clamps at 0x1c (more sensitive floor).
    t = RegTx({0x0C50: 0x1D})                      # IGI just above the floor
    tick = dig.watchdog_tick(t)                    # all FA regs 0 -> cnt_all=0 < fa_th[0]
    assert tick.igi == 0x1C                         # 0x1d - 2 = 0x1b -> clamped to 0x1c
    assert (0x0C50, 0x1C) in t.w
    # High FA -> IGI steps up; clamps at 0x2a (least sensitive).
    t = RegTx({0x0C50: 0x29, 0x0CF0: 6000})        # cnt_all 6000 > fa_th[2]
    tick = dig.watchdog_tick(t)
    assert tick.igi == 0x2A                         # 0x29 + 2 = 0x2b -> clamped to 0x2a


def test_watchdog_tick_no_write_when_unchanged():
    t = RegTx({0x0C50: 0x20, 0x0CF0: 3000})        # in-band FA -> IGI unchanged
    dig.watchdog_tick(t)
    assert not any(a == 0x0C50 for a, _ in t.w)    # no IGI write
