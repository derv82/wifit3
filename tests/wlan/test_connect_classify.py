"""WlanInterface.connect turns a fixable "can't open the card" failure into BringUpPermissionsError.

Drivers wrap the libusb open/claim error in BringUpError (sometimes through an IOError), so the
classifier walks the cause/context chain rather than the top exception. A genuine bring-up fault
(firmware/init) and an unrelated USBError (e.g. a timeout) must pass through unchanged.
"""
import usb.core
import pytest

from wifit3.errors import BringUpError, BringUpPermissionsError, is_permission_error
from wifit3.wlan.interface import WlanInterface


class _Driver:
    def __init__(self, exc=None, ok=True):
        self._exc, self._ok = exc, ok

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
        pass

    async def connect(self, progress_cb=None):
        if self._exc is not None:
            raise self._exc
        return self._ok


def _iface(exc=None, ok=True):
    return WlanInterface(_Driver(exc, ok), "wlan0", "Test card", vid=0x1, pid=0x2)


async def test_notimplemented_becomes_permissions():
    with pytest.raises(BringUpPermissionsError):
        await _iface(NotImplementedError("no WinUSB backend")).connect()


async def test_eacces_usberror_becomes_permissions():
    with pytest.raises(BringUpPermissionsError):
        await _iface(usb.core.USBError("access denied", errno=13)).connect()


async def test_wrapped_busy_becomes_permissions():
    be = BringUpError("bring-up", "claim failed")
    be.__cause__ = usb.core.USBError("busy", -6)   # LIBUSB_ERROR_BUSY
    with pytest.raises(BringUpPermissionsError):
        await _iface(be).connect()


async def test_double_wrapped_through_ioerror():
    inner = usb.core.USBError("access", errno=13)
    mid = IOError("set_configuration failed")
    mid.__cause__ = inner
    be = BringUpError("bring-up", "open failed")
    be.__cause__ = mid
    with pytest.raises(BringUpPermissionsError):
        await _iface(be).connect()


async def test_permissions_error_keeps_stage_and_detail():
    be = BringUpError("bring-up", "usbfs node not writable")
    be.__cause__ = usb.core.USBError("access", -3)
    with pytest.raises(BringUpPermissionsError) as ei:
        await _iface(be).connect()
    assert ei.value.stage == "bring-up" and ei.value.detail == "usbfs node not writable"


async def test_real_bringup_error_stays():
    with pytest.raises(BringUpError) as ei:
        await _iface(BringUpError("firmware", "FW_READY timeout")).connect()
    assert not isinstance(ei.value, BringUpPermissionsError)


async def test_timeout_usberror_passes_through():
    with pytest.raises(usb.core.USBError):
        await _iface(usb.core.USBError("operation timed out", errno=110)).connect()


async def test_success_returns_true():
    assert await _iface(ok=True).connect() is True


def test_is_permission_error_direct():
    assert is_permission_error(NotImplementedError())
    assert is_permission_error(usb.core.USBError("x", -3))
    assert is_permission_error(usb.core.USBError("x", errno=16))
    assert not is_permission_error(usb.core.USBError("x", errno=110))
    assert not is_permission_error(BringUpError("firmware", "y"))
