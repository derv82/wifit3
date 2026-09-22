"""rtl8188ftv_dkms M5h: DM-init prologue + MISC11 tail + GPIO."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read8(self, addr):
        return self._reads.pop(0)

    def read32(self, addr):
        return self._reads.pop(0)

    def write8(self, addr, value):
        self.writes.append((addr, 1, value & 0xFF))

    def write16(self, addr, value):
        self.writes.append((addr, 2, value & 0xFFFF))

    def write32(self, addr, value):
        self.writes.append((addr, 4, value & 0xFFFFFFFF))


def test_dm_init_writes():
    from wifit3.chips.rtl8188ftv_dkms import dm
    t = FakeT([0x0] * 11)
    dm.dm_init(t)
    assert (0x896, 2, 0xC350) in t.writes
    assert (0x892, 2, 0xFFFF) in t.writes
    assert (0x898, 4, 0xFFFFFF50) in t.writes
    assert (0x89C, 4, 0xFFFFFFFF) in t.writes
    assert (0xE28, 4, 0xFF) in t.writes
    assert (0x890, 4, 0x100) in t.writes
    assert (0xC0C, 4, 0x80) in t.writes
    assert (0x520, 4, 0x0) in t.writes
    assert (0x524, 4, 0x800) in t.writes
    assert (0x908, 4, 0x208) in t.writes
    assert (0xE24, 4, 0x100000) in t.writes


def test_dm_init_reads_masked():
    from wifit3.chips.rtl8188ftv_dkms import dm
    t = FakeT([0xFFFFFFFF] * 11)
    cck, rx = dm.common_info_self_init(t)
    assert (cck, rx) == (0x1, 0xF)
    assert dm.dig_init_igi(t) == 0xFFFFFFFF
    assert dm.cfo_init_atc(t) == 0x1
    assert dm.thermal_swing_index(t) == 0x3FF


def test_misc11_tail_gpio():
    from wifit3.chips.rtl8188ftv_dkms import misc
    t = FakeT([])
    misc.misc11_tail(t)
    assert t.writes == [(0x423, 1, 0xFF), (0x4CC, 4, 0x0201FFFF)]
    t = FakeT([0xFF])
    misc.init_gpio_setting(t)
    assert t.writes == [(0x40, 1, 0xDF)]
