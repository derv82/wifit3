"""Punter: builds the eviction frames for the enabled PuntModes and bursts them on the interface."""
import struct

from wifit3.campaigns.eviltwin import Punter, PuntMode
from wifit3.dot11.ie import ssid_ie, rates_ie, ds_param_ie, GENERIC_RSN_IE

_BSSID_B = bytes.fromhex("9483c48c3f78")
_TWIN_B = bytes.fromhex("9483c48c3f79")
_C1 = bytes.fromhex("02aaaaaaaaaa")
_C2 = bytes.fromhex("02bbbbbbbbbb")
_BROADCAST = b"\xff" * 6
_FIXED = struct.pack("<Q", 0) + struct.pack("<H", 100) + b"\x11\x04"
_BEACON = (b"\x80\x00\x00\x00" + _BROADCAST + _BSSID_B + _BSSID_B + b"\x00\x00"
           + _FIXED + ssid_ie("GL-Test") + rates_ie() + ds_param_ie(11) + GENERIC_RSN_IE)


class _FakeIface:
    def __init__(self):
        self.sent: list[bytes] = []

    async def send_no_wait(self, frame: bytes) -> bool:
        self.sent.append(bytes(frame))
        return True


def _punter(modes):
    return Punter(modes, _BEACON, _BSSID_B, csa_channel=1, twin_bssid=_TWIN_B, twin_channel=6,
                  source_channel=11)


async def test_punt_frames_by_mode():
    # mgmt subtype in FC[0]: CSA beacon 0x80, deauth 0xC0. Empty modes send nothing.
    cases = {(PuntMode.CSA,): {0x80}, (PuntMode.DEAUTH,): {0xC0},
             (PuntMode.DEAUTH, PuntMode.CSA): {0x80, 0xC0}, (): set()}
    for modes, want in cases.items():
        iface = _FakeIface()
        await _punter(modes).punt(iface, clients=[_C1])
        assert {f[0] for f in iface.sent} == want


async def test_btm_is_unicast_per_client():
    iface = _FakeIface()
    await _punter((PuntMode.BTM,)).punt(iface, clients=[_C1, _C2])
    assert {f[0] for f in iface.sent} == {0xD0}                 # all Action frames
    assert {bytes(f[4:10]) for f in iface.sent} == {_C1, _C2}   # one steer addressed to each client
    assert all(f[16:22] == _BSSID_B for f in iface.sent)        # a3 = spoofed real AP


async def test_btm_with_no_clients_sends_nothing():
    iface = _FakeIface()
    await _punter((PuntMode.BTM,)).punt(iface, clients=[])
    assert iface.sent == []


async def test_csa_channel_is_decoupled():
    a, b = _FakeIface(), _FakeIface()
    await _punter((PuntMode.CSA,)).punt(a)
    await Punter((PuntMode.CSA,), _BEACON, _BSSID_B, csa_channel=6,
                 twin_bssid=_TWIN_B, twin_channel=6, source_channel=11).punt(b)
    assert a.sent[0] != b.sent[0]      # different CSA target channel -> different beacon bytes
