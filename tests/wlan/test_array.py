"""Tests for the card pool (wlan/array.py).

A lightweight FakeIface stands in for a connected WlanInterface (no hardware), exposing just the
surface the array touches. Covers card selection (channel + capability), the dedupe/ingest wiring
into the shared sink, STACK set_channel, SPREAD hop partitioning, and member-loss re-emit."""

from types import SimpleNamespace

from wifit3.chips.driver import FakeMacSupport
from wifit3.wlan.array import WlanArray

from tests.frames import pkt


class FakeIface:
    """The slice of WlanInterface the array uses, plus test hooks to emit RX / disconnect."""

    def __init__(self, name, channels, fake_mac=FakeMacSupport.SPOOFABLE, description="fake"):
        self.name = name
        self.description = description
        self._channels = list(channels)
        self.current_channel = channels[0]
        self.driver = SimpleNamespace(FAKE_MAC=fake_mac, SUPPORTED_CHANNELS=list(channels))
        self.on_tx = None
        self._rx = []
        self._disc = []
        self.tuned = []
        self.hop_calls = []
        self.stopped = 0
        self.closed = False

    @property
    def supported_channels(self):
        return self._channels

    def register_rx_callback(self, cb):
        self._rx.append(cb)

    def register_disconnect_callback(self, cb):
        self._disc.append(cb)

    async def set_channel(self, ch, scan=False):
        self.current_channel = ch
        self.tuned.append(ch)
        return True

    async def start_hopping(self, channels=None, interval=0.5):
        self.hop_calls.append(list(channels) if channels else None)

    async def stop_hopping(self):
        self.stopped += 1

    async def close(self):
        self.closed = True

    # ----- test drivers -----
    def emit(self, packet):
        for cb in list(self._rx):
            cb(packet)

    def emit_disconnect(self, exc):
        for cb in list(self._disc):
            cb(exc)


def _raw(seq=b"\x00\x00", a2=b"\xaa\xbb\xcc\xdd\xee\xff", retry=False):
    fc = b"\x80\x08" if retry else b"\x80\x00"
    return fc + b"\x00\x00" + b"\xff\xff\xff\xff\xff\xff" + a2 + a2 + seq + b"\x00" * 12


def _beacon(raw, bssid="aa:bb:cc:dd:ee:ff", rssi=-40):
    return pkt({"type": "beacon", "bssid": bssid, "source": bssid, "dest": "ff:ff:ff:ff:ff:ff",
               "rssi": rssi, "ssid": "AP", "channel": 6, "raw": raw})


def _pool(*ifaces):
    a = WlanArray()
    for i in ifaces:
        a.attach(i)
    return a


# ----- card selection --------------------------------------------------------

def test_select_iface_filters_by_channel_band():
    two4 = FakeIface("wlan0", [1, 6, 11])
    five = FakeIface("wlan1", [36, 44])
    a = _pool(two4, five)
    assert a.select_iface(6) is two4
    assert a.select_iface(44) is five
    assert a.select_iface(100) is None      # no card covers this channel


def test_select_iface_prefers_most_capable():
    weak = FakeIface("wlan0", [1, 6, 11], fake_mac=FakeMacSupport.NONE)
    strong = FakeIface("wlan1", [1, 6, 11], fake_mac=FakeMacSupport.SPOOFABLE)
    a = _pool(weak, strong)
    assert a.select_iface(6) is strong                       # SPOOFABLE outranks NONE
    assert a.select_iface(6, needs_spoof=True) is strong


def test_select_iface_needs_spoof_excludes_incapable():
    weak = FakeIface("wlan0", [1, 6, 11], fake_mac=FakeMacSupport.NONE)
    a = _pool(weak)
    assert a.select_iface(6) is weak                         # fine for passive/plain TX
    assert a.select_iface(6, needs_spoof=True) is None       # cannot HW-ACK a spoofed MAC


# ----- ingest / dedupe -------------------------------------------------------

def test_ingest_novel_populates_sink_with_card_signal():
    card = FakeIface("wlan0", [1, 6, 11])
    a = _pool(card)
    card.emit(_beacon(_raw(), rssi=-42))
    ap = a.get_access_points()[0]
    assert ap.bssid == "aa:bb:cc:dd:ee:ff" and ap.beacons == 1
    assert ap.signal_by_card == {"wlan0": -42}


def test_ingest_dedupes_same_air_across_cards():
    a_card = FakeIface("wlan0", [6])
    b_card = FakeIface("wlan1", [6])
    a = _pool(a_card, b_card)
    seen = []
    a.register_rx_callback(seen.append)
    raw = _raw()
    a_card.emit(_beacon(raw, rssi=-70))     # novel: A folds it in
    b_card.emit(_beacon(raw, rssi=-55))     # duplicate: only B's signal
    ap = a.get_access_points()[0]
    assert ap.beacons == 1                   # counted once
    assert ap.signal_by_card == {"wlan0": -70, "wlan1": -55}
    assert ap.signal == -55                  # strongest antenna
    assert len(seen) == 1                     # deduped stream fires on novel only


def test_ingest_drops_our_own_forged_frames():
    card = FakeIface("wlan0", [6])
    a = _pool(card)
    a.register_forged_mac("aa:bb:cc:dd:ee:ff")
    card.emit(_beacon(_raw(), rssi=-40))     # source == the forged BSSID
    assert a.get_access_points() == []        # never entered the picture


def test_ingest_drops_our_own_self_mac_transmissions():
    """A pooled RX card hears the TX card's WEP replays over the air; frames sourced from our own
    fake STA (a self-MAC) must be dropped so they don't inflate the IV rate."""
    card = FakeIface("wlan0", [6])
    a = _pool(card)
    mac = a.register_self_mac("aa:bb:cc:dd:ee:01", "11:22:33:44:55:66")
    card.emit(pkt({"type": "data", "to_ds": True, "bssid": "11:22:33:44:55:66",
                   "source": mac, "dest": "11:22:33:44:55:66", "rssi": -40}))
    assert a._dedupe.rx.get("wlan0", 0) == 0    # dropped before dedupe / picture


def test_array_of_one_processes_distinct_frames():
    card = FakeIface("wlan0", [6])
    a = _pool(card)
    card.emit(_beacon(_raw(seq=b"\x10\x00")))
    card.emit(_beacon(_raw(seq=b"\x20\x00")))   # different seq = distinct beacon
    assert a.get_access_points()[0].beacons == 2


# ----- channel policy --------------------------------------------------------

async def test_set_channel_stack_only_capable_members():
    two4 = FakeIface("wlan0", [1, 6, 11])
    five = FakeIface("wlan1", [36, 44])
    a = _pool(two4, five)
    ok = await a.set_channel(6)
    assert ok is True
    assert two4.tuned == [6] and five.tuned == []   # 2.4-only card stays put
    assert await a.set_channel(100) is False        # nobody can reach it


async def test_start_hopping_spread_partitions_channels():
    two4 = FakeIface("wlan0", [1, 6, 11])
    five = FakeIface("wlan1", [36, 44])
    a = _pool(two4, five)
    await a.start_hopping([1, 6, 11, 36, 44])
    assert two4.hop_calls == [[1, 6, 11]]
    assert five.hop_calls == [[36, 44]]


async def test_start_hopping_balances_across_equal_cards():
    x = FakeIface("wlan0", [1, 6, 11])
    y = FakeIface("wlan1", [1, 6, 11])
    a = _pool(x, y)
    await a.start_hopping([1, 6, 11])
    sub_x, sub_y = x.hop_calls[0], y.hop_calls[0]
    assert sorted(sub_x + sub_y) == [1, 6, 11]      # full coverage
    assert abs(len(sub_x) - len(sub_y)) <= 1        # balanced


# ----- member loss -----------------------------------------------------------

def test_member_lost_reemits_with_remaining_count():
    x = FakeIface("wlan0", [6])
    y = FakeIface("wlan1", [6])
    a = _pool(x, y)
    events = []
    a.register_disconnect_callback(lambda exc, remaining: events.append(remaining))
    x.emit_disconnect(RuntimeError("gone"))
    assert events == [1] and [m.name for m in a.members] == ["wlan1"]
    y.emit_disconnect(RuntimeError("gone"))
    assert events == [1, 0] and a.members == []


async def test_hot_unplug_closes_and_drops():
    x = FakeIface("wlan0", [6])
    a = _pool(x)
    seen = []
    a.register_disconnect_callback(lambda exc, remaining: seen.append(remaining))
    await a.hot_unplug(x)
    assert x.closed is True and a.members == [] and seen == [0]
