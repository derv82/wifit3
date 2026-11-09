"""RTL8822CU firmware failure-path tests."""
from unittest.mock import MagicMock

import pytest

import wifit3.chips.rtl8822cu.firmware as firmware


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


def test_h2c_busy_mailbox_does_not_overwrite_firmware_command(monkeypatch):
    transport = MagicMock()
    transport.read8.return_value = 1
    monkeypatch.setattr(firmware.time, "sleep", lambda seconds: None)

    with pytest.raises(IOError, match="stayed owned"):
        firmware.fill_h2c_cmd(transport, firmware.H2cState(), 0x42, b"\x01")

    transport.write32.assert_not_called()


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
