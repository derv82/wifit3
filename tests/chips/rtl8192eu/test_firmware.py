"""Offline check on firmware header parsing against the shipped blob (no hardware)."""
from importlib.resources import files
from types import SimpleNamespace

import pytest

from wifit3.chips.rtl8192eu.core import FIRMWARE_HEADER_SIZE, rtl8xxxu_load_firmware


def _fw_bytes() -> bytes:
    return files("wifit3.chips.rtl8192eu").joinpath("assets/rtl8192eu_nic.bin").read_bytes()


def test_load_firmware_accepts_shipped_blob():
    priv = SimpleNamespace(fw_data=b"", fw_size=0)
    rtl8xxxu_load_firmware(priv, _fw_bytes())
    assert priv.fw_size == len(_fw_bytes()) - FIRMWARE_HEADER_SIZE
    signature = int.from_bytes(priv.fw_data[0:2], "little")
    assert (signature & 0xfff0) == 0x92e0  # RTL8192E firmware


def test_load_firmware_rejects_bad_signature():
    blob = bytearray(_fw_bytes())
    blob[0:2] = (0x1234).to_bytes(2, "little")
    with pytest.raises(ValueError):
        rtl8xxxu_load_firmware(SimpleNamespace(fw_data=b"", fw_size=0), bytes(blob))
