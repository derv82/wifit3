"""Cross-platform device-setup *actions*: the privileged step that makes a card openable.

This package is the privileged action layer behind the splash's Install / Restore (✕) buttons and
the bring-up engine, dispatched by :class:`wifit3.setup.base.Setup`: WinUSB bind/unbind on Windows,
kernel detach + udev on Linux, no privileged step on macOS (nothing binds the supported
RTL/MT/RT/AR cards there). The per-chipset VID:PID list each step needs comes from the driver
registry (:func:`target_for_vidpid`), never hand-maintained.
"""
from __future__ import annotations

from dataclasses import dataclass


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
    import cycle; ``key`` is the family/map key, so a prior install's setup files still resolve.
    """
    from wifit3.device.manager import driver_for, supported_ids

    smap = supported_ids()
    got = smap.get((vid, pid))
    if got is None:
        return None
    entry, key, _import = got
    driver_cls, _ = driver_for(vid, pid)
    ids = tuple(sorted(vp for vp, (_e, k, _i) in smap.items() if k == key))
    return SetupTarget(
        key=key, description=entry.description, ids=ids,
        module_hints=tuple(driver_cls.CONFLICTING_LINUX_MODULES),
        replug_after_modprobe=driver_cls.LINUX_REPLUG_AFTER_MODPROBE)
