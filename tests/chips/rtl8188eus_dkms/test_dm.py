"""Hardware-free regression for the RTL8188EUS (DKMS) phydm InitHalDm seed (M7).

Locks the deterministic seed (GPIO, IGI-derived NHM thresholds, EDCCA encoding, LNA
gain rows) and that the EDCCA pwdb search terminates on a clear band. The full
byte-for-byte replay (incl. capture-2's data-dependent 279-op loop) lives in
``scripts/rtl8188eus_dkms/verify_pcap.py``.
"""
from wifit3.chips.rtl8188eus_dkms import dm


class RegTx:
    """Stateful register fake; 0xdf4 (BB debug value) served by a supplied function."""
    def __init__(self, init=None, dbg=lambda sel: 0):
        self.regs = dict(init or {})
        self.w32, self.w8 = [], []
        self._dbg = dbg
        self._sel = 0

    def read8(self, a):
        return self.regs.get(a, 0) & 0xFF

    def read32(self, a):
        if a == 0x0DF4:
            return self._dbg(self._sel)
        return self.regs.get(a, 0)

    def write8(self, a, v):
        self.regs[a] = v & 0xFF
        self.w8.append((a, v & 0xFF))

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        if a == 0x0908:
            self._sel = v
        self.regs[a] = v
        self.w32.append((a, v))


def test_init_gpio_clears_enbt():
    t = RegTx({0x0040: 0x20})
    dm._init_gpio(t)
    assert t.w8 == [(0x0040, 0x00)]          # GPIOSEL_ENBT (BIT5) cleared


def test_env_monitor_nhm_thresholds_igi_derived():
    # IGI=0x20 -> th0=(0x20-14)<<1=0x24, th[i]=th0+4i. Matches cap1 op 1643-1650.
    t = RegTx({0x0890: 0x800})
    dm._env_monitor_init(t, 0x20)
    r = t.regs
    assert r[0x0898] == 0x302C2824           # th3,th2,th1,th0
    assert r[0x089C] == 0x403C3834           # th7,th6,th5,th4
    assert r[0x0E28] == 0x44                 # th8
    assert r[0x0890] == 0x4C480900           # th10,th9 in [31:16]; restart BIT8 set
    assert r[0x0894] == 0xFFFF               # CLM period 65535


def test_set_edcca_threshold_encoding():
    t = RegTx({0x0C4C: 0x007F037F})
    dm._set_edcca_threshold(t, -30, -23)     # initial (th_h2l=-30, th_l2h=-23)
    assert t.regs[0x0C4C] == 0x00E203E9      # byte0=L2H(0xe9), byte2=H2L(0xe2)


def test_set_lna_gain_rows():
    t = RegTx()
    dm._set_lna(t, enable=False)
    # 0x32 = 0x37f82 (LNA disabled). RF writes ride 0x840 (path-A LSSI).
    assert (0x0840, (0x32 << 20) | 0x37F82) in t.w32
    t = RegTx()
    dm._set_lna(t, enable=True)
    assert (0x0840, (0x32 << 20) | 0x77F82) in t.w32   # back to normal


def test_search_terminates_on_clear_band():
    # dbg port always reads 0 -> tx_edcca1=0 -> one outer iteration, no threshold step.
    t = RegTx({0x0C4C: 0x007F037F})
    dm._search_pwdb_lower_bound(t)
    c4c = [v for a, v in t.w32 if a == 0x0C4C]
    # initial set + final 0x7f/0x7f restore only (no in-loop increment).
    assert c4c == [0x00E203E9, 0x007F037F]
    # LNA disabled then re-enabled.
    assert (0x0840, (0x32 << 20) | 0x37F82) in t.w32
    assert (0x0840, (0x32 << 20) | 0x77F82) in t.w32


def test_search_steps_threshold_while_edcca_busy():
    # Return BIT30 (EDCCA asserted) for the first 2 outer iterations, then clear.
    state = {"outer": -1}

    def dbg(sel):
        if sel == 0x0:                       # CCA-status read starts a new outer iter
            state["outer"] += 1
            return 0
        return (1 << 30) if state["outer"] < 2 else 0
    t = RegTx({0x0C4C: 0x007F037F}, dbg=dbg)
    dm._search_pwdb_lower_bound(t)
    c4c = [v for a, v in t.w32 if a == 0x0C4C]
    # initial(-23/-30) + 2 increments(-22/-29, -21/-28) + final 0x7f restore.
    assert c4c == [0x00E203E9, 0x00E303EA, 0x00E403EB, 0x007F037F]
