"""rtl8188ftv_dkms M5d: MISC02 queue/page/filter init."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def _read(self):
        return self._reads.pop(0)

    def read8(self, addr):
        return self._read()

    def read16(self, addr):
        return self._read()

    def read32(self, addr):
        return self._read()

    def write8(self, addr, value):
        self.writes.append((addr, 1, value & 0xFF))

    def write16(self, addr, value):
        self.writes.append((addr, 2, value & 0xFFFF))

    def write32(self, addr, value):
        self.writes.append((addr, 4, value & 0xFFFFFFFF))


def test_queue_reserved_page_two_ep():
    from wifit3.chips.rtl8188ftv_dkms import queues
    t = FakeT([])
    queues.init_queue_reserved_page(t, 0x05)
    assert t.writes == [(0x214, 1, 0x02), (0x200, 4, 0x80E9000C)]


def test_tx_buffer_boundary():
    from wifit3.chips.rtl8188ftv_dkms import queues
    t = FakeT([])
    queues.init_tx_buffer_boundary(t)
    assert t.writes == [(0x424, 1, 0xF8), (0x425, 1, 0xF8), (0x45D, 1, 0xF8),
                        (0x114, 1, 0xF8), (0x209, 1, 0xF8)]


def test_queue_priority_two_ep():
    from wifit3.chips.rtl8188ftv_dkms import queues
    t = FakeT([0x0000])
    queues.init_queue_priority(t, 2, 0x05)
    assert t.writes == [(0x10C, 2, 0xFAF0)]


def test_page_and_transfer_and_drvinfo():
    from wifit3.chips.rtl8188ftv_dkms import queues
    t = FakeT([])
    queues.init_page_boundary(t)
    queues.init_transfer_page_size(t)
    queues.init_driver_info_size(t)
    assert t.writes == [(0x116, 2, 0x3F7F), (0x104, 1, 0x22), (0x60F, 1, 4)]


def test_macaddr_networktype():
    from wifit3.chips.rtl8188ftv_dkms import queues
    t = FakeT([0x00000000])
    queues.init_macaddr(t, bytes.fromhex("44efbf1f9dfb"))
    assert t.writes == [(0x610 + i, 1, b) for i, b in enumerate(bytes.fromhex("44efbf1f9dfb"))]
    t = FakeT([0x00000000])
    queues.init_network_type(t)
    assert t.writes == [(0x100, 4, 0x00020000)]


def test_wmac_filter_maps():
    from wifit3.chips.rtl8188ftv_dkms import queues
    t = FakeT([])
    queues.init_wmac_setting(t)
    assert t.writes[0] == (0x608, 4, queues.RCR_ALL)
    assert t.writes[1:] == [(0x6A4, 2, 0xFFFF), (0x6A2, 2, 0x0400), (0x6A0, 2, 0xFFFF)]


def test_adaptive_edca_fallback_retry():
    from wifit3.chips.rtl8188ftv_dkms import queues
    t = FakeT([0x00000000])
    queues.init_adaptive_ctrl(t)
    assert t.writes == [(0x440, 4, 0xFFFF1), (0x428, 2, 0x1010), (0x42A, 2, 0x3030)]
    t = FakeT([])
    queues.init_edca(t)
    assert t.writes == [(0x428, 2, 0x100A), (0x63A, 2, 0x100A),
                        (0x514, 2, 0x100A), (0x516, 2, 0x100A),
                        (0x508, 4, 0x005EA42B), (0x50C, 4, 0x0000A44F),
                        (0x504, 4, 0x005EA324), (0x500, 4, 0x002FA226)]
    t = FakeT([])
    queues.init_rate_fallback(t)
    assert t.writes == [(0x430, 4, 0x0), (0x434, 4, 0x10080404),
                        (0x438, 4, 0x04030201), (0x43C, 4, 0x08070605)]
    t = FakeT([0x00])
    queues.init_retry_function(t)
    assert t.writes == [(0x420, 1, 0x80), (0x640, 1, 0x40)]
