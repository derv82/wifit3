"""Driver registry (env A/B switch for the Realtek 11ac pairs) + the discovery surface.

The DKMS port and the mainline driver claim the same VID:PID. The registry resolves each pair to a
single driver via ``env_or_none``; the env var picks which, defaulting to DKMS. ``find_devices`` /
``build_interface`` are the enumeration + factory the bring-up engine drives.
"""
import pytest

from wifit3.chips.driver import DeviceID
from wifit3.wlan import discovery


@pytest.fixture(autouse=True)
def _fresh_registry():
    # The registry is built once and cached; null it so each test's env var is read fresh.
    discovery._DRIVER_CLASSES = None
    yield
    discovery._DRIVER_CLASSES = None


class _FakeDev:
    def __init__(self, vid, pid):
        self.idVendor, self.idProduct = vid, pid


def _driver_for(vid, pid):
    cls, _ = discovery._match_driver(_FakeDev(vid, pid))
    return cls.__name__


def test_rtl8821_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8821", raising=False)
    assert _driver_for(0x0BDA, 0x0811) == "Rtl8821auDkmsDriver"


def test_rtl8821_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "MainLine")   # case-insensitive
    assert _driver_for(0x0BDA, 0x0811) == "RTL8821AUDriver"


def test_rtl8821_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "dkms")   # any non-"mainline" -> default DKMS
    assert _driver_for(0x0BDA, 0x0811) == "Rtl8821auDkmsDriver"


def test_both_8821_drivers_claim_0811():
    from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver
    assert (0x0BDA, 0x0811) in {(e.vid, e.pid) for e in Rtl8821auDkmsDriver.SUPPORTED_IDS}


def test_rtl8814_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8814", raising=False)
    assert _driver_for(0x0BDA, 0x8813) == "Rtl8814auDkmsDriver"


def test_rtl8814_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8814", "mainline")
    assert _driver_for(0x0BDA, 0x8813) == "RTL8814AUDriver"


def test_rtl8812_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8812", raising=False)
    assert _driver_for(0x0BDA, 0x8812) == "Rtl8812auDkmsDriver"


def test_rtl8812_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8812", "MainLine")   # case-insensitive
    assert _driver_for(0x0BDA, 0x8812) == "RTL8812AUDriver"


def test_rtl8812_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8812", "dkms")
    assert _driver_for(0x0BDA, 0x8812) == "Rtl8812auDkmsDriver"


def test_both_8812_drivers_claim_8812():
    from wifit3.chips.rtl8812au_dkms.driver import Rtl8812auDkmsDriver
    assert (0x0BDA, 0x8812) in {(e.vid, e.pid) for e in Rtl8812auDkmsDriver.SUPPORTED_IDS}


def test_rtl8822_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8822", raising=False)
    assert _driver_for(0x2357, 0x0138) == "Rtl8822buDkmsDriver"


def test_rtl8822_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8822", "MainLine")   # case-insensitive
    assert _driver_for(0x2357, 0x0138) == "RTL8822BUDriver"


def test_rtl8822_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8822", "dkms")
    assert _driver_for(0x2357, 0x0138) == "Rtl8822buDkmsDriver"


def test_both_8822_drivers_claim_0138():
    from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver
    assert (0x2357, 0x0138) in {(e.vid, e.pid) for e in Rtl8822buDkmsDriver.SUPPORTED_IDS}


# --- find_devices / build_interface --------------------------------------------------------------

class _FakeDriver:
    """Minimal driver satisfying WlanInterface's constructor (registers two callbacks)."""
    def __init__(self):
        self._rx = None
        self._disc = None

    @classmethod
    def from_usb_device(cls, dev, id_entry):
        inst = cls()
        inst.dev, inst.id_entry = dev, id_entry
        return inst

    def register_rx_callback(self, cb):
        self._rx = cb

    def register_disconnect_callback(self, cb):
        self._disc = cb


def _stub_scan(monkeypatch, handles):
    monkeypatch.setattr(discovery.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(discovery, "_scan_bus", lambda backend: handles)


def test_find_devices_returns_matched_ids(monkeypatch):
    d1 = DeviceID(0x0BDA, 0x8813, "RTL8814AU")
    d2 = DeviceID(0x148F, 0x5370, "RT5370")
    _stub_scan(monkeypatch, [(_FakeDev(0x0BDA, 0x8813), _FakeDriver, d1),
                             (_FakeDev(0x148F, 0x5370), _FakeDriver, d2)])
    assert discovery.find_devices() == [d1, d2]


def test_build_interface_dispatches_matching_driver(monkeypatch):
    entry = DeviceID(0x0BDA, 0x8813, "RTL8814AU")
    dev = _FakeDev(0x0BDA, 0x8813)
    _stub_scan(monkeypatch, [(dev, _FakeDriver, entry)])
    iface = discovery.build_interface(entry, name="wlan3")
    assert iface is not None
    assert iface.name == "wlan3" and iface.vid == 0x0BDA and iface.dev is dev


def test_build_interface_none_when_absent(monkeypatch):
    _stub_scan(monkeypatch, [])
    assert discovery.build_interface(DeviceID(0x1, 0x2, "nope")) is None


def test_build_interface_none_when_driver_raises(monkeypatch):
    class _Boom(_FakeDriver):
        @classmethod
        def from_usb_device(cls, dev, id_entry):
            raise RuntimeError("not ported yet")

    entry = DeviceID(0x0BDA, 0x8813, "RTL8814AU")
    _stub_scan(monkeypatch, [(_FakeDev(0x0BDA, 0x8813), _Boom, entry)])
    assert discovery.build_interface(entry) is None
