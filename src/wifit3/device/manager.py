"""The device layer: the VID:PID map (read without importing any driver), the stateless bus-scan
queries over it, and ``DeviceManager`` (bring-up + setup + interface construction).

The map is built by a ``pkgutil`` walk over ``chips/*`` that imports only each package's light
``__init__`` (``SUPPORTED_IDS`` + ``import_driver``), never the driver itself. ``import_driver`` runs
only on a match (``driver_for`` / bring-up).
"""
from __future__ import annotations

import errno
import functools
import importlib
import logging
import os
import pkgutil
import sys
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable, List, NamedTuple, NoReturn, Optional, Tuple

import libusb_package
import usb.core

from wifit3.errors import BringUpError, BringUpPermissionsError, WifiteFatalError, is_device_gone
from wifit3.models.device_id import DeviceID
from wifit3.setup.base import Setup, SetupResult
from wifit3.wlan.array import WlanArray
from wifit3.wlan.interface import WlanInterface

if TYPE_CHECKING:
    from wifit3.chips.driver import Driver

logger = logging.getLogger(__name__)


class Status(Enum):
    READY = auto()
    CANCELLED = auto()
    FAILED = auto()


@dataclass
class BringupResult:
    """The terminal outcome of a bring-up. The live interface lives in the WlanArray, not here."""
    status: Status
    message: str = ""

    @classmethod
    def ready(cls) -> "BringupResult":
        return cls(Status.READY)

    @classmethod
    def cancelled(cls, message: str = "") -> "BringupResult":
        return cls(Status.CANCELLED, message)

    @classmethod
    def failed(cls, message: str) -> "BringupResult":
        return cls(Status.FAILED, message)


VidPid = tuple[int, int]


class Claim(NamedTuple):
    """``(entry, key, import_driver)`` for one supported vid:pid."""
    entry: DeviceID
    key: str
    import_driver: Callable[[], type["Driver"]]


@dataclass(frozen=True)
class DkmsFamily:
    """Env var picks the DKMS ``default`` or the ``mainline`` package."""
    key: str
    default: str
    mainline: Optional[str] = None
    env: Optional[str] = None


@dataclass(frozen=True)
class VidPidFamily:
    """Distinct chips on one vid:pid; ``resolve(dev)`` routes by live device."""
    default: str
    resolve: Callable[[usb.core.Device], str]


def _resolve_2357_0137(dev: usb.core.Device) -> str:
    try:
        cfg = dev.get_active_configuration()
    except Exception:
        return "mt76x2u"  # default
    for intf in cfg:
        if any(ep.bEndpointAddress == 0x85 for ep in intf):
            return "mt76x2u"  # bulk-IN 0x85 = Mediatek
    return "rtl8822cu"


# One chip, two driver packages; an env var picks the winner. Keyed by every dir a family owns.
_DKMS_BY_DIR: dict[str, DkmsFamily] = {
    d: fam
    for fam in (
        DkmsFamily(key="ar9271",     default="ar9271_v2"),
        DkmsFamily(key="rtl8821cu",  default="rtl8821cu_dkms"),
        DkmsFamily(key="rtl8188eus", default="rtl8188eus_dkms", mainline="rtl8188eus",   env="WIFIT3_RTL8188"),
        DkmsFamily(key="rtl8812au",  default="rtl8812au_dkms",  mainline="rtl8812au",    env="WIFIT3_RTL8812"),
        DkmsFamily(key="rtl8821au",  default="rtl8821au_dkms",  mainline="rtl8821au",    env="WIFIT3_RTL8821"),
        DkmsFamily(key="rtl8814au",  default="rtl8814au_dkms",  mainline="rtw88_8814au", env="WIFIT3_RTL8814"),
        DkmsFamily(key="rtl8822bu",  default="rtl8822bu_dkms",  mainline="rtl8822bu",    env="WIFIT3_RTL8822"),
    )
    for d in (fam.default, fam.mainline) if d
}

# Distinct chips behind one vid:pid, keyed by (vid, pid).
_VIDPID_FAMILIES: dict[VidPid, VidPidFamily] = {
    (0x2357, 0x0137): VidPidFamily(default="mt76x2u", resolve=_resolve_2357_0137),
}


def _winner_dir(fam: DkmsFamily) -> str:
    """Driver dir: env var opts into ``mainline``, else ``default``."""
    if fam.mainline and os.environ.get(fam.env or "", "").strip().lower() == "mainline":
        return fam.mainline
    return fam.default


@functools.cache
def supported_ids() -> dict[VidPid, Claim]:
    """``{(vid, pid): Claim}`` for every supported vid:pid."""
    from wifit3 import chips as chips_pkg

    winners = {fam.key: _winner_dir(fam) for fam in set(_DKMS_BY_DIR.values())}
    mapping: dict[VidPid, Claim] = {}
    for mod_info in pkgutil.iter_modules(chips_pkg.__path__, chips_pkg.__name__ + "."):
        if not mod_info.ispkg:
            continue
        dir_name = mod_info.name.rsplit(".", 1)[-1]
        fam = _DKMS_BY_DIR.get(dir_name)
        if fam is not None and dir_name != winners[fam.key]:
            continue  # a DKMS family's losing package
        key = fam.key if fam is not None else dir_name
        mod = importlib.import_module(mod_info.name)
        for entry in getattr(mod, "SUPPORTED_IDS", None) or ():
            slot = (entry.vid, entry.pid)
            shared = _VIDPID_FAMILIES.get(slot)
            if shared is not None and dir_name != shared.default:
                continue  # a shared vid:pid's non-default chip
            assert slot not in mapping, (
                f"VID:PID {slot[0]:#06x}:{slot[1]:#06x} claimed by two packages; "
                f"add a DkmsFamily or VidPidFamily to disambiguate")
            mapping[slot] = Claim(entry, key, mod.import_driver)
    return mapping


def driver_for(vid: int, pid: int) -> Optional[Tuple[type["Driver"], str]]:
    """``(driver class, setup key)`` claiming ``vid:pid``, or None."""
    claim = supported_ids().get((vid, pid))
    if claim is None:
        return None
    return claim.import_driver(), claim.key


def _package_claim(dir_name: str, slot: VidPid) -> Optional[Claim]:
    """The ``Claim`` a package dir contributes for ``slot``, or None."""
    try:
        mod = importlib.import_module(f"wifit3.chips.{dir_name}")
    except ImportError:
        return None
    for entry in getattr(mod, "SUPPORTED_IDS", ()) or ():
        if (entry.vid, entry.pid) == slot:
            return Claim(entry, dir_name, mod.import_driver)
    return None


def resolve_driver(vid: int, pid: int, dev: usb.core.Device) -> Optional[Tuple[type["Driver"], str]]:
    """``(driver class, setup key)`` for the live ``dev``."""
    shared = _VIDPID_FAMILIES.get((vid, pid))
    if shared is not None:
        claim = _package_claim(shared.resolve(dev), (vid, pid))
        if claim is not None:
            return claim.import_driver(), claim.key
    return driver_for(vid, pid)


def _raise_usblib_fatal(cause: Exception) -> NoReturn:
    # pyusb's NoBackendError is opaque.
    if sys.platform.startswith("linux"):
        message = (
            "The bundled libusb could not be loaded. A system dependency is missing.\n\n"
            "Install it for your architecture and replug the card:\n"
            "  Debian / Ubuntu / Kali:   sudo apt install libudev1\n"
            "  Fedora / RHEL:            sudo dnf install systemd-libs\n"
            "  Arch:                     sudo pacman -S systemd-libs")
    elif sys.platform == "darwin":
        message = (
            "The bundled libusb failed to load. Reinstall wifit3: the install is likely corrupt. "
            "If it persists, check that the Mac's architecture (Apple Silicon / Intel) matches "
            "the installed package.")
    else:
        message = (
            "The bundled libusb failed to initialize. Reinstall wifit3: the install is likely "
            "corrupt or being blocked by security software.")
    raise WifiteFatalError("USB backend unavailable", message) from cause


def _bus_devices(backend) -> List[usb.core.Device]:
    # The raw bus enumeration, turning pyusb's opaque NoBackendError into a WifiteFatalError.
    try:
        return list(usb.core.find(find_all=True, backend=backend))
    except usb.core.NoBackendError as exc:
        _raise_usblib_fatal(exc)


def devices() -> List[DeviceID]:
    """Every supported device present on the USB bus right now."""
    backend = libusb_package.get_libusb1_backend()
    smap = supported_ids()
    out: List[DeviceID] = []
    for dev in _bus_devices(backend):
        slot = (dev.idVendor, dev.idProduct)
        claim = smap.get(slot)
        if claim is None:
            continue
        entry = claim.entry
        shared = _VIDPID_FAMILIES.get(slot)
        if shared is not None:
            resolved = _package_claim(shared.resolve(dev), slot)
            if resolved is not None:
                entry = resolved.entry
        out.append(replace(entry, bus=dev.bus, address=dev.address))
    return out


def device(device_id: DeviceID) -> Optional[DeviceID]:
    """The live device with ``device_id``'s VID:PID, tagged with its current (bus, address), or None."""
    for d in devices():
        if (d.vid, d.pid) == (device_id.vid, device_id.pid):
            return d
    return None


def linux_node_path(device_id: DeviceID) -> Optional[str]:
    """The usbfs node ``/dev/bus/usb/BBB/DDD`` of the present card matching ``device_id`` (Linux),
    or None if it isn't on the bus. Honors the (bus, address) instance when set, so the right node is
    returned with identical siblings plugged in. The path the udev rule / chgrp acts on."""
    backend = libusb_package.get_libusb1_backend()
    want_instance = device_id.bus is not None and device_id.address is not None
    for dev in _bus_devices(backend):
        if (dev.idVendor, dev.idProduct) != (device_id.vid, device_id.pid):
            continue
        if want_instance and (dev.bus, dev.address) != (device_id.bus, device_id.address):
            continue
        return f"/dev/bus/usb/{dev.bus:03d}/{dev.address:03d}"
    return None


def _live_dev(device_id: DeviceID) -> Optional[usb.core.Device]:
    backend = libusb_package.get_libusb1_backend()
    if device_id.bus is not None and device_id.address is not None:
        return usb.core.find(idVendor=device_id.vid, idProduct=device_id.pid,
                             bus=device_id.bus, address=device_id.address, backend=backend)
    return usb.core.find(idVendor=device_id.vid, idProduct=device_id.pid, backend=backend)


def wlan_iface(device_id: DeviceID, name: str = "wlan0") -> Optional[WlanInterface]:
    """The (unconnected) WlanInterface for the present card matching ``device_id``, or None if it
    isn't on the bus or its driver can't be constructed. With an instance address (bus + address, as
    ``devices()`` tags them) the exact physical card is opened via a targeted ``usb.core.find``; a
    bare catalog DeviceID (bus/address None) falls back to the first VID:PID match. ``connect()``
    opens it, not this."""
    if supported_ids().get((device_id.vid, device_id.pid)) is None:
        return None
    dev = _live_dev(device_id)
    if dev is None:
        logger.debug("wlan_iface: no live %04x:%04x instance (bus=%s addr=%s)",
                     device_id.vid, device_id.pid, device_id.bus, device_id.address)
        return None
    got = resolve_driver(device_id.vid, device_id.pid, dev)
    if got is None:
        return None
    driver_cls, _key = got
    try:
        driver = driver_cls.from_usb_device(dev, device_id)
    except Exception as e:
        logger.debug("wlan_iface: %04x:%04x (%s): %s",
                     device_id.vid, device_id.pid, driver_cls.__name__, e)
        return None
    return WlanInterface(driver, name, device_id.description,
                         vid=device_id.vid, pid=device_id.pid, dev=dev,
                         chipset=device_id.chipset, vendor=device_id.vendor,
                         product_name=device_id.product_name,
                         bus=dev.bus, address=dev.address)


def wlan_ifaces() -> List[WlanInterface]:
    """Every present supported card as an (unconnected) WlanInterface, named wlan0..N. A one-shot
    convenience for the dev scripts; the app builds one card at a time through bring-up."""
    out: List[WlanInterface] = []
    for dev_id in devices():
        iface = wlan_iface(dev_id, name=f"wlan{len(out)}")
        if iface is not None:
            out.append(iface)
    return out


async def wlan_close(ifaces) -> None:
    """Close every interface from ``wlan_ifaces()``, tolerating a per-card close fault."""
    for iface in ifaces:
        try:
            await iface.close()
        except Exception:
            logger.debug("wlan_close: %s close failed", getattr(iface, "name", "?"))


class DeviceManager:
    """Owns bring-up + setup (connect, attach, permissions). One per app. The stateless bus queries
    and the interface builders (``wlan_iface``/``wlan_ifaces``/``wlan_close``) are module-level
    functions; ``devices``/``device`` are re-exposed as thin delegating methods so the app and
    ``DeviceWatch`` can call ``dm.devices()``."""

    def __init__(self, app, *, setup: Setup | None = None, prompter=None) -> None:
        self.app = app
        self.setup = setup or Setup.for_platform()
        if prompter is None:
            from wifit3.ui.bringup_prompter import BringupPrompter
            prompter = BringupPrompter(app)
        self.prompter = prompter
        self._name_counter = 0

    def devices(self) -> List[DeviceID]:
        return devices()

    def device(self, device_id: DeviceID) -> Optional[DeviceID]:
        return device(device_id)

    async def bringup(self, device_id, *, bail_at_permissions: bool = False) -> BringupResult:
        """Bring ``device_id`` up (installing setup if the card can't be opened yet) and attach it. Brings
        up exactly this one card: the caller decides the set (Splash loops over the checked cards). Shows
        the progress modal for the whole flow. ``bail_at_permissions`` returns a FAILED result instead of
        running setup when the card needs it (mid-session Windows, where a WinUSB install is disruptive)."""
        await self.prompter.open(f"Bringing up {device_id.description}…")
        try:
            return await self._bringup(device_id, bail_at_permissions=bail_at_permissions)
        finally:
            self.prompter.close()

    async def _bringup(self, device_id, *, bail_at_permissions: bool) -> BringupResult:
        if self.setup.requires_setup(device_id):
            if bail_at_permissions:
                return BringupResult.failed("Installation required. START it from the main menu.")
            device_id = await self.setup.install(device_id, self.prompter)
            if device_id is None:
                return BringupResult.cancelled()
        try:
            await self._connect_and_attach(device_id)
            return BringupResult.ready()
        except BringUpPermissionsError:
            if bail_at_permissions:
                return BringupResult.failed("Installation required. START it from the main menu.")
            # else fixable: fall through to setup
        except BringUpError as e:
            return BringupResult.failed(self._fault_message(device_id, e))
        except OSError as e:
            return BringupResult.failed(self._usb_fault_message(device_id, e))

        device_id = await self.setup.install(device_id, self.prompter)
        if device_id is None:
            return BringupResult.cancelled()              # declined or failed (setup already reported)

        try:
            await self._connect_and_attach(device_id)
            return BringupResult.ready()
        except BringUpError as e:
            return BringupResult.failed(self._fault_message(device_id, e))
        except OSError as e:
            return BringupResult.failed(self._usb_fault_message(device_id, e))

    async def uninstall(self, device_id) -> SetupResult:
        """Reverse a prior setup for ``device_id`` (the splash's ✕ button). Shows the progress modal
        so the setup's status lines (removing rules, revoking access) are visible."""
        await self.prompter.open(f"Uninstalling {device_id.description}…")
        try:
            return await self.setup.uninstall(device_id, self.prompter)
        finally:
            self.prompter.close()

    async def _connect_and_attach(self, device_id) -> None:
        iface = wlan_iface(device_id, name=self._next_name())
        if iface is None:
            raise BringUpError("discover", "card not present")
        try:
            if not await iface.connect(progress_cb=self.prompter.status_progress):
                # A few drivers return False (rather than raise) on a genuine bring-up fault; don't
                # attach a dead card.
                raise BringUpError("init", "the driver reported bring-up failure")
        except Exception:
            # Drop any partial USB claim so a following setup / retry isn't blocked by us holding it.
            try:
                await iface.close()
            except Exception:
                pass
            raise
        self._ensure_array().attach(iface)

    def _ensure_array(self) -> WlanArray:
        if self.app.array is None:
            array = WlanArray()
            array.register_disconnect_callback(self.app.notify_device_lost)
            self.app.array = array
        return self.app.array

    def _next_name(self) -> str:
        name = f"wlan{self._name_counter}"
        self._name_counter += 1
        return name

    @staticmethod
    def _fault_message(device_id, e: BringUpError) -> str:
        chip = device_id.chipset
        return f"{chip}: {e.stage} failed" + (f": {e.detail}" if e.detail else "")

    @staticmethod
    def _usb_fault_message(device_id, e: OSError) -> str:
        chip = device_id.chipset
        if is_device_gone(e) or e.errno in (errno.EIO, errno.ENODEV):
            return f"{chip}: adapter disconnected during bring-up; replug it and try again"
        return f"{chip}: USB I/O failed during bring-up: {e}"
