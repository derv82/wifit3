"""rtl8188ftv_dkms M2 wire: EFUSE cell-select, power-switch, bank, one-byte read.

Scripted-read fakes; the byte-for-byte proof is the capture-2 walk.
"""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def _read(self, addr, width):
        return self._reads.pop(0)

    def read8(self, addr):
        return self._read(addr, 1)

    def read16(self, addr):
        return self._read(addr, 2)

    def read32(self, addr):
        return self._read(addr, 4)

    def _write(self, addr, width, value):
        self.writes.append((addr, width, value))

    def write8(self, addr, value):
        self._write(addr, 1, value & 0xFF)

    def write16(self, addr, value):
        self._write(addr, 2, value & 0xFFFF)

    def write32(self, addr, value):
        self._write(addr, 4, value & 0xFFFFFFFF)


def test_get_eeprom_size_boot_source():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    assert efuse.get_eeprom_size(FakeT([0x20])) == 4    # capture-2 op1
    assert efuse.get_eeprom_size(FakeT([0x30])) == 6    # BOOT_FROM_EEPROM set


def test_cell_select_clears_sel_bits():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    t = FakeT([0x3C000000])
    efuse.cell_select(t)
    assert t.writes == [(0x34, 4, 0x3C000000)]   # no SEL bits set: unchanged


def test_power_switch_skips_set_bits():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    t = FakeT([0x1000, 0x0022])   # FEN_ELDR set, LOADER_CLK+ANA8M set
    efuse.power_switch(t, False, True)
    assert t.writes == [(0xCF, 1, 0x69)]


def test_power_switch_sets_clear_bits():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    t = FakeT([0x0000, 0x0000])
    efuse.power_switch(t, False, True)
    assert t.writes == [(0xCF, 1, 0x69), (0x02, 2, 0x1000), (0x08, 2, 0x22)]


def test_power_switch_off_ldo():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    t = FakeT([0x90])
    efuse.power_switch(t, True, False)
    assert t.writes == [(0xCF, 1, 0x00), (0x37, 1, 0x10)]


def test_switch_to_bank_sel():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    t = FakeT([0x3C000213])
    assert efuse.switch_to_bank(t, 0) is True
    assert t.writes == [(0x34, 4, 0x3C000013)]
    assert efuse.switch_to_bank(FakeT([0]), 9) is False


def test_one_byte_read_smic_handshake():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    # R16(0x34) preamble, W31 addr, R32 keep-high, W32, R33 clear-31,
    # R33 poll set, R30 data
    t = FakeT([0x1234, 0xFC, 0x80, 0x00, 0x80, 0xAB])
    ok, value = efuse.one_byte_read(t, 0x10, True)
    assert (ok, value) == (True, 0xAB)
    assert t.writes[0] == (0x34, 2, 0x1234 & ~0x800)
    assert t.writes[1] == (0x31, 1, 0x10)
    assert t.writes[3] == (0x33, 1, 0x00)


def test_one_byte_read_no_smic_preamble():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    t = FakeT([0xFC, 0x80, 0x80, 0xCD])
    ok, value = efuse.one_byte_read(t, 0xEE, False)
    assert (ok, value) == (True, 0xCD)
    assert all(a != 0x34 for a, _, _ in t.writes)


def test_section_map_empty_physical():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    t = FakeT([0x3C000000, 0x1234, 0xFC, 0x80, 0x80, 0xFF])
    table = efuse.read_section_map(t, True, 0, 16)
    assert table == b"\xff" * 16   # header 0xFF ends the walk immediately


def test_word_cnts():
    from wifit3.chips.rtl8188ftv_dkms import efuse
    assert efuse.word_cnts(0x0) == 4
    assert efuse.word_cnts(0xF) == 0
    assert efuse.word_cnts(0x5) == 2
