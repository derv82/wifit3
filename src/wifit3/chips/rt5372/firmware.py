"""Firmware blob load + MCU boot for the RT5372 (RT5392).

#TODO M2: port ``rt2800_load_firmware`` / ``rt2800usb_write_firmware``. The blob
+ upload offset (0 vs 4096 of the combined rt2870/rt5572 image) are confirmed on
the cold-boot wire by ``scripts/verify_pcap.py rt5372``.
"""
from __future__ import annotations

from .transport import RT5372Transport


def load_firmware_blob() -> bytes:
    raise NotImplementedError("#TODO M2: rt5372 firmware blob (assets/rt5372_fw.bin)")


def upload(t: RT5372Transport, blob: bytes) -> None:
    raise NotImplementedError("#TODO M2: rt5372 firmware upload + MCU boot signal")
