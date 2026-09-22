"""rtl8188ftv_dkms M3: power-sequence parser + power-on composition.

Hardware-free: a fake transport serves scripted reads and records writes.
asyncio_mode=auto runs the async tests undecorated (none here yet).
"""


class FakeTransport:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read8(self, addr):
        return self._reads.pop(0)

    def read16(self, addr):
        return self._reads.pop(0)

    def write8(self, addr, value):
        self.writes.append((addr, 1, value & 0xFF))

    def write16(self, addr, value):
        self.writes.append((addr, 2, value & 0xFFFF))


M3_READS = [0x08, 0x5C, 0x00, 0x02, 0x00, 0x00, 0x00] + [0x01] * 15 + [0x00, 0x34]


def test_cr_composition_matches_vendor_dma_enable():
    from wifit3.chips.rtl8188ftv_dkms.power import CR_INIT_POWER_ON
    assert CR_INIT_POWER_ON == 0x063F   # pcap op33 W16 0x100=1599


def test_parser_skips_sdio_rows_on_usb():
    from wifit3.chips.rtl8188ftv_dkms import pwrseq
    t = FakeTransport(reads=list(M3_READS))
    assert pwrseq.parse(t) is True
    addrs = [a for a, _, _ in t.writes]
    assert 0x86 not in addrs   # SDIO rows emit no wire ops on USB
    assert t.writes[0] == (0x05, 1, 0x00)
    assert t.writes[1] == (0xC4, 1, 0x4C)
    assert t.writes[-1] == (0x27, 1, 0x35)


def test_parser_poll_timeout_fails():
    from wifit3.chips.rtl8188ftv_dkms import pwrseq
    flow = [(0x06, 0xFF, 0x0F, 0x0F, 0x00, 2, 0x02, 0x02),
            (0xFFFF, 0xFF, 0x0F, 0x0F, 0, 4, 0, 0)]
    t = FakeTransport(reads=[0x00] * 6000)
    assert pwrseq.parse(t, flow) is False


def test_parser_unknown_cmd_is_noop_then_ends():
    from wifit3.chips.rtl8188ftv_dkms import pwrseq
    flow = [(0x05, 0xFF, 0x0F, 0x0F, 0x00, 0, 0xFF, 0),
            (0xFFFF, 0xFF, 0x0F, 0x0F, 0, 4, 0, 0)]
    t = FakeTransport(reads=[])
    assert pwrseq.parse(t, flow) is True
    assert t.writes == []   # PWR_CMD_READ is a no-op in the source


def test_power_on_cr_dance_order():
    from wifit3.chips.rtl8188ftv_dkms import power
    t = FakeTransport(reads=list(M3_READS) + [0x0000])
    assert power.power_on(t) is True
    assert t.writes[-2:] == [(0x100, 1, 0x00), (0x100, 2, 0x063F)]


def test_self_reset_skipped_for_non_81xxc_fw():
    from wifit3.chips.rtl8188ftv_dkms import power
    t = FakeTransport(reads=[])
    power.firmware_self_reset(t, 0x88F1, 4, 0)
    assert t.writes == []


def test_self_reset_no_wait_when_bit_clear():
    from wifit3.chips.rtl8188ftv_dkms import power
    t = FakeTransport(reads=[0xF8])
    power.firmware_self_reset(t, 0x88C0, 0x20, 0x00)
    assert t.writes == [(0x1CF, 1, 0x20)]


def test_card_disable_probe_path():
    from wifit3.chips.rtl8188ftv_dkms import power
    reads = [0x00, 0xC6, 0x01, 0x00, 0x00, 0x00, 0x00, 0x14, 0x14,
             0x00, 0x06, 0x00, 0xFC, 0x00, 0x02, 0x35, 0x00, 0x00, 0x00, 0x4C]
    t = FakeTransport(reads=reads)
    assert power.card_disable(t, False) is True
    assert t.writes[:3] == [(0x4EC, 1, 0x00), (0x100, 1, 0x00), (0x139, 1, 0x01)]
    assert (0x522, 1, 0xFF) in t.writes
    assert t.writes[-5:] == [(0x4E, 1, 0x02), (0x27, 1, 0x34), (0x05, 1, 0x02),
                             (0x05, 1, 0x08), (0xC4, 1, 0x5C)]
    assert not any(a == 0x86 for a, _, _ in t.writes)
