"""ZeroCdEjector: the merged-ID scan, per-port rate limit, and the CBW the eject sends. No hardware:
the eject function and the bus enumeration are injected."""
import struct

from wifit3.device import zerocd
from wifit3.device.zerocd import ZeroCdEjector, _start_stop_unit_cbw

_STUB = (0x0BDA, 0x1A2B)
_OTHER = (0x1234, 0x5678)


class _Dev:
    """A usb.core.Device stand-in: just what the ejector reads (ids + port identity)."""
    def __init__(self, vid, pid, bus=1, ports=(1,)):
        self.idVendor = vid
        self.idProduct = pid
        self.bus = bus
        self.port_numbers = ports
        self.address = ports[-1]


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _recorder():
    ejected = []
    return ejected, lambda dev: ejected.append((dev.idVendor, dev.idProduct)) or True


def _ejector(devs, *, ids=(_STUB,), clock=None, eject_fn=None):
    ejected, rec = _recorder()
    ej = ZeroCdEjector(lambda: ids, lambda: list(devs),
                       eject_fn=eject_fn or rec, clock=clock)
    return ej, ejected


def test_cbw_is_31_bytes_start_stop_unit():
    cbw = _start_stop_unit_cbw()
    assert len(cbw) == 31
    sig, tag, dlen, flags, lun, cblen = struct.unpack("<4sIIBBB", cbw[:15])
    assert sig == b"USBC" and dlen == 0 and flags == 0 and lun == 0 and cblen == 6
    assert cbw[15:21] == bytes([0x1B, 0, 0, 0, 0x02, 0])   # SCSI START STOP UNIT, LoEj=1


def test_ejects_only_matching_ids():
    ej, ejected = _ejector([_Dev(*_STUB), _Dev(*_OTHER)])
    ej.eject_present()
    assert ejected == [_STUB]


def test_no_ids_skips_enumeration():
    called = []
    ej = ZeroCdEjector(lambda: (), lambda: called.append(1) or [], eject_fn=lambda d: True)
    ej.eject_present()
    assert called == []


def test_rate_limited_after_three_attempts_on_a_port():
    clock = _Clock()
    devs = [_Dev(*_STUB)]
    ej, ejected = _ejector(devs, clock=clock)
    for _ in range(5):
        ej.eject_present()
    assert ejected == [_STUB, _STUB, _STUB]   # 4th and 5th within the window are suppressed


def test_window_expiry_allows_again():
    clock = _Clock()
    ej, ejected = _ejector([_Dev(*_STUB)], clock=clock)
    for _ in range(3):
        ej.eject_present()
    clock.t += zerocd._WINDOW_S + 1
    ej.eject_present()
    assert len(ejected) == 4


def test_twins_on_different_ports_are_limited_independently():
    clock = _Clock()
    devs = [_Dev(*_STUB, ports=(1,)), _Dev(*_STUB, ports=(2,))]
    ej, ejected = _ejector(devs, clock=clock)
    for _ in range(3):
        ej.eject_present()
    assert ejected == [_STUB] * 6   # 3 attempts each, both ports still under the cap


def test_failed_eject_still_counts_against_the_limit():
    calls = []
    ej = ZeroCdEjector(lambda: (_STUB,), lambda: [_Dev(*_STUB)],
                       eject_fn=lambda dev: calls.append(1) or False)
    for _ in range(5):
        ej.eject_present()
    assert len(calls) == 3   # a failing stub is still capped, so we don't spin on it


def test_scan_error_is_swallowed():
    def boom():
        raise RuntimeError("no backend")
    ej = ZeroCdEjector(lambda: (_STUB,), boom, eject_fn=lambda d: True)
    ej.eject_present()   # must not raise
