"""Process-local RTL8822CU DACK calibration cache."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from threading import Lock


from .cal import DackState
from .constants import CHIP_ID_RTL8822CU, EEPROM_ID, EEPROM_SIZE, EFUSE_SIZE

_MAX_ENTRIES = 32
_lock = Lock()
_entries: dict[tuple, DackState] = {}


def identity(efuse, chip) -> tuple | None:
    """Return a conservative factory identity, or None for an unreliable EFUSE."""
    if not efuse.autoload_ok or not efuse.map_valid or chip.chip_id != CHIP_ID_RTL8822CU:
        return None
    logical, physical = efuse.logical_map, efuse.physical_map
    if len(logical) != EEPROM_SIZE or len(physical) != EFUSE_SIZE:
        return None
    if int.from_bytes(logical[:2], "little") != EEPROM_ID:
        return None
    mac = efuse.mac_address
    if len(mac) != 6 or mac == bytes(6) or mac[0] & 3:
        return None
    return (mac, sha256(logical).digest(), sha256(physical).digest(),
            chip.chip_id, chip.cut, chip.rf_2t2r, chip.rom_version, chip.chip_ver)


def valid(state: DackState) -> bool:
    """Require calibration's explicit success signal, not just the restore enable bit."""
    return state.dack_en is True and getattr(state, "valid", False) is True


def get(key: tuple | None) -> DackState | None:
    """Return an independent copy of a live calibration, if available."""
    if key is None:
        return None
    with _lock:
        state = _entries.get(key)
        if state is None:
            return None
        if not valid(state):
            del _entries[key]
            return None
        return deepcopy(state)


def put(key: tuple | None, state: DackState) -> None:
    """Store an independent copy only when calibration explicitly succeeded."""
    if key is None or not valid(state):
        return
    with _lock:
        copied_state = deepcopy(state)
        if key not in _entries and len(_entries) >= _MAX_ENTRIES:
            del _entries[next(iter(_entries))]
        _entries[key] = copied_state


def discard(key: tuple | None) -> None:
    """Invalidate an entry after a failed restore."""
    with _lock:
        _entries.pop(key, None)
