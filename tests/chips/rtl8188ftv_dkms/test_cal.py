"""rtl8188ftv_dkms M5h: LC calibration."""


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


def test_lc_worker_tx_pause_branch():
    from wifit3.chips.rtl8188ftv_dkms import cal
    t = FakeT([0x04, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0])
    cal.worker(t)
    assert t.writes[0] == (0x522, 1, 0xFF)
    assert t.writes[-1] == (0x522, 1, 0x00)
    assert (0x840, 4, 0x1808000) in t.writes


def test_lc_calibrate_gates():
    from wifit3.chips.rtl8188ftv_dkms import cal
    t = FakeT([])
    cal.lc_calibrate(t, single_tone=True)
    cal.lc_calibrate(t, ability=False)
    assert t.writes == []
