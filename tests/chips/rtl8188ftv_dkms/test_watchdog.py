"""rtl8188ftv_dkms M5h: watchdog tick (FA + DIG + adaptivity + CCK-PD)."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read8(self, addr):
        return self._reads.pop(0)

    def read16(self, addr):
        return self._reads.pop(0)

    def read32(self, addr):
        return self._reads.pop(0)

    def write8(self, addr, value):
        self.writes.append((addr, 1, value & 0xFF))

    def write16(self, addr, value):
        self.writes.append((addr, 2, value & 0xFFFF))

    def write32(self, addr, value):
        self.writes.append((addr, 4, value & 0xFFFFFFFF))


def test_false_alarm_stats():
    from wifit3.chips.rtl8188ftv_dkms import dm
    t = FakeT([0x48071D40, 0x04030740, 0x350000, 0x2F60642, 0x1702F7,
               0x2E, 0xD3A000, 0xD3B000, 0x154, 0x2005881, 0x80801B04,
               0x6C6C6CEC, 0xEC6C6CEC, 0x84030740, 0x8C030740,
               0xC8071D40, 0x84030740, 0xD3F000, 0xD3C000, 0xD3E000,
               0xD32000])
    fa = dm.false_alarm_stats(t)
    assert fa["all"] == 53 + 758 + 759 + 23 + 46 + 84 + 512
    assert fa["cck_fail"] == 0x254
    assert (0xC00, 4, 0xC8071D40) in t.writes
    assert (0xA2C, 4, 0xD3A000) in t.writes


def test_dig_step():
    from wifit3.chips.rtl8188ftv_dkms import dm
    t = FakeT([0x69553420])
    st = {"cur_ig": 0x20}
    dm.dig_step(t, st, {"all": 6000})
    assert st["cur_ig"] == 0x24
    assert t.writes == [(0xC50, 4, 0x69553424)]
    t = FakeT([])
    dm.dig_step(t, st, {"all": 3000})
    assert st["cur_ig"] == 0x24
    assert t.writes == []
    t = FakeT([0x69553424])
    dm.dig_step(t, st, {"all": 100})
    assert st["cur_ig"] == 0x22
    assert t.writes == [(0xC50, 4, 0x69553422)]


def test_adaptivity_edcca():
    from wifit3.chips.rtl8188ftv_dkms import dm
    t = FakeT([0xA03E0346])
    st = {"th_l2h_ini": 0xF5, "adaptivity_ability": False}
    dm.adaptivity_edcca(t, st)
    assert st["th_l2h_ini"] == 20
    assert t.writes == [(0xC4C, 4, 0xA03E0346)]


def test_cck_pd():
    from wifit3.chips.rtl8188ftv_dkms import dm
    t = FakeT([])
    st = {"cur_cck": 0}
    dm.cck_pd(t, st, {"cck_fail": 596})
    assert t.writes == [(0xA0A, 1, 0x40)]
    assert st["cur_cck"] == 0x40
    t = FakeT([])
    dm.cck_pd(t, st, {"cck_fail": 1500})
    assert t.writes == [(0xA0A, 1, 0x83)]
    t = FakeT([])
    dm.cck_pd(t, st, {"cck_fail": 1500})
    assert t.writes == []


def test_rxfifo_check():
    from wifit3.chips.rtl8188ftv_dkms import misc
    t = FakeT([0xA0, 0x0000])
    misc.check_rxfifo_full(t)
    assert t.writes == [(0x667, 1, 0xA0)]
