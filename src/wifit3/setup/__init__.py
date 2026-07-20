"""Cross-platform device-setup *actions* (Tier-1).

Tier-0 (detect + classify unbound cards) lives in ``wlan/manager.py``; this package is the
privileged action layer the splash's Install / Restore buttons drive: WinUSB bind/unbind on
Windows, kernel detach / udev on Linux. The VID:PID list each step needs comes from the
driver registry (:func:`ids_from_registry`), never hand-maintained.
"""
from __future__ import annotations

from dataclasses import dataclass

from wifit3.chips.driver import DeviceID


@dataclass(frozen=True)
class SetupTarget:
    """One opt-in unit for the Zadig-style "hand wifit3 this chipset" flow.

    The unit is a *driver* (= chipset), not a single physical device: the modprobe blacklist
    that keeps the kernel off the card is module-granular, so handing over one card hands over
    every card that driver claims. ``key`` names the per-chipset rule/blacklist file pair.
    """
    key: str                              # registry/chipset name, e.g. "ar9271", names the files
    description: str                      # human label of the card the user selected
    ids: tuple[tuple[int, int], ...]      # every VID:PID this driver claims
    module_hints: tuple[str, ...]         # fallback names (driver's CONFLICTING_LINUX_MODULES)
    replug_after_modprobe: bool = False   # warm card can't recover → make the user replug (cold)


def target_for_vidpid(vid: int, pid: int) -> SetupTarget | None:
    """The :class:`SetupTarget` for the driver that claims ``vid:pid`` (or ``None`` if none does).

    Live module discovery (sysfs / ``modprobe -R``) is authoritative at install time; the
    driver's optional ``CONFLICTING_LINUX_MODULES`` rides along as a fallback hint for the
    degenerate "device not plugged in" path. Import is deferred to sidestep the chip-driver
    import cycle.
    """
    from wifit3.wlan.manager import _import_driver_classes

    for key, driver_cls in _import_driver_classes().items():
        for entry in driver_cls.SUPPORTED_IDS:
            if entry.vid == vid and entry.pid == pid:
                ids = tuple(sorted({(e.vid, e.pid) for e in driver_cls.SUPPORTED_IDS}))
                hints = tuple(getattr(driver_cls, "CONFLICTING_LINUX_MODULES", ()) or ())
                return SetupTarget(
                    key=key, description=entry.description, ids=ids, module_hints=hints,
                    replug_after_modprobe=bool(
                        getattr(driver_cls, "LINUX_REPLUG_AFTER_MODPROBE", True)))
    return None


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
