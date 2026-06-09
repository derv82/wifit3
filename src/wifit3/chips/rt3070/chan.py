"""Channel tuning for the RT3070 (RF3020, 1T1R).

Ported from ``rt2800_config_channel`` dispatch [SRC rt2800lib.c:4161] →
``rt2800_config_channel_rf3xxx`` [SRC rt2800lib.c:2547] + the RF3020 channel
tables. Filled in at the M4 milestone.
"""
from __future__ import annotations

from .eeprom import EepromValues
from .transport import RT3070Transport


def set_channel(t: RT3070Transport, ev: EepromValues, channel: int) -> None:
    raise NotImplementedError("rt3070 set_channel (RF3020) not ported yet (M4)")
