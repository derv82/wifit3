"""RFCSR init for the RT3070 — RF30xx family path + RX-filter calibration.

Ported from ``rt2800_init_rfcsr`` dispatch [SRC rt2800lib.c:10740] →
``rt2800_init_rfcsr_30xx`` [SRC rt2800lib.c:7618], including
``rt2800_rf_init_calibration``, ``rt2800_rx_filter_calibration`` and
``rt2800_normal_mode_setup_3xxx``. Filled in at the M2f milestone.
"""
from __future__ import annotations

from .eeprom import EepromValues
from .transport import RT3070Transport


def init_rfcsr_30xx(t: RT3070Transport, ev: EepromValues) -> None:
    raise NotImplementedError("rt3070 init_rfcsr_30xx not ported yet (M2f)")
