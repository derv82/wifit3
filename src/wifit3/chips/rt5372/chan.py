"""Channel tune (RF53xx, 2.4 GHz) for the RT5372 (RT5392).

#TODO M4: port ``rt2800_config_channel_rf53xx`` [SRC rt2800lib.c:3387-3483] +
``rt2800_config_channel`` tail. RF53xx packs (rf1, rf2, rf3) into RFCSR8/RFCSR9/
RFCSR11.R (vs RT3070/RF3020's RFCSR2/3/6), writes TX power to RFCSR49/50, applies
freq_offset via the MCU_FREQ_OFFSET command, and runs a per-tune VCO calibration.
"""
from __future__ import annotations

from .constants import ChipInfo
from .eeprom import EepromValues
from .transport import RT5372Transport


def set_channel(t: RT5372Transport, chip: ChipInfo, ev: EepromValues, drv, channel: int) -> None:
    raise NotImplementedError("#TODO M4: rt5372 set_channel (config_channel_rf53xx)")


def config_lna_gain(ev: EepromValues, channel: int) -> int:
    raise NotImplementedError("#TODO M4: rt5372 config_lna_gain")
