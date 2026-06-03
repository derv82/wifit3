"""Hardware-free regression for the M3c runtime DIG/AGC watchdog.

The watchdog adapts live and is not pcap-diffable; these tests pin the FA->IGI step
logic, the no-link clamp [0x1c, 0x2a], the cnt_all = OFDM-FA (+CCK-FA) read, and the
write-to-all-4-paths-only-when-changed behaviour.
"""
from wifit3.chips.rtl8814au_dkms import dig


class Rec:
    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}

    def _r(self, a):
        self.ops.append(("R", a))
        return self.reads.get(a, 0)

    def read16(self, a):
        return self._r(a)

    def read32(self, a):
        return self._r(a)

    def write32(self, a, v):
        self.ops.append(("W", a, v))

    # _set_reg_masked uses read32 + write32; no read8/write8 needed here.


def test_new_igi_by_fa_steps():
    assert dig._new_igi_by_fa(0x20, 6000) == 0x22   # > TH2 -> +2
    assert dig._new_igi_by_fa(0x20, 4500) == 0x21   # > TH1 -> +1
    assert dig._new_igi_by_fa(0x20, 3000) == 0x20   # between TH0 and TH1 -> hold
    assert dig._new_igi_by_fa(0x20, 1000) == 0x1E   # < TH0 -> -2


def test_read_fa_cnt_sums_cck_on_2g():
    # cck enabled (0x808 bit28) -> cnt_all = ofdm + cck; raw components surfaced.
    rec = Rec(reads={0x0F48: 100, 0x0A5C: 50, 0x0808: 1 << 28})
    assert dig._read_fa_cnt(rec) == (150, 100, 50)


def test_read_fa_cnt_ofdm_only_when_cck_disabled():
    rec = Rec(reads={0x0F48: 100, 0x0A5C: 50, 0x0808: 0})
    assert dig._read_fa_cnt(rec) == (100, 100, 50)


def _writes(rec):
    return {o[1]: o[2] for o in rec.ops if o[0] == "W"}


def test_watchdog_raises_igi_on_high_fa_and_writes_all_paths():
    # IGI 0x20, huge FA -> +2 -> 0x22, written to all four IGI regs.
    rec = Rec(reads={0x0C50: 0x20, 0x0F48: 9000, 0x0A5C: 0, 0x0808: 1 << 28})
    tick = dig.watchdog_tick(rec)
    assert tick.igi == 0x22 and tick.fa_cnt == 9000 and tick.ofdm_fa == 9000
    writes = _writes(rec)
    for reg in (0x0C50, 0x0E50, 0x1850, 0x1A50):
        assert writes[reg] & dig._IGI_MASK == 0x22


def test_watchdog_clamps_to_upper_bound():
    # Already at max; high FA must not push IGI past 0x2a.
    rec = Rec(reads={0x0C50: 0x2A, 0x0F48: 9000, 0x0A5C: 0, 0x0808: 1 << 28})
    assert dig.watchdog_tick(rec).igi == 0x2A


def test_watchdog_clamps_to_lower_bound():
    # At min with low FA; -2 must clamp at 0x1c, and (unchanged) skip the IGI write.
    rec = Rec(reads={0x0C50: 0x1C, 0x0F48: 0, 0x0A5C: 0, 0x0808: 1 << 28})
    assert dig.watchdog_tick(rec).igi == 0x1C
    assert not any(o[0] == "W" and o[1] in (0x0C50, 0x0E50, 0x1850, 0x1A50)
                   for o in rec.ops)


def test_watchdog_resets_fa_counters():
    rec = Rec(reads={0x0C50: 0x20, 0x0F48: 3000, 0x0A5C: 0, 0x0808: 1 << 28})
    dig.watchdog_tick(rec)
    # The 3-pulse FA/CCA reset: OFDM (0x9a4) + CCK (0xa2c) + page-F CCA (0xb58).
    written = {o[1] for o in rec.ops if o[0] == "W"}
    assert {0x09A4, 0x0A2C, 0x0B58} <= written
