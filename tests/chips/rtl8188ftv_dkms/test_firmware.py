"""rtl8188ftv_dkms M4: LLT + TX-report + FW download + ready + C2H handshake."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def _read(self, width):
        return self._reads.pop(0)

    def read8(self, addr):
        return self._read(1)

    def read16(self, addr):
        return self._read(2)

    def read32(self, addr):
        return self._read(4)

    def _write(self, addr, width, value):
        self.writes.append((addr, width, value))

    def write8(self, addr, value):
        self._write(addr, 1, value & 0xFF)

    def write16(self, addr, value):
        self._write(addr, 2, value & 0xFFFF)

    def write32(self, addr, value):
        self._write(addr, 4, value & 0xFFFFFFFF)

    def writeN(self, addr, data):
        self._write(addr, len(data), bytes(data))


def test_parse_header_strips_32b():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    blob = bytes(32) + bytes(100)
    blob = bytes([0xF1, 0x88, 0, 0, 4, 0, 0, 0]) + blob[8:]
    ver, sub, sig, payload = firmware.parse_header(blob)
    assert (ver, sub, sig, len(payload)) == (4, 0, 0x88F1, 100)


def test_parse_header_absent_keeps_blob():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    blob = bytes([0x00, 0x00, 0, 0, 1, 0, 0, 0]) + bytes(32)
    assert firmware.parse_header(blob)[3] == blob


def test_block_write_phases():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    t = FakeT([])
    buf = bytes(range(256)) * 2 + bytes([9])
    firmware.block_write(t, buf[:400])
    kinds = [(a, w) for a, w, _ in t.writes]
    assert kinds == [(0x1000, 196), (0x1000 + 196, 196)] + [(0x1000 + 392, 8)]
    t = FakeT([])
    firmware.block_write(t, bytes(5))
    assert [(a, w) for a, w, _ in t.writes] == [(0x1000 + i, 1) for i in range(5)]


def test_page_write_selects_page():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    t = FakeT([0x05])
    firmware.page_write(t, 3, bytes(10))
    assert t.writes[0] == (0x82, 1, (0x05 & 0xF8) | 3)


def test_download_enable_disable():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    t = FakeT([0xFC, 0x04, 0x05, 0x05])
    firmware.download_enable(t, True)
    assert t.writes == [(0x03, 1, 0xFC), (0x80, 1, 0x05), (0x82, 1, 0x05)]
    t = FakeT([0x07])
    firmware.download_enable(t, False)
    assert t.writes == [(0x80, 1, 0x06)]


def test_reset_8051_order():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    t = FakeT([0x03, 0xFC, 0x03, 0xFC])
    firmware.reset_8051(t)
    assert t.writes == [(0x1D, 1, 0x02), (0x03, 1, 0xF8),
                        (0x1D, 1, 0x03), (0x03, 1, 0xFC)]


def test_checksum_poll_first_try_and_timeout():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    assert firmware.polling_fwdl_chksum(FakeT([0x04]), 5, 50) is True
    assert firmware.polling_fwdl_chksum(FakeT([0x00]), 0, 0) is False


def test_free_to_go_ready_path():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    ready = 0x02 | 0x04 | 0x40 | 0x80
    t = FakeT([0x00, 0x03, 0xFC, 0x03, 0xFC, 0x03, 0xFC, 0x03, 0xFC, ready])
    assert firmware.fw_free_to_go(t, 10, 200) is True
    assert t.writes[0] == (0x80, 4, (0x00 | 0x02) & ~0x40)


def test_download_rejects_oversize():
    from wifit3.chips.rtl8188ftv_dkms import firmware
    try:
        firmware.download_firmware(FakeT([]), bytes(0x8001))
    except ValueError:
        pass
    else:
        raise AssertionError("oversize blob accepted")


def test_init_llt_and_tx_report():
    from wifit3.chips.rtl8188ftv_dkms import llt
    t = FakeT([0x2020, 0x2020])
    assert llt.init_llt(t) is True
    assert t.writes == [(0x224, 4, 0x2020 | 0x10000)]
    t = FakeT([0x00])
    llt.enable_tx_report(t)
    assert t.writes == [(0x4EC, 1, 0x02), (0x4ED, 1, 2), (0x4F0, 2, 0xCDF0)]


def test_c2h_handshake():
    from wifit3.chips.rtl8188ftv_dkms import c2h
    t = FakeT([])
    c2h.request_hidden_report(t)
    assert t.writes == [(0x1A0, 1, 0xFD)]
    t = FakeT([0x19] + list(range(13)))
    ident, report = c2h.collect_hidden_report(t)
    assert ident == 0x19 and report == bytes(range(13))
    assert t.writes == [(0x1A0, 1, 0x00)]
    t = FakeT([0xFD])
    ident, report = c2h.collect_hidden_report(t, timeout_ms=0, min_cnt=0)
    assert (ident, report) == (0xFD, b"")
