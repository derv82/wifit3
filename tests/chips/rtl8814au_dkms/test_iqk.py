"""Hardware-free regression for the M3d IQK one-shots (halrf_iqk_8814a).

The full byte-for-byte check vs the cold-boot capture is
``scripts/chips/rtl8814au_dkms/verify_pcap.py`` (capture-1 5G + capture-3 2.4G both reproduce the
whole IQK block). These pin the DATA-DEPENDENT control flow the spec flags as replay hotspots
so the port is provably DRIVEN BY THE WIRE READS, not a fixed transcript: the LOK poll length,
the LOK DAC fill computed from ``0x1bfc``, and the ``while (fail)`` retry bounded by the
``0x1b08`` fail bit.
"""
from wifit3.chips.rtl8814au_dkms import iqk, watchdog

_TXK_TRIG_PATH0 = 0xF8000311   # 0xf8000001 | (band_width 0 + 3)<<8 | (1<<(4+0))
_FAIL_BIT = 1 << 26            # R_0x1b08 BIT(26) — IQK fail


class FakeT:
    """Scripted transport: per-addr read queues (default 0 when drained), logs writes."""

    def __init__(self, reads=None):
        self.reads = {a: list(q) for a, q in (reads or {}).items()}
        self.writes = []

    def read32(self, a):
        q = self.reads.get(a)
        return q.pop(0) if q else 0

    def read8(self, a):
        return self.read32(a) & 0xFF

    def write32(self, a, v):
        self.writes.append((a, v & 0xFFFFFFFF))

    def write16(self, a, v):
        self.writes.append((a, v & 0xFFFF))

    def write8(self, a, v):
        self.writes.append((a, v & 0xFF))


def _lok_rf8_writes(t):
    """The path-A RF_0x8 fill words the LOK emits (RF writes ride the path-A LSSI reg 0xc90)."""
    return [v for a, v in t.writes if a == 0x0C90]


def test_lok_poll_length_and_dac_fill_come_from_the_wire():
    """path-A LOK polls ``R_0x1b00`` bit0 until the wire says done (not-ready, not-ready, ready
    -> exactly 3 reads), then fills RF_0x8 from the ``0x1bfc`` DAC read-back. Two runs with
    different read-backs must yield different RF_0x8 writes — the fill is COMPUTED, not constant."""
    def run(bfc_hi, bfc_lo):
        st = watchdog.WatchdogState(eeprom_thermal=35)
        t = FakeT({0x1B00: [1, 1, 0],           # path0: not-ready, not-ready, ready
                   0x1BFC: [bfc_hi, bfc_lo]})    # path0 DAC read-back (temp2 hi, temp1 lo)
        iqk._lok_one_shot(t, st)
        return t, st

    t_hi, st_hi = run(0x003E0000, 0x0000003E)    # both DAC fields = 0x1f
    t_lo, _ = run(0x0, 0x0)                       # both DAC fields = 0
    assert t_hi.reads[0x1B00] == []              # all 3 path0 polls consumed (loop ran to the wire)
    assert st_hi.iqk_lok_fail[0] is False        # bit0==0 read => LOK success branch
    assert len(_lok_rf8_writes(t_hi)) == 2       # RF_0x8 filled twice (0x07c00 + 0xf8000 fields)
    assert _lok_rf8_writes(t_hi) != _lok_rf8_writes(t_lo)   # fill tracks the 0x1bfc read-back


def test_iqk_retry_count_is_driven_by_the_wire_fail_bit():
    """path-A TXK: the wire returns fail on the first one-shot, pass on the second -> the port
    re-triggers exactly once (2 total), then records success. The retry is the wire's fail bit,
    not a fixed op-count."""
    st = watchdog.WatchdogState(eeprom_thermal=35)
    st.current_band_type = iqk.ODM_BAND_5G       # skip the 2.4G RXK tone block (RF ops)
    t = FakeT({0x1B08: [_FAIL_BIT, 0]})          # path0-TXK: fail then pass; rest default pass
    iqk._iqk_one_shot(t, st)
    triggers = [v for a, v in t.writes if a == 0x1B00 and v == _TXK_TRIG_PATH0]
    assert len(triggers) == 2                    # one retry, driven by the fail read
    assert st.iqk_fail[iqk.TX_IQK][0] is False   # second attempt passed


def test_iqk_retry_is_bounded_when_the_wire_keeps_failing():
    """If the wire never clears the fail bit, ``cal_retry > 3`` stops the loop at 4 attempts
    (bounded, not infinite) and the path is marked failed."""
    st = watchdog.WatchdogState(eeprom_thermal=35)
    st.current_band_type = iqk.ODM_BAND_5G
    t = FakeT({0x1B08: [_FAIL_BIT] * 4})         # path0-TXK: fail x4; rest default pass
    iqk._iqk_one_shot(t, st)
    triggers = [v for a, v in t.writes if a == 0x1B00 and v == _TXK_TRIG_PATH0]
    assert len(triggers) == 4                    # bounded at cal_retry > 3
    assert st.iqk_fail[iqk.TX_IQK][0] is True    # never cleared -> failed
