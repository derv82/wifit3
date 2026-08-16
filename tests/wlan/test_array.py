"""Tests for the card pool (wlan/array.py).

A lightweight FakeIface stands in for a connected WlanInterface (no hardware), exposing just the
surface the array touches. Covers card selection (channel + capability), the dedupe/ingest wiring
into the shared sink, STACK set_channel, SPREAD hop partitioning, and member-loss re-emit."""

import asyncio
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


def _beacon(raw, bssid="aa:bb:cc:dd:ee:ff", rssi=-40, channel=6):
    return pkt({"type": "beacon", "bssid": bssid, "source": bssid, "dest": "ff:ff:ff:ff:ff:ff",
               "rssi": rssi, "ssid": "AP", "channel": channel, "raw": raw})


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


def test_select_iface_returns_only_card_even_if_spoof_incapable():
    weak = FakeIface("wlan0", [1, 6, 11], fake_mac=FakeMacSupport.NONE)
    a = _pool(weak)
    assert a.select_iface(6) is weak


def test_prefer_pins_a_card_over_capability_rank():
    weak = FakeIface("wlan0", [1, 6, 11], fake_mac=FakeMacSupport.NONE)
    strong = FakeIface("wlan1", [1, 6, 11], fake_mac=FakeMacSupport.SPOOFABLE)
    a = _pool(weak, strong)
    assert a.select_iface(6) is strong                       # default: most capable
    a.prefer(weak)
    assert a.preferred is weak
    assert a.select_iface(6) is weak                         # pin wins even though it's weaker
    a.prefer(None)
    assert a.select_iface(6) is strong                       # cleared: back to rank


def test_prefer_falls_back_to_rank_when_pin_cant_reach_band():
    two4 = FakeIface("wlan0", [1, 6, 11])
    five = FakeIface("wlan1", [36, 44])
    a = _pool(two4, five)
    a.prefer(two4)
    assert a.select_iface(44) is five     # pinned 2.4 card can't reach ch44: fall back
    assert a.select_iface(6) is two4      # pin still honored for targets it can reach
    assert a.preferred is two4            # a fallback for one target doesn't clear the pin


def test_member_lost_clears_a_pinned_card():
    two4 = FakeIface("wlan0", [1, 6, 11])
    other = FakeIface("wlan1", [1, 6, 11], fake_mac=FakeMacSupport.NONE)
    a = _pool(two4, other)
    a.prefer(two4)
    two4.emit_disconnect(RuntimeError("unplugged"))
    assert a.preferred is None
    assert a.select_iface(6) is other     # only survivor


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


def test_ingest_drops_stray_beacon_but_keeps_the_real_ap():
    """A beacon on a BSSID's registered stray (decoy) channel is our twin looping back and is dropped;
    the real AP shares the BSSID on its own channel and still lands."""
    card = FakeIface("wlan0", [1])
    a = _pool(card)
    a.ignore_stray_beacons("aa:bb:cc:dd:ee:ff", 6)
    card.emit(_beacon(_raw(), rssi=-40, channel=6))              # twin on the decoy channel
    assert a.get_access_points() == []
    card.emit(_beacon(_raw(seq=b"\x10\x00"), rssi=-50, channel=1))   # real AP on its own channel
    assert list(a.access_points) == ["aa:bb:cc:dd:ee:ff"]


def test_ingest_drops_our_own_self_mac_transmissions():
    """A pooled RX card hears the TX card's WEP replay (ToDS) over the air; a frame whose transmitter
    (Addr2) is our own fake STA must be dropped so it doesn't inflate the IV rate."""
    card = FakeIface("wlan0", [6])
    a = _pool(card)
    a.register_self_mac("aa:bb:cc:dd:ee:01", "11:22:33:44:55:66")
    bssid, mac = b"\x11\x22\x33\x44\x55\x66", b"\xaa\xbb\xcc\xdd\xee\x01"
    raw = b"\x08\x01\x00\x00" + bssid + mac + bssid + b"\x00\x00" + b"\x00" * 12   # ToDS, Addr2 = us
    card.emit(pkt({"type": "data", "to_ds": True, "bssid": "11:22:33:44:55:66",
                   "source": "aa:bb:cc:dd:ee:01", "dest": "11:22:33:44:55:66",
                   "rssi": -40, "raw": raw}))
    assert a._dedupe.rx.get("wlan0", 0) == 0    # transmitter == self-MAC → dropped before dedupe


def test_ingest_counts_ap_echo_of_our_replay():
    """The AP's rebroadcast of our replayed WEP ARP is FromDS with our MAC in Addr3 (source) but the
    BSSID as transmitter (Addr2). It carries a FRESH IV, so it must NOT be dropped even though our MAC
    is a registered self-MAC -- the regression that zeroed the WEP IV rate (keying on source, not TA)."""
    card = FakeIface("wlan0", [6])
    a = _pool(card)
    a.register_self_mac("aa:bb:cc:dd:ee:01", "11:22:33:44:55:66")
    bssid, mac = b"\x11\x22\x33\x44\x55\x66", b"\xaa\xbb\xcc\xdd\xee\x01"
    bcast = b"\xff\xff\xff\xff\xff\xff"
    raw = b"\x08\x42\x00\x00" + bcast + bssid + mac + b"\x00\x00" + b"\x00" * 12   # FromDS, Addr2=BSSID
    card.emit(pkt({"type": "data", "from_ds": True, "bssid": "11:22:33:44:55:66",
                   "source": "aa:bb:cc:dd:ee:01", "dest": "ff:ff:ff:ff:ff:ff",
                   "rssi": -40, "raw": raw}))
    assert a._dedupe.rx.get("wlan0", 0) == 1    # transmitter == BSSID → survives the filter


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


async def test_partition_stacks_extra_cards_on_a_narrow_filter():
    x = FakeIface("wlan0", [1, 6, 11])
    y = FakeIface("wlan1", [1, 6, 11])
    a = _pool(x, y)
    await a.start_hopping([11])                      # 1 channel, 2 cards
    assert x.hop_calls[0] == [11]
    assert y.hop_calls[0] == [11]                    # the extra card stacks onto ch11, not stranded


async def test_partition_5ghz_first_keeps_cards_on_band():
    two4 = FakeIface("wlan0", [1, 6, 11])            # 2.4-only
    dual = FakeIface("wlan1", [1, 6, 11, 36, 44])    # dual-band
    a = _pool(two4, dual)
    await a.start_hopping([1, 6, 11, 36, 44])
    assert two4.hop_calls[0] == [1, 6, 11]
    assert dual.hop_calls[0] == [36, 44]             # dual took only 5 GHz; no 2.4 spillover onto it


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


# ----- re-partition on membership change (the array owns hopping) -------------

async def test_attach_while_hopping_repartitions():
    a = WlanArray()
    m1 = FakeIface("wlan0", [1, 6, 11])
    a.attach(m1)
    await a.start_hopping(interval=0.25)
    before = len(m1.hop_calls)
    a.attach(FakeIface("wlan1", [1, 6, 11]))     # a card joins mid-hop
    await asyncio.sleep(0.05)                     # let the array's internal re-hop task run
    assert a.members[1].hop_calls, "the new card starts hopping"
    assert len(m1.hop_calls) > before, "the existing card re-partitioned"


async def test_attach_when_not_hopping_is_inert():
    a = WlanArray()
    a.attach(FakeIface("wlan0", [1, 6, 11]))      # never started hopping
    m2 = FakeIface("wlan1", [1, 6, 11])
    a.attach(m2)
    await asyncio.sleep(0.05)
    assert m2.hop_calls == []


async def test_member_lost_while_hopping_repartitions_survivor():
    a = WlanArray()
    m1 = FakeIface("wlan0", [1, 6, 11])
    m2 = FakeIface("wlan1", [1, 6, 11])
    a.attach(m1)
    a.attach(m2)
    await a.start_hopping(interval=0.25)
    before = len(m1.hop_calls)
    m2.emit_disconnect(RuntimeError("unplug"))    # -> _member_lost -> re-partition survivor
    await asyncio.sleep(0.05)
    assert len(m1.hop_calls) > before


async def test_member_lost_closes_the_dead_card():
    a = WlanArray()
    x = FakeIface("wlan0", [6])
    a.attach(x)                                    # captures the loop for off-thread scheduling
    x.emit_disconnect(RuntimeError("unplug"))      # RX-reader death path
    await asyncio.sleep(0.05)                      # let the scheduled close run
    assert x.closed is True                        # driver closed -> its async tasks stopped


async def test_set_channel_logs_a_failing_card_and_tunes_the_rest():
    class _Boom(FakeIface):
        async def set_channel(self, ch, scan=False):
            raise RuntimeError("tune failed")

    good = FakeIface("wlan0", [1, 6, 11])
    bad = _Boom("wlan1", [1, 6, 11])
    a = _pool(good, bad)
    ok = await a.set_channel(6)
    assert ok is True and good.tuned == [6]        # one card failing doesn't abort the others
