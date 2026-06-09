"""BBP (baseband processor) init for the RT3070 — RF30xx family path.

Ported from ``rt2800_init_bbp`` dispatch [SRC rt2800lib.c:7247] →
``rt2800_init_bbp_30xx`` [SRC rt2800lib.c:6521]. Filled in at the M2e milestone.
"""
from __future__ import annotations

from .eeprom import EepromValues
from .transport import RT3070Transport


def prepare_bbp(t: RT3070Transport) -> None:
    raise NotImplementedError("rt3070 BBP prepare not ported yet (M2e)")


def init_bbp_30xx(t: RT3070Transport, ev: EepromValues) -> None:
    raise NotImplementedError("rt3070 init_bbp_30xx not ported yet (M2e)")
