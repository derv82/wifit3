"""Cross-platform device-setup *actions* (Tier-1).

Tier-0 (detect + classify unbound cards) lives in ``wlan/manager.py``; this package is the
privileged action layer the splash's Install / Restore buttons drive — WinUSB bind/unbind on
Windows, kernel detach / udev on Linux. The VID:PID list each step needs comes from the
driver registry (:func:`ids_from_registry`), never hand-maintained.
"""
from __future__ import annotations

from wifit3.engine.protocols import DeviceID


def ids_from_registry() -> list[DeviceID]:
    """Every supported USB VID:PID, de-duplicated, flattened from the driver registry.

    Single source of truth for the setup layer (Linux udev emitter + Windows bind list), so a
    new driver's ``SUPPORTED_IDS`` extends device-setup automatically. A VID:PID claimed by
    several drivers keeps the first description seen. Import is deferred so callers that only
    need the protocol types don't pull in the chip drivers.
    """
    from wifit3.wlan.manager import _import_driver_classes

    seen: set[tuple[int, int]] = set()
    out: list[DeviceID] = []
    for driver_cls in _import_driver_classes().values():
        for entry in driver_cls.SUPPORTED_IDS:
            key = (entry.vid, entry.pid)
            if key not in seen:
                seen.add(key)
                out.append(entry)
    return out
