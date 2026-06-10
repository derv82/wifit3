"""RFCSR init for the RT5372 (RT5392).

#TODO M3: port ``rt2800_init_rfcsr_5392`` [SRC rt2800lib.c:8394-8460] +
``rt2800_normal_mode_setup_5xxx`` + ``rt2800_led_open_drain_enable``. Unlike RT3070,
RT5392 runs NO RX-filter loopback calibration (no init_rx_filter / BBP55 feedback),
so this module is strictly simpler and threads no per-tune calibration into the tune.
"""
from __future__ import annotations

from .constants import ChipInfo
from .eeprom import EepromValues
from .transport import RT5372Transport


def init_rfcsr_5392(t: RT5372Transport, chip: ChipInfo, ev: EepromValues):
    raise NotImplementedError("#TODO M3: rt5372 init_rfcsr_5392")
