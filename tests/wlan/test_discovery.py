"""Driver registry (env A/B switch for the Realtek 11ac pairs) + the device-scan surface.

The DKMS port and the mainline driver claim the same VID:PID. The family table resolves each pair to a
single driver via its env var; the var picks which, defaulting to DKMS. ``devices`` / ``wlan_iface``
are the enumeration + factory the bring-up engine drives.
"""
import pytest
import usb.core

import wifit3.device.manager as manager
from wifit3.chips.driver import DeviceID


@pytest.fixture(autouse=True)
def _fresh_registry():
    # The VID:PID map is built once and cached; clear it so each test's env var is read fresh.
    manager.supported_ids.cache_clear()
    yield
    manager.supported_ids.cache_clear()


def _driver_for(vid, pid):
    cls, _ = manager.driver_for(vid, pid)
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
    from wifit3.chips.rtl8821au_dkms import SUPPORTED_IDS
    assert (0x0BDA, 0x0811) in {(e.vid, e.pid) for e in SUPPORTED_IDS}


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
    from wifit3.chips.rtl8812au_dkms import SUPPORTED_IDS
    assert (0x0BDA, 0x8812) in {(e.vid, e.pid) for e in SUPPORTED_IDS}


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
    from wifit3.chips.rtl8822bu_dkms import SUPPORTED_IDS
    assert (0x2357, 0x0138) in {(e.vid, e.pid) for e in SUPPORTED_IDS}


def test_rtl8822_t4u_v3_plus_uses_dkms_label(monkeypatch):
    from wifit3.chips.products import TPLink
    from wifit3.device import manager
    monkeypatch.delenv("WIFIT3_RTL8822", raising=False)
    manager.supported_ids.cache_clear()

    cls, _key = manager.driver_for(0x2357, 0x0115)
    entry, _key, _import_driver = manager.supported_ids()[(0x2357, 0x0115)]

    assert cls.__name__ == "Rtl8822buDkmsDriver"
    assert entry.product_name == TPLink.ARCHER_T4U_V3_PLUS


def test_23570137_catalog_default_is_mt76x2u():
    # The catalog (no live device) keeps the mainline claim for the reused VID:PID.
    assert _driver_for(0x2357, 0x0137) == "MT76x2UDriver"


def test_23570137_mt76x2u_layout_resolves_mt7612u(monkeypatch):
    # MT7612U exposes bulk-IN 0x85 (CMD_RESP); the 8822CU does not.
    _stub_bus(monkeypatch, [_FakeDev(0x2357, 0x0137, endpoints=[0x84, 0x85, 0x08, 0x05, 0x06])])
    assert [d.chipset for d in manager.devices()] == ["MT7612U"]


def test_23570137_rtl8822cu_layout_resolves_8822cu(monkeypatch):
    # RTL8822CU: single bulk-IN 0x84, bulk-OUT 0x05/0x06/0x08 (0x87 is an interrupt event EP).
    _stub_bus(monkeypatch, [_FakeDev(0x2357, 0x0137, endpoints=[0x84, 0x05, 0x06, 0x08])])
    assert [d.chipset for d in manager.devices()] == ["RTL8822CU"]


def test_wlan_iface_23570137_dispatches_resolved_rtl8822cu(monkeypatch):
    dev = _FakeDev(0x2357, 0x0137, endpoints=[0x84, 0x05, 0x06, 0x08])
    entry = DeviceID(0x2357, 0x0137, "RTL8822CU", bus=1, address=1)
    monkeypatch.setattr(manager, "driver_for", lambda v, p: pytest.fail("catalog default must not win"))
    monkeypatch.setattr(manager.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(usb.core, "find", lambda **kw: dev)
    iface = manager.wlan_iface(entry)
    assert iface is not None
    assert type(iface.driver).__name__ == "RTL8822CUDriver"


def test_wlan_iface_23570137_with_ep85_dispatches_mt76x2u(monkeypatch):
    dev = _FakeDev(0x2357, 0x0137, endpoints=[0x84, 0x85, 0x08, 0x05, 0x06])
    entry = DeviceID(0x2357, 0x0137, "MT7612U", bus=1, address=1)
    monkeypatch.setattr(manager, "driver_for", lambda v, p: pytest.fail("catalog default must not win"))
    monkeypatch.setattr(manager.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(usb.core, "find", lambda **kw: dev)
    iface = manager.wlan_iface(entry)
    assert iface is not None
    assert type(iface.driver).__name__ == "MT76x2UDriver"


@pytest.mark.parametrize("pid", [0xC82C, 0xC82E, 0xC812, 0xD820, 0xD82B])
def test_realtek_default_ids_claim_the_8822cu(pid):
    # The vendor's RTL8822C demoboard defaults [SRC os_dep/linux/usb_intf.c:296-300].
    assert _driver_for(0x0BDA, pid) == "RTL8822CUDriver"


def test_the_alpha_id_stays_with_the_8822bu(monkeypatch):
    # 13b1:0043 is in the vendor 8822C table but commented out of ours: 88x2bu already claims it.
    monkeypatch.delenv("WIFIT3_RTL8822", raising=False)
    assert _driver_for(0x13B1, 0x0043) == "Rtl8822buDkmsDriver"


def test_key_is_the_family_key_not_the_package_dir():
    # The setup key must stay the family key (ar9271, not ar9271_v2), so a prior install's files resolve.
    _cls, key = manager.driver_for(0x0CF3, 0x9271)
    assert key == "ar9271"


# --- devices / wlan_iface ------------------------------------------------------------------------

class _FakeEP:
    def __init__(self, addr):
        self.bEndpointAddress = addr


class _FakeDev:
    def __init__(self, vid, pid, bus=1, address=1, endpoints=()):
        self.idVendor, self.idProduct = vid, pid
        self.bus, self.address = bus, address
        self._eps = tuple(_FakeEP(a) for a in endpoints)

    def get_active_configuration(self):
        if not self._eps:
            raise RuntimeError("fake device has no active configuration")
        eps = self._eps

        class _Intf:
            def __iter__(self):
                return iter(eps)

        return iter([_Intf()])


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


def _stub_bus(monkeypatch, devs):
    monkeypatch.setattr(manager.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(usb.core, "find", lambda **kw: list(devs))


def test_devices_tags_each_match_with_its_bus_address(monkeypatch):
    _stub_bus(monkeypatch, [_FakeDev(0x0BDA, 0x8813, bus=1, address=4),
                            _FakeDev(0x148F, 0x5370, bus=1, address=5)])
    out = manager.devices()
    assert [d.instance_key for d in out] == [(0x0BDA, 0x8813, 1, 4), (0x148F, 0x5370, 1, 5)]


def test_devices_ignores_unsupported_vidpid(monkeypatch):
    _stub_bus(monkeypatch, [_FakeDev(0x1234, 0x5678, bus=1, address=4),
                            _FakeDev(0x148F, 0x5370, bus=1, address=5)])
    assert [d.instance_key for d in manager.devices()] == [(0x148F, 0x5370, 1, 5)]


def test_devices_distinguishes_two_identical_cards(monkeypatch):
    _stub_bus(monkeypatch, [_FakeDev(0x0E8D, 0x7961, bus=2, address=32),
                            _FakeDev(0x0E8D, 0x7961, bus=2, address=35)])
    assert len({x.instance_key for x in manager.devices()}) == 2


def test_device_returns_the_current_address_for_a_moved_device(monkeypatch):
    # device_id carries a stale address (WinUSB moved it); device() returns the live one.
    stale = DeviceID(0x148F, 0x5372, "RT5372", bus=2, address=63)
    _stub_bus(monkeypatch, [_FakeDev(0x148F, 0x5372, bus=2, address=64)])
    got = manager.device(stale)
    assert got is not None and got.instance_key == (0x148F, 0x5372, 2, 64)


def test_device_none_when_vidpid_absent(monkeypatch):
    _stub_bus(monkeypatch, [])
    assert manager.device(DeviceID(0x1, 0x2, "nope")) is None


def test_wlan_iface_dispatches_matching_driver(monkeypatch):
    entry = DeviceID(0x0BDA, 0x8813, "RTL8814AU", bus=1, address=7)
    dev = _FakeDev(0x0BDA, 0x8813, bus=1, address=7)
    monkeypatch.setattr(manager, "driver_for", lambda v, p: (_FakeDriver, "rtl8814au"))
    monkeypatch.setattr(manager.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(usb.core, "find", lambda **kw: dev)
    iface = manager.wlan_iface(entry, name="wlan3")
    assert iface is not None
    assert iface.name == "wlan3" and iface.vid == 0x0BDA and iface.dev is dev
    assert iface.instance_key == (0x0BDA, 0x8813, 1, 7)


def test_wlan_iface_selects_the_addressed_instance(monkeypatch):
    # Two identical cards on the bus; the entry names one by (bus, address). wlan_iface must open THAT
    # one via a targeted find, not the first VID:PID match (the two-identical-cards hotplug bug).
    entry = DeviceID(0x0E8D, 0x7961, "MT7921AU", bus=2, address=35)
    second = _FakeDev(0x0E8D, 0x7961, bus=2, address=35)
    seen = {}

    def _find(**kw):
        seen.update(kw)
        return second if kw.get("address") == 35 else None

    monkeypatch.setattr(manager, "driver_for", lambda v, p: (_FakeDriver, "mt7921au"))
    monkeypatch.setattr(manager.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(usb.core, "find", _find)
    iface = manager.wlan_iface(entry)
    assert iface is not None and iface.dev is second
    assert seen["bus"] == 2 and seen["address"] == 35


def test_wlan_iface_none_when_absent(monkeypatch):
    monkeypatch.setattr(manager, "driver_for", lambda v, p: (_FakeDriver, "k"))
    monkeypatch.setattr(manager.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(usb.core, "find", lambda **kw: None)
    assert manager.wlan_iface(DeviceID(0x0BDA, 0x8813, "RTL8814AU")) is None


def test_wlan_iface_none_when_unsupported(monkeypatch):
    assert manager.wlan_iface(DeviceID(0x1, 0x2, "nope")) is None


def test_wlan_iface_none_when_driver_raises(monkeypatch):
    class _Boom(_FakeDriver):
        @classmethod
        def from_usb_device(cls, dev, id_entry):
            raise RuntimeError("not ported yet")

    monkeypatch.setattr(manager, "driver_for", lambda v, p: (_Boom, "k"))
    monkeypatch.setattr(manager.libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(usb.core, "find", lambda **kw: _FakeDev(0x0BDA, 0x8813))
    assert manager.wlan_iface(DeviceID(0x0BDA, 0x8813, "RTL8814AU")) is None


def test_linux_node_path_targets_the_addressed_instance(monkeypatch):
    entry = DeviceID(0x0E8D, 0x7961, "MT7921AU", bus=2, address=35)
    _stub_bus(monkeypatch, [_FakeDev(0x0E8D, 0x7961, bus=2, address=32),
                            _FakeDev(0x0E8D, 0x7961, bus=2, address=35)])
    assert manager.linux_node_path(entry) == "/dev/bus/usb/002/035"


def test_linux_node_path_first_match_for_catalog_entry(monkeypatch):
    entry = DeviceID(0x148F, 0x5370, "RT5370")   # no (bus, address)
    _stub_bus(monkeypatch, [_FakeDev(0x148F, 0x5370, bus=1, address=5)])
    assert manager.linux_node_path(entry) == "/dev/bus/usb/001/005"
