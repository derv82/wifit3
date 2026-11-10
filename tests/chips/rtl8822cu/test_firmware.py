"""RTL8822CU firmware failure-path tests."""
from unittest.mock import MagicMock

import pytest

import wifit3.chips.rtl8822cu.firmware as firmware
from wifit3.chips.rtl8822cu.cal import rfk_handshake
from wifit3.chips.rtl8822cu.mac import _compute_trx_alloc, arm_monitor
from wifit3.chips.rtl8822cu.transport import RTL8822CUTransport


class RecordingTransport(RTL8822CUTransport):
    """Returns a fixed value per register address and logs every access in order."""

    def __init__(self, reads: dict[int, int]):
        super().__init__(MagicMock())
        self.reads = reads
        self.log: list[tuple[str, int, int]] = []

    def _read(self, addr: int, length: int) -> bytes:
        value = self.reads.get(addr, 0) & ((1 << (8 * length)) - 1)
        self.log.append(("r", addr, value))
        return value.to_bytes(length, "little")

    def _write(self, addr: int, payload) -> None:
        self.log.append(("w", addr, int.from_bytes(bytes(payload), "little")))

    def box_writes(self) -> list[tuple[str, int, int]]:
        boxes = range(firmware.REG_HMEBOX0, firmware.REG_HMEBOX_E0 + 16)
        return [op for op in self.log if op[0] == "w" and op[1] in boxes]


def _image() -> firmware.FirmwareImage:
    return firmware.FirmwareImage(
        version=1,
        subversion=0,
        h2c_format=0,
        dmem_addr=0,
        dmem=b"dmem",
        imem_addr=0,
        imem=b"imem",
        emem_addr=None,
        emem=b"",
    )


def test_download_restores_ltecoex_when_upload_fails(monkeypatch):
    transport = MagicMock()
    restored: list[tuple[int, int]] = []
    monkeypatch.setattr(firmware, "_txfifo_wait_empty", lambda t: None)
    monkeypatch.setattr(firmware, "_ltecoex_read", lambda t, addr: 0x12345678)
    monkeypatch.setattr(firmware, "_ltecoex_write", lambda t, addr, value: restored.append((addr, value)))
    monkeypatch.setattr(firmware, "_upload_section", lambda *args, **kwargs: (_ for _ in ()).throw(IOError("write failed")))

    with pytest.raises(IOError, match="write failed"):
        firmware.download_firmware(MagicMock(), transport, 0x05, _image())

    assert restored == [(0x38, 0x12345678)]


def test_download_failure_clears_fwdl_en_and_enables_cpu_before_restoring_ltecoex():
    transport = RecordingTransport({
        firmware.REG_TXPKT_EMPTY: 0xFF,
        firmware.REG_TXPKT_EMPTY + 1: 0x06,
        firmware.REG_LTECOEX_CTRL + 3: 0x20,
        firmware.REG_LTECOEX_RDATA: 0x12345678,
        firmware.REG_MCUFW_CTRL: 0xFF,
        firmware.REG_SYS_FUNC_EN + 1: 0x00,
    })
    dev = MagicMock()
    dev.write.return_value = 0

    with pytest.raises(IOError, match="firmware bulk write short"):
        firmware.download_firmware(dev, transport, 0x05, _image())

    last_restore = max(i for i, op in enumerate(transport.log)
                       if op == ("w", firmware.REG_BCN_CTRL, 0))
    assert transport.log[last_restore + 1:] == [
        ("r", firmware.REG_MCUFW_CTRL, 0xFF),
        ("w", firmware.REG_MCUFW_CTRL, 0xFE),
        ("r", firmware.REG_SYS_FUNC_EN + 1, 0x00),
        ("w", firmware.REG_SYS_FUNC_EN + 1, 0x04),
        ("r", firmware.REG_LTECOEX_CTRL + 3, 0x20),
        ("w", firmware.REG_LTECOEX_WDATA, 0x12345678),
        ("w", firmware.REG_LTECOEX_CTRL, 0xC00F0038),
    ]


def test_h2c_busy_box_returns_false_without_writing_the_box():
    transport = RecordingTransport({firmware.REG_HMETFR: 0x01})
    h2c_state = firmware.H2cState()

    assert firmware.fill_h2c_cmd(transport, h2c_state, 0x42, b"") is False

    assert transport.box_writes() == []
    assert h2c_state.box_num == 0


def test_h2c_busy_box_reads_hmetfr_101_times_then_once_more_for_the_warning():
    transport = RecordingTransport({firmware.REG_HMETFR: 0x01})

    firmware.fill_h2c_cmd(transport, firmware.H2cState(), 0x42, b"")

    assert transport.log == [("r", firmware.REG_HMETFR, 0x01)] * 102


def test_general_info_stops_when_the_register_h2c_box_is_busy():
    h2cq_addr = _compute_trx_alloc().h2cq_addr
    transport = RecordingTransport({
        firmware.REG_HMETFR: 0x01,
        0x8000 + (h2cq_addr & 0xFFF): 0xFF01,
    })
    dev = MagicMock()
    dev.write.side_effect = lambda ep, packet, timeout: len(packet)

    with pytest.raises(IOError, match="Fail to send general info by register"):
        firmware.send_general_info(dev, transport, 0x05, firmware.H2cState(), 1, 1)

    assert transport.box_writes() == []


def test_rfk_handshake_still_polls_0x49c_when_the_h2c_box_is_busy():
    transport = RecordingTransport({firmware.REG_HMETFR: 0x01})

    rfk_handshake(transport, firmware.H2cState(), before_k=False)

    assert transport.box_writes() == []
    assert transport.log[-1] == ("r", 0x049C, 0)


def test_arm_monitor_still_programs_the_mac_when_the_h2c_box_is_busy():
    transport = RecordingTransport({firmware.REG_HMETFR: 0x01})

    arm_monitor(transport, firmware.H2cState(), bytes.fromhex("020000000001"))

    assert transport.box_writes() == []
    assert ("w", 0x0610, 0x00000002) in transport.log


def test_first_download_boundary_uses_packet_offset_and_offset_56(monkeypatch):
    transport = MagicMock()
    dev = MagicMock()
    payload = bytes(range(256)) * 15 + bytes(range(208))
    dev.write.side_effect = lambda ep, packet, timeout: len(packet)
    monkeypatch.setattr(firmware, "_poll8", lambda *args: True)

    firmware._write_fw_packet(dev, transport, 0x05, payload, beacon=False, bulk_out_size=512)

    packet = dev.write.call_args.args[1]
    assert len(packet) == 48 + 8 + len(payload)
    assert int.from_bytes(packet[0:2], "little") == len(payload)
    assert (int.from_bytes(packet[0:4], "little") >> 16) & 0xFF == 56
    assert (int.from_bytes(packet[4:8], "little") >> 24) & 0x1F == 1
    assert packet[48:56] == b"\0" * 8
    assert packet[56:] == payload


def test_second_download_boundary_uses_packet_offset_and_offset_48(monkeypatch):
    transport = MagicMock()
    dev = MagicMock()
    payload = bytes(range(256)) * 15 + bytes(range(208))
    dev.write.side_effect = lambda ep, packet, timeout: len(packet)
    monkeypatch.setattr(firmware, "_poll8", lambda *args: True)

    firmware._write_fw_packet(dev, transport, 0x05, payload, beacon=True, bulk_out_size=512)

    packet = dev.write.call_args.args[1]
    assert len(packet) == 48 + 8 + len(payload)
    assert int.from_bytes(packet[0:2], "little") == len(payload)
    assert (int.from_bytes(packet[0:4], "little") >> 16) & 0xFF == 48
    assert (int.from_bytes(packet[4:8], "little") >> 24) & 0x1F == 1
    assert packet[48:56] == b"\0" * 8
    assert packet[56:] == payload


def test_reserved_page_non_boundary_has_no_packet_offset(monkeypatch):
    transport = MagicMock()
    dev = MagicMock()
    payload = b"x" * 4095
    dev.write.side_effect = lambda ep, packet, timeout: len(packet)
    monkeypatch.setattr(firmware, "_poll8", lambda *args: True)

    firmware._write_fw_packet(dev, transport, 0x05, payload, beacon=True, bulk_out_size=512)

    packet = dev.write.call_args.args[1]
    assert len(packet) == 48 + len(payload)
    assert (int.from_bytes(packet[0:4], "little") >> 16) & 0xFF == 48
    assert (int.from_bytes(packet[4:8], "little") >> 24) & 0x1F == 0
    assert packet[48:] == payload


def test_upload_iddma_keeps_original_firmware_length(monkeypatch):
    writes: list[bytes] = []
    transfers: list[tuple[int, int, int, bool]] = []
    dev = MagicMock()
    transport = MagicMock()
    dev.write.side_effect = lambda ep, packet, timeout: writes.append(packet) or len(packet)
    transport.read32.return_value = 0
    transport.read8.return_value = 0
    monkeypatch.setattr(firmware, "_poll8", lambda *args: True)
    monkeypatch.setattr(firmware, "_iddma", lambda transport, src, dst, length, first:
                        transfers.append((src, dst, length, first)))

    firmware._upload_section(dev, transport, 0x05, 0x1000, b"x" * 4048,
                             beacon=True, bulk_out_size=512)

    assert len(writes[0]) == 4104
    assert transfers == [(firmware.OCPBASE_TXBUF_88XX + 48, 0x1000, 4048, True)]


def test_general_info_stops_on_short_first_h2c_write(monkeypatch):
    transport = MagicMock()
    dev = MagicMock()
    dev.write.return_value = 0
    monkeypatch.setattr(firmware, "_dump_h2cq", MagicMock())

    with pytest.raises(IOError, match="general-info H2C bulk write short"):
        firmware.send_general_info(dev, transport, 0x05, firmware.H2cState(), 1, 1)

    assert dev.write.call_count == 1
    firmware._dump_h2cq.assert_not_called()
