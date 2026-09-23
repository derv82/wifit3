"""rtl8188ftv_dkms M5e: beacon/burst/agg/turn-on tail."""


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


def test_beacon_params():
    from wifit3.chips.rtl8188ftv_dkms import misc
    t = FakeT([0x0] * 5)
    st: dict = {}
    misc.init_beacon_params(t, st)
    assert t.writes == [(0x550, 2, 0x1010), (0x540, 2, 0x6404),
                        (0x559, 1, 0x02), (0x510, 2, 0x660F)]
    assert st == {"bcn_ctrl": 0x0, "tx_pause": 0x0, "fw_hw_tx_q_ctrl": 0x0,
                  "reg542": 0x0, "cr1": 0x0}
    t = FakeT([0x0] * 5)
    misc.init_beacon_params(t, {}, station=False)
    assert (0x558, 1, 0x05) in t.writes


def test_burst_and_agg():
    from wifit3.chips.rtl8188ftv_dkms import misc
    t = FakeT([0x00, 0x00, 0x1C])
    misc.init_burst(t)
    assert (0x290, 1, 0x10 | 0x0E) in t.writes
    assert (0x456, 1, 0x70) in t.writes
    assert (0x458, 4, 0xFFFFFFFF) in t.writes
    t = FakeT([0x00000000])
    misc.agg_tx_update(t)
    assert t.writes == [(0x208, 4, 0x60), (0x228, 1, 0x0C)]
    t = FakeT([0x00, 0x00000000, 0x00])
    misc.agg_rx_update(t)
    assert t.writes[0] == (0x10C, 1, 0x04)
    assert t.writes[1] == (0x280, 4, 0x2005)
    assert t.writes[2] == (0x290, 1, 0x02)


def test_drop_lifetime_turnon():
    from wifit3.chips.rtl8188ftv_dkms import misc
    t = FakeT([0x00FD0000])
    misc.drop_incorrect_bulk_out(t)
    assert t.writes == [(0x20C, 4, 0x00FD0000 | 0x200)]
    t = FakeT([])
    misc.mcast2uni_lifetime(t)
    assert t.writes == [(0x4C0, 2, 0x0400), (0x4C2, 2, 0x0400)]
    t = FakeT([0x0, 0x0])
    misc.turn_on_block(t)
    assert t.writes == [(0x800, 4, 0x1000000), (0x800, 4, 0x2000000)]
