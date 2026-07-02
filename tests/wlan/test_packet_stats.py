"""Tests for the Focus live-packet-dashboard counters: the pure PacketStats
tally plus its RX/TX wiring into WlanInterface."""

import struct

from wifit3.wlan.interface import WlanInterface
from wifit3.wlan.packet_stats import PACKET_CLASSES, PacketStats

from tests.frames import pkt

BSSID = "00:11:22:33:44:55"
CLIENT = "aa:bb:cc:dd:ee:ff"


# ----- PacketStats (pure) ----------------------------------------------------

def test_rx_class_mapping():
    s = PacketStats()
    s.record_rx(BSSID, "beacon")
    s.record_rx(BSSID, "data")
    s.record_rx(BSSID, "qos_data")   # folds into 'data'
    s.record_rx(BSSID, "wep_data")   # → wep_iv
    s.record_rx(BSSID, "eapol")
    s.record_rx(BSSID, "deauth")
    snap = s.snapshot(BSSID)
    assert snap["beacon"] == 1
    assert snap["data"] == 2
    assert snap["wep_iv"] == 1
    assert snap["eapol"] == 1
    assert snap["deauth"] == 1
    assert snap["inject"] == 0


def test_rx_untracked_types_are_noop():
    s = PacketStats()
    for t in ("probe_req", "probe_resp", "assoc_req", "assoc_resp", "mgmt_5", "ctrl_11"):
        s.record_rx(BSSID, t)
    assert s.snapshot(BSSID) == dict.fromkeys(PACKET_CLASSES, 0)


def test_tx_deauth_vs_inject():
    s = PacketStats()
    s.record_tx(BSSID, is_deauth=True)
    s.record_tx(BSSID, is_deauth=False)
    s.record_tx(BSSID, is_deauth=False)
    snap = s.snapshot(BSSID)
    assert snap["deauth"] == 1
    assert snap["inject"] == 2


def test_snapshot_unknown_bssid_is_zero_filled_and_not_registered():
    s = PacketStats()
    snap = s.snapshot("de:ad:be:ef:00:00")
    assert snap == dict.fromkeys(PACKET_CLASSES, 0)
    # Reading must not create a registry entry (snapshot is side-effect free).
    assert "de:ad:be:ef:00:00" not in s._counts


def test_snapshot_returns_a_copy():
    s = PacketStats()
    s.record_rx(BSSID, "beacon")
    snap = s.snapshot(BSSID)
    snap["beacon"] = 999
    assert s.snapshot(BSSID)["beacon"] == 1


# ----- WlanInterface wiring --------------------------------------------------

class _FakeDriver:
    """Minimal driver with an async inject_frame (MagicMock isn't awaitable),
    so the send_raw TX-tally path can be exercised."""

    SUPPORTED_CHANNELS = [1, 6, 11]

    def __init__(self):
        self.injected = []

    def register_rx_callback(self, cb):
        self._cb = cb

    async def inject_frame(self, frame_bytes, use_no_ack=True):
        self.injected.append(frame_bytes)
        return True


def _mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def _deauth_frame(bssid=BSSID, client=CLIENT) -> bytes:
    # FC byte0 0xC0 = deauth mgmt; addr3 carries the BSSID.
    return (b"\xc0\x00\x00\x00" + _mac(client) + _mac(bssid) + _mac(bssid)
            + b"\x00\x00" + struct.pack("<H", 7))


def _data_frame(bssid=BSSID, client=CLIENT) -> bytes:
    # FC byte0 0x08 = data, to_ds set so addr1 is the BSSID.
    return (b"\x08\x01\x00\x00" + _mac(bssid) + _mac(client) + _mac(client)
            + b"\x00\x00")


def test_interface_rx_tallies_by_class():
    iface = WlanInterface(_FakeDriver(), "wlan0", "Fake")
    iface._on_frame_parsed(pkt({"type": "beacon", "bssid": BSSID, "rssi": -42}))
    iface._on_frame_parsed(pkt({"type": "eapol", "bssid": BSSID, "rssi": -42}))
    snap = iface.packet_stats.snapshot(BSSID)
    assert snap["beacon"] == 1
    assert snap["eapol"] == 1


def test_interface_rx_skips_broadcast_bssid():
    iface = WlanInterface(_FakeDriver(), "wlan0", "Fake")
    iface._on_frame_parsed(pkt({"type": "beacon", "bssid": "ff:ff:ff:ff:ff:ff", "rssi": -42}))
    assert iface.packet_stats.snapshot("ff:ff:ff:ff:ff:ff")["beacon"] == 0


async def test_send_raw_classifies_deauth():
    iface = WlanInterface(_FakeDriver(), "wlan0", "Fake")
    await iface.send_raw(_deauth_frame())
    snap = iface.packet_stats.snapshot(BSSID)
    assert snap["deauth"] == 1
    assert snap["inject"] == 0


async def test_send_raw_classifies_other_inject():
    iface = WlanInterface(_FakeDriver(), "wlan0", "Fake")
    await iface.send_raw(_data_frame())
    snap = iface.packet_stats.snapshot(BSSID)
    assert snap["inject"] == 1
    assert snap["deauth"] == 0


async def test_send_raw_tally_never_breaks_tx_on_garbage():
    iface = WlanInterface(_FakeDriver(), "wlan0", "Fake")
    # A too-short / unparseable frame must still inject (tally is best-effort).
    ok = await iface.send_raw(b"\x00\x01")
    assert ok is True
    assert iface.driver.injected == [b"\x00\x01"]
