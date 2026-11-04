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
