"""BBP init for the RT5372 (RT5392).

#TODO M3: port ``rt2800_init_bbp_53xx`` [SRC rt2800lib.c:6858-6965] + the EEPROM-BBP
overlay loop. RT5392 differs from RT3070's init_bbp_30xx (bbp4_mac_if_ctrl first,
RT5392-only BBP88/95/98/134/135, BBP106=0x12, disable_unused_dac_adc, init_freq_calibration).
"""
from __future__ import annotations

from .constants import ChipInfo
from .eeprom import EepromValues
from .transport import RT5372Transport


def init_bbp(t: RT5372Transport, chip: ChipInfo, ev: EepromValues) -> None:
    raise NotImplementedError("#TODO M3: rt5372 init_bbp_53xx")
