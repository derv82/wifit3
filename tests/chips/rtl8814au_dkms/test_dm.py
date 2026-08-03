"""Hardware-free regression for the M3a InitHalDm (phydm) seed.

The full byte-for-byte check vs the cold-boot capture is
`scripts/chips/rtl8814au_dkms/verify_pcap.py`; this pins the IGI-derived NHM thresholds
and the masked-write masks that the differ would otherwise only catch on hardware.
"""
from wifit3.chips.rtl8814au_dkms import dm


class Rec:
    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}

    def _r(self, a, w):
        self.ops.append(("R", a))
        return self.reads.get(a, 0)

    def read8(self, a):
        return self._r(a, 1)

    def read32(self, a):
        return self._r(a, 4)

    def write8(self, a, v):
        self.ops.append(("W", a, v))

    def write32(self, a, v):
        self.ops.append(("W", a, v))


def test_nhm_thresholds_from_igi():
    # IGI default 0x20 -> th[i] = ((0x20-14)<<1)+4i = 0x24+4i -> the captured words.
    rec = Rec(reads={0x0C50: 0x20, 0x0994: 0xFFFF0100, 0x09A0: 0x000000FF,
                     0x0990: 0x27100000})
    dm._env_monitor_init(rec)
    w = {a: v for op, a, *rest in [(o[0], o[1], *(o[2:])) for o in rec.ops]
         if op == "W" for v in rest}
    assert w[0x0998] == 0x302C2824
    assert w[0x099C] == 0x403C3834
    assert w[0x0990] == 0x2710FFFF        # CLM low word = 0xffff


def test_misc11_cam_clear():
    rec = Rec()
    dm._misc11(rec)
    assert ("W", 0x0670, 0xC0000000) in rec.ops   # invalidate_cam_all
    assert ("W", 0x04CC, 0x0201FFFF) in rec.ops   # BAR mode


def test_cck_pd_level0():
    rec = Rec()
    dm._cck_pd_init(rec)
    assert rec.ops == [("W", 0x0A0A, 0x40)]


def test_rf_gain_table_opens_and_closes_page():
    rec = Rec(reads={0x0440: 0, 0x0C1C: 0})
    dm._rf_gain_table(rec)
    w32 = [(a, v) for k, a, *r in [(o[0], o[1], *(o[2:])) for o in rec.ops]
           if k == "W" for v in r]
    # Page open on all 4 LSSI write regs, then close.
    assert (0x0C90, 0x0EF80000) in w32        # RF 0xEF = 0x80000 (open), path A
    assert (0x0C90, 0x0EF00000) in w32        # RF 0xEF = 0 (close), path A
    assert (0x0C90, 0x03018000) in w32        # RF 0x30 = 0x18000 base row
