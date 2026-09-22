"""rtl8188ftv_dkms M5h: Path-A IQK."""


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

    def write32(self, addr, value):
        self.writes.append((addr, 4, value & 0xFFFFFFFF))


def test_save_reload_round_trip():
    from wifit3.chips.rtl8188ftv_dkms import iqk
    reads = [0x100 + i for i in range(16)] + [0x1, 0x2, 0x3, 0x4] + \
        [0x200 + i for i in range(9)]
    t = FakeT(reads)
    st: dict = {}
    iqk.save_adda(t, st)
    iqk.save_mac(t, st)
    iqk.save_bb(t, st)
    assert st["adda"] == [0x100 + i for i in range(16)]
    assert st["mac"] == [0x1, 0x2, 0x3, 0x4]
    assert st["bb"] == [0x200 + i for i in range(9)]
    iqk.reload_adda(t, st)
    iqk.reload_mac(t, st)
    iqk.reload_bb(t, st)
    assert (0x85C, 4, 0x100) in t.writes
    assert (0x522, 1, 0x1) in t.writes
    assert (0x40, 4, 0x4) in t.writes
    assert (0x800, 4, 0x208) in t.writes


def test_path_adda_on_mac_calibration():
    from wifit3.chips.rtl8188ftv_dkms import iqk
    t = FakeT([])
    iqk.path_adda_on(t)
    assert t.writes == [(a, 4, 0x03C00014) for a in iqk.ADDA_REG]
    t = FakeT([0x3F0F])
    iqk.mac_setting_calibration(t)
    assert t.writes == [(0x520, 4, 0xFF3F0F)]


def test_fill_path_a_matrix():
    from wifit3.chips.rtl8188ftv_dkms import iqk
    t = FakeT([0x390000E4, 0x390000E4, 0x7F037F, 0x0, 0x0, 0x390000E6,
               0x390000E6, 0x807F037F, 0xA07F037F, 0x40000100, 0x40000101,
               0x4000D501, 0x0, 0xF0000000])
    st: dict = {}
    iqk.fill_path_a_matrix(t, st, [0x103, 0x2, 0x101, 0x3F5])
    assert t.writes == [(0xC80, 4, 0x390000E6), (0xC4C, 4, 0x807F037F),
                        (0xC94, 4, 0x0), (0xC80, 4, 0x390100E6),
                        (0xC4C, 4, 0xA07F037F), (0xC14, 4, 0x40000101),
                        (0xC14, 4, 0x4000D501), (0xCA0, 4, 0xF0000000)]
    assert st["rxiqc_ca0"] == 0xF0000000


def test_similarity():
    from wifit3.chips.rtl8188ftv_dkms import iqk
    result = [[0x103, 0x2, 0x101, 0x3F5, 0, 0, 0, 0],
              [0x103, 0x2, 0x101, 0x3F5, 0, 0, 0, 0],
              [0] * 8, [0] * 8]
    assert iqk.similarity(result, 0, 1) is True
    assert result[3] == [0] * 8
    other = [row[:] for row in result]
    other[1][0] = 0x140
    assert iqk.similarity(other, 0, 1) is False
