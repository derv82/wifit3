"""rtl8188ftv_dkms M5a: BB-reg RMW helpers + antenna selection order."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read32(self, addr):
        return self._reads.pop(0)

    def write32(self, addr, value):
        self.writes.append((addr, value & 0xFFFFFFFF))

    def write8(self, addr, value):
        self.writes.append((addr, value & 0xFF))


def test_bit_shift():
    from wifit3.chips.rtl8188ftv_dkms import bb
    assert bb.bit_shift(0xFF) == 0
    assert bb.bit_shift(1 << 20) == 20
    assert bb.bit_shift(0xFFFF0000) == 16
    assert bb.bit_shift(0) == 32


def test_full_dword_skips_readback():
    from wifit3.chips.rtl8188ftv_dkms import bb
    t = FakeT([])
    bb.set_bb_reg(t, 0x100, 0xFFFFFFFF, 0x12345678)
    assert t.writes == [(0x100, 0x12345678)]


def test_masked_rmw_places_value():
    from wifit3.chips.rtl8188ftv_dkms import bb
    t = FakeT([0xFF00FF00])
    bb.set_bb_reg(t, 0x64, 0x00F00000, 0x0)
    assert t.writes == [(0x64, 0xFF00FF00 & ~0x00F00000)]
    t = FakeT([0x00000000])
    bb.set_bb_reg(t, 0x944, 0x3, 0x3)
    assert t.writes == [(0x944, 0x3)]


def test_antenna_selection_order():
    from wifit3.chips.rtl8188ftv_dkms import mac
    t = FakeT([0x0] * 9)
    mac.init_antenna_selection(t)
    addrs = [a for a, _ in t.writes]
    assert addrs == [0x64, 0x64, 0x40, 0x40, 0x4C, 0x4C, 0x944, 0x930, 0x38]
    assert t.writes[3] == (0x40, 0x08)
    assert t.writes[7] == (0x930, 0x77)


class FakeTB:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

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

    def writeN(self, addr, data):
        self.writes.append((addr, len(data), bytes(data)))


def test_bb_config_phase_order():
    from wifit3.chips.rtl8188ftv_dkms import bb
    t = FakeTB([0xFC14, 0x00000000])
    bb.bb_config(t, 0x1D)
    assert t.writes[0] == (0x02, 2, 0xFC14 | 0x2003)
    assert t.writes[1] == (0x1F, 1, 0x07)
    assert t.writes[2] == (0x840, 4, 0x100780)
    assert t.writes[3] == (0x02, 1, 0x17)
    addr, width, value = t.writes[-1]
    assert (addr, width) == (0x24, 4)
    assert value == ((0x1D | (0x1D << 6)) << 11) & 0x007FF800


def test_rf_serial_word():
    from wifit3.chips.rtl8188ftv_dkms import rf
    t = FakeTB([])
    rf.serial_write(t, 0, 0x1, 0x780)
    assert t.writes == [(0x840, 4, 0x100780)]
    try:
        rf.serial_write(t, 1, 0x1, 0x780)
    except ValueError:
        pass
    else:
        raise AssertionError("path B accepted")


def test_rf_serial_read_sequence():
    from wifit3.chips.rtl8188ftv_dkms import rf
    t = FakeTB([0xAAAAAAAA, 0xBBBBBBBB, 0x00, 0x00054321])
    assert rf.serial_read(t, 0, 0x5) == 0x54321
    assert t.writes[0][0] == 0x824
    assert t.writes[1] == (0x824, 4, 0xBBBBBBBB & ~0x80000000)
    assert t.writes[2] == (0x824, 4, 0xBBBBBBBB | 0x80000000)


def test_rf_first_write_clears_edge():
    from wifit3.chips.rtl8188ftv_dkms import rf
    t = FakeTB([0x390204, 0x390204, 0x00, 0x00])
    rf.serial_read(t, 0, 0xB2)
    assert t.writes[0] == (0x824, 4, 0x59390204)


def test_query_rf_reg_shifts():
    from wifit3.chips.rtl8188ftv_dkms import rf
    t = FakeTB([0x0, 0x0, 0x00, 0x000BCDE])
    assert rf.query_rf_reg(t, 0, 0xB6, 0xFF000) == 0x0B
    t = FakeTB([0x0, 0x0, 0x00, 0x000BCDE])
    assert rf.query_rf_reg(t, 0, 0xB6, 0xFFFFFFFF) == 0xBCDE


def test_config_rf_reg_plain_row():
    from wifit3.chips.rtl8188ftv_dkms import rf
    t = FakeTB([])
    rf.config_rf_reg(t, 0x18, 0x12345)
    assert t.writes == [(0x840, 4, (0x18 << 20) | 0x12345)]


def test_config_rf_reg_b6_match_first_try():
    from wifit3.chips.rtl8188ftv_dkms import rf
    data = 0x34C00
    t = FakeTB([0x0, 0x0, 0x00, data])
    rf.config_rf_reg(t, 0xB6, data)
    assert t.writes.count((0x840, 4, (0xB6 << 20) | data)) == 1


def test_config_rf_reg_b2_retries_with_lck_kick():
    from wifit3.chips.rtl8188ftv_dkms import rf
    data = 0x10000
    t = FakeTB([0x0, 0x0, 0x00, 0x00000,
                0x0, 0x0, 0x00, data])
    rf.config_rf_reg(t, 0xB2, data)
    assert (0x840, 4, (0x18 << 20) | 0x0FC07) in t.writes
    assert t.writes.count((0x840, 4, (0xB2 << 20) | data)) == 2


def test_rf_config_prologue_restore_and_tables():
    from wifit3.chips.rtl8188ftv_dkms import rf
    t = FakeTB([0x0] * 4000)
    tables = rf.rf_config(t)
    assert sorted(tables.keys()) == sorted([
        "2GA_P", "2GA_N", "2GB_P", "2GB_N",
        "2GCCKA_P", "2GCCKA_N", "2GCCKB_P", "2GCCKB_N",
        "5GA_P", "5GA_N", "5GB_P", "5GB_N"])
    assert t.writes[0][0] == 0x860
    assert t.writes[-1] == (0x870, 4, 0x0)
    try:
        rf.rf_config(FakeTB([0x0] * 4000), load_phy_file=0x20)
    except ValueError:
        pass
    else:
        raise AssertionError("para-file path accepted")
