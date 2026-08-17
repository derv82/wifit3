"""Punter: builds the eviction frames per PuntMode and bursts them on the handed interface."""
import struct

from wifit3.campaigns.eviltwin import Punter, PuntMode
from wifit3.dot11.ie import ssid_ie, rates_ie, ds_param_ie, GENERIC_RSN_IE

_BSSID_B = bytes.fromhex("9483c48c3f78")
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


async def test_punt_frames_by_mode():
    cases = {PuntMode.CSA: {0x80}, PuntMode.DEAUTH: {0xC0},
             PuntMode.BOTH: {0x80, 0xC0}, PuntMode.NONE: set()}
    for mode, want_subtypes in cases.items():
        iface = _FakeIface()
        await Punter(mode, _BEACON, _BSSID_B, csa_channel=1).punt(iface)
        assert {f[0] for f in iface.sent} == want_subtypes


async def test_csa_channel_is_decoupled():
    a, b = _FakeIface(), _FakeIface()
    await Punter(PuntMode.CSA, _BEACON, _BSSID_B, csa_channel=1).punt(a)
    await Punter(PuntMode.CSA, _BEACON, _BSSID_B, csa_channel=6).punt(b)
    assert a.sent[0] != b.sent[0]      # different CSA target channel -> different beacon bytes
