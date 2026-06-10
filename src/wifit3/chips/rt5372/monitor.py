"""Monitor-mode entry (airmon-ng start) for the RT5372 (RT5392).

#TODO M4: port the ``rt2x00mac`` monitor entry — interface-up filter (0x97) →
initial config → monitor filter (0x93). Family-common with RT3070; the filter masks
and order are shared.
"""
from __future__ import annotations

from . import constants as C
from .constants import ChipInfo
from .eeprom import EepromValues
from .transport import RT5372Transport

# mac80211 monitor filter flag set, masked/forced by rt2x00mac_configure_filter
# [SRC rt2x00mac.c:355-401]. Finalised in M4.
MONITOR_FILTER = C.FIF_ALLMULTI | C.FIF_CONTROL | C.FIF_PSPOLL


def enable_monitor(t: RT5372Transport, chip: ChipInfo, ev: EepromValues, drv) -> None:
    raise NotImplementedError("#TODO M4: rt5372 enable_monitor")
