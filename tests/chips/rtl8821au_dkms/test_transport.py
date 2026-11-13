"""Hardware-free regression for RX endpoint selection.

Realtek ships two USB descriptor layouts for the 8821AU silicon. Single-interface
parts expose one bulk-IN (0x81), so "first bulk-IN in the config" is correct there.
Multi-function parts -- the rtw88-shaped set seen on bare 0bda:0823 reference
modules -- expose four functions, and only interface 2's bulk-IN 0x84 carries
frames:

    intf 0 (class 0xe0)  bulk-IN 0x81, bulk-OUT 0x02, bulk-IN 0x82   <- decoys
    intf 1 (class 0xe0)  isochronous in/out                            (BT)
    intf 2 (class 0xff)  bulk-IN 0x84 + TX queues 0x05/0x06/0x08/0x09  <- the data path

Picking the first bulk-IN lands on 0x82, every read times out, and the card looks
deaf while the radio is fine. Pins the selection rule and the interface claim.
"""
import usb.util

from wifit3.chips.rtl8821au_dkms import transport

EP_TYPE_ISO = 0x01
EP_TYPE_BULK = 0x02
EP_TYPE_INT = 0x03


class FakeEndpoint:
    def __init__(self, addr: int, ep_type: int = EP_TYPE_BULK):
        self.bEndpointAddress = addr
        self.bmAttributes = ep_type


class FakeInterface:
    def __init__(self, number: int, iface_class: int, endpoints):
        self.bInterfaceNumber = number
        self.bInterfaceClass = iface_class
        self._endpoints = endpoints

    def __iter__(self):
        return iter(self._endpoints)


class FakeConfig:
    def __init__(self, interfaces):
        self._interfaces = interfaces

    def __iter__(self):
        return iter(self._interfaces)


class FakeDevice:
    def __init__(self, config):
        self._config = config

    def get_active_configuration(self):
        return self._config

    def read(self, *a, **kw):        # never reached by these tests
        raise AssertionError("no I/O expected")


MULTIFUNCTION = FakeConfig([
    FakeInterface(0, 0xE0, [FakeEndpoint(0x81), FakeEndpoint(0x02, 0x02),
                            FakeEndpoint(0x82)]),
    FakeInterface(1, 0xE0, [FakeEndpoint(0x03, EP_TYPE_ISO),
                            FakeEndpoint(0x83, EP_TYPE_ISO)]),
    FakeInterface(2, 0xFF, [FakeEndpoint(0x84), FakeEndpoint(0x05, EP_TYPE_BULK),
                            FakeEndpoint(0x06, EP_TYPE_BULK),
                            FakeEndpoint(0x87, EP_TYPE_INT),
                            FakeEndpoint(0x08, EP_TYPE_BULK),
                            FakeEndpoint(0x09, EP_TYPE_BULK)]),
])

SIMPLE = FakeConfig([
    FakeInterface(0, 0xFF, [FakeEndpoint(0x81), FakeEndpoint(0x02, EP_TYPE_BULK)]),
])


def test_multifunction_layout_prefers_real_rx_endpoint():
    t = transport.RTL8821AUDkmsTransport(FakeDevice(MULTIFUNCTION))
    assert t._bulk_in_ep() == 0x84
    assert t._in_ep_iface == 2


def test_multifunction_layout_ignores_decoy_bulk_ins():
    t = transport.RTL8821AUDkmsTransport(FakeDevice(MULTIFUNCTION))
    decoys = {ep.bEndpointAddress for intf in MULTIFUNCTION for ep in intf
              if (usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
                  and usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)}
    assert decoys == {0x81, 0x82, 0x84}, "fixture must model all three bulk-IN endpoints"
    assert t._bulk_in_ep() not in (0x81, 0x82)


def test_single_interface_layout_keeps_first_bulk_in():
    t = transport.RTL8821AUDkmsTransport(FakeDevice(SIMPLE))
    assert t._bulk_in_ep() == 0x81
    assert t._in_ep_iface == 0


def test_claim_rx_interface_claims_the_endpoint_interface(monkeypatch):
    claimed = []
    monkeypatch.setattr(transport.usb.util, "claim_interface",
                        lambda dev, num: claimed.append(num))
    t = transport.RTL8821AUDkmsTransport(FakeDevice(MULTIFUNCTION))
    assert t.claim_rx_interface() == 2
    assert claimed == [2]


def test_claim_rx_interface_skips_interface_zero(monkeypatch):
    """Interface 0 is claimed by the driver's bring-up; claiming it twice can fail on
    WinUSB, so the helper must not re-claim it on the simple layout."""
    claimed = []
    monkeypatch.setattr(transport.usb.util, "claim_interface",
                        lambda dev, num: claimed.append(num))
    t = transport.RTL8821AUDkmsTransport(FakeDevice(SIMPLE))
    assert t.claim_rx_interface() == 0
    assert claimed == []


def test_missing_bulk_in_endpoint_raises():
    import pytest
    t = transport.RTL8821AUDkmsTransport(
        FakeDevice(FakeConfig([FakeInterface(0, 0xFF, [FakeEndpoint(0x02, EP_TYPE_BULK)])])))
    with pytest.raises(RuntimeError, match="no bulk-IN endpoint"):
        t._bulk_in_ep()
