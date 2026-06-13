"""Cross-platform device-setup *actions* (Tier-1).

Tier-0 (detect + classify present-but-unbound cards) lives in ``wlan/manager.py``;
this package is the privileged *action* layer the splash's Install / Restore buttons
drive — WinUSB bind/unbind on Windows, kernel detach / udev on Linux.

The VID:PID list every per-OS step needs is derived from the driver registry
(:attr:`SUPPORTED_IDS`), never hand-maintained — see :func:`ids_from_registry`.
"""
from __future__ import annotations

from wifit3.engine.protocols import DeviceID


def ids_from_registry() -> list[DeviceID]:
    """Every supported USB VID:PID, de-duplicated, flattened from the driver registry.

    Single source of truth for the setup layer: the Linux udev-rules emitter and the
    Windows WinUSB-bind target list both read this, so registering a driver with new
    ``SUPPORTED_IDS`` automatically extends device-setup with no parallel list to keep
    in sync. Several VID:PIDs are claimed by more than one driver (the Realtek 11ac
    DKMS/mainline pairs); we keep the first description seen, since the setup layer only
    needs the identifier, not the driver choice. The manager import is deferred to avoid
    pulling the chip drivers into callers that only need the protocol types.
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
