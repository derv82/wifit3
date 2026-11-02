"""A USB bring-up fault must name a remedy, not just an errno.

Measured on an MT7601U left authorized but unconfigured: claim_interface raises
ENOENT, which used to surface as "USB I/O failed: [Errno 2] Entity not found" --
true, and useless to someone holding the dongle.
"""
import errno
from types import SimpleNamespace

import pytest

from wifit3.device.manager import DeviceManager

_ID = SimpleNamespace(chipset="mt7601u")


@pytest.mark.parametrize("err", [errno.EIO, errno.ENODEV])
def test_a_disconnected_adapter_says_replug(err: int) -> None:
    msg = DeviceManager._usb_fault_message(_ID, OSError(err, "gone"))
    assert "replug" in msg.lower()
    assert "Entity not found" not in msg


def test_an_enumerable_but_unbound_dongle_names_the_remedy() -> None:
    """ENOENT from claim_interface means interface 0 does not exist: the dongle is dead,
    not transiently busy. The user has to replug it, so say that."""
    msg = DeviceManager._usb_fault_message(_ID, OSError(errno.ENOENT, "Entity not found"))

    assert "not answering" in msg
    assert "replug" in msg.lower()
    assert "Entity not found" not in msg      # the raw errno adds nothing actionable
    assert "mt7601u" in msg                   # still names the chipset


def test_an_unrelated_io_error_keeps_its_own_detail() -> None:
    """Not every errno is a dead dongle; the generic branch must still surface the cause."""
    msg = DeviceManager._usb_fault_message(_ID, OSError(errno.EPIPE, "broken pipe"))
    assert "broken pipe" in msg
    assert "replug" not in msg.lower()
