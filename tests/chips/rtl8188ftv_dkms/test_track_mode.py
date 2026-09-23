"""rtl8188ftv_dkms M5h: thermal trigger + monitor entry."""


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


def test_thermal_trigger():
    from wifit3.chips.rtl8188ftv_dkms import track
    t = FakeT([0x0, 0x0, 0x0, 0x0])
    track.thermal_trigger(t)
    assert t.writes[-1] == (0x840, 4, 0x4230000)


def test_enter_monitor():
    from wifit3.chips.rtl8188ftv_dkms import mode
    t = FakeT([0x02])
    mode.enter_monitor(t)
    assert t.writes == [(0x102, 1, 0x0), (0x608, 4, 0x9000382F),
                        (0x6A4, 2, 0xFFFF)]


def test_set_station_opmode():
    from wifit3.chips.rtl8188ftv_dkms import mode
    t = FakeT([0x10, 0x02])
    st = {"fw_hw_tx_q_ctrl": 0x71, "reg542": 0x00}
    mode.set_station_opmode(t, st)
    assert t.writes == [(0x550, 1, 0x10), (0x102, 1, 0x02),
                        (0x422, 1, 0x31), (0x541, 1, 0x64),
                        (0x542, 1, 0x00), (0x550, 1, 0x19)]
    assert st == {"fw_hw_tx_q_ctrl": 0x31, "reg542": 0x00}
