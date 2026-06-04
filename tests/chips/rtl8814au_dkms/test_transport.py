"""bulk_in timeout handling — a benign read timeout (no traffic) must return None, not
raise, or the shared RxReaderThread counts it as a fatal error and gives up. Regression
for the 5 GHz scan dying on quiet DFS channels: the Windows/WinUSB backend reports
"[Errno 10060] Operation timed out" (errno 10060, "timed out" — no "timeout" substring),
which the old message-only check missed.
"""
import pytest
import usb.core

from wifit3.chips.rtl8814au_dkms.transport import Rtl8814auTransport


class _RaisingDev:
    def __init__(self, exc):
        self._exc = exc

    def read(self, ep, size, timeout):
        raise self._exc


def _transport(exc):
    t = Rtl8814auTransport(_RaisingDev(exc))
    t._in_ep = 0x84          # pre-cache so bulk_in skips the endpoint probe
    return t


def test_bulk_in_windows_timeout_returns_none():
    # The case that killed the 5 GHz scan: USBTimeoutError, errno 10060, "Operation timed out".
    t = _transport(usb.core.USBTimeoutError("Operation timed out", errno=10060))
    assert t.bulk_in() is None


def test_bulk_in_libusb_timeout_returns_none():
    t = _transport(usb.core.USBTimeoutError("Operation timed out", errno=110))
    assert t.bulk_in() is None


def test_bulk_in_plain_usberror_timeout_returns_none():
    # A backend raising plain USBError (not the timeout subclass) for a timeout.
    t = _transport(usb.core.USBError("Operation timed out", errno=10060))
    assert t.bulk_in() is None


def test_bulk_in_real_fault_propagates():
    # A real pipe fault must NOT be swallowed (else a wedged pipe looks like a quiet channel).
    t = _transport(usb.core.USBError("pipe error", errno=5))
    with pytest.raises(usb.core.USBError):
        t.bulk_in()


def test_bulk_in_success_returns_bytes():
    class _OkDev:
        def read(self, ep, size, timeout):
            return b"\x01\x02\x03"

    t = Rtl8814auTransport(_OkDev())
    t._in_ep = 0x84
    assert t.bulk_in() == b"\x01\x02\x03"
