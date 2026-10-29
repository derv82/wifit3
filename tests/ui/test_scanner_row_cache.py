"""ScannerView._row_signature must name every input to an APRow.

An unchanged AP reuses its previous row object, which is only sound if the signature
covers everything the row reads. Most of that rides on ap.last_seen — wlan/sink.py
rewrites it after every IE-derived field — but several paths mutate an AP without it
(cross-card RSSI, a decloak from a client's probe, a WPS M1, WEP IV counting), and the
badges read vault and config state that no frame touches at all.

So: mutate one thing, take the cached row, then drop the cache and rebuild. The two must
agree. A signature missing an input returns the stale row and fails here.
"""
import time

import pytest
import pytest_asyncio
from textual.app import App

from wifit3.models import AccessPoint, Client, Handshake, IdKey, IdSource
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.screens.scanner import ScannerView

pytestmark = pytest.mark.asyncio(loop_scope="module")


class _Array:
    def __init__(self):
        self.access_points = {}
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = [1, 6, 11]
        self.members = []

    def get_access_points(self, include_eviltwin=True):
        return list(self.access_points.values())

    async def start_hopping(self, channels=None, interval=0.25):
        pass

    async def stop_hopping(self):
        pass


class _Host(App):
    def __init__(self, array):
        super().__init__()
        self.array = array
        self.pbc_enabled = False
        self.vault = Vault()

    def persist_config(self) -> None:
        pass

    def on_mount(self) -> None:
        self.push_screen(ScannerView())


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def _app():
    app = _Host(_Array())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0)
        yield app, pilot


@pytest_asyncio.fixture(loop_scope="module")
async def scanner(_app):
    app, pilot = _app
    app.array.access_points.clear()
    app.array.clients.clear()
    app.vault._index.clear()
    app.vault.notify_changed(None)
    Config.silenced_bssids = []
    screen = app.screen
    screen.ap_cache.clear()
    screen._row_cache.clear()
    screen._prev_beacons.clear()
    screen._beacon_flash_until.clear()
    yield app, screen


def _ap(bssid="02:00:00:00:00:01") -> AccessPoint:
    ap = AccessPoint(bssid=bssid, ssid="Net", channel=6, encryption="WPA2", akms=["PSK"])
    ap.signal_by_card = {"card0": -55}
    ap.beacons = 40
    return ap


def _assert_cache_is_honest(screen, ap, clients=0, sibling=None):
    """The cached row must equal one built from scratch for the same state."""
    now = time.time()
    cached = screen._row_for(ap, now, clients, sibling)
    screen._row_cache.clear()
    screen._prev_beacons.pop(ap.bssid, None)
    fresh = screen._row_for(ap, now, clients, sibling)
    assert cached == fresh, "row cache returned a stale row"
    return fresh


async def test_an_untouched_ap_reuses_its_row_object(scanner):
    app, screen = scanner
    ap = _ap()
    now = time.time()
    first = screen._row_for(ap, now, 0, None)
    assert screen._row_for(ap, now, 0, None) is first


async def test_an_ap_holding_a_handshake_is_never_cached(scanner):
    app, screen = scanner
    ap = _ap()
    ap.handshakes["11:22:33:44:55:66"] = Handshake(
        bssid=ap.bssid, client_mac="11:22:33:44:55:66")
    now = time.time()
    assert screen._row_for(ap, now, 0, None) is not screen._row_for(ap, now, 0, None)


# Each case mutates one thing that a real code path can change, then checks the cached
# row still matches a freshly built one.


async def test_beacon_arrival_is_seen(scanner):
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    ap.beacons += 1
    ap.last_seen = time.time()
    assert _assert_cache_is_honest(screen, ap).beacons == 41


async def test_cross_card_rssi_without_last_seen_is_seen(scanner):
    # WlanSink.record_signal updates signal_by_card and nothing else.
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    ap.signal_by_card["card1"] = -20
    assert _assert_cache_is_honest(screen, ap).signal == -20


async def test_decloak_without_last_seen_is_seen(scanner):
    # _decloak runs off a client's probe request via _track_client.
    app, screen = scanner
    ap = _ap()
    ap.ssid = None
    _assert_cache_is_honest(screen, ap)
    ap.ssid = "Revealed"
    assert _assert_cache_is_honest(screen, ap).ssid == "Revealed"


async def test_wps_m1_flag_without_last_seen_is_seen(scanner):
    # _on_wps_m1_frame sets ap.wps off an EAPOL frame.
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    ap.wps = True
    assert _assert_cache_is_honest(screen, ap).wps is True


async def test_identity_changing_on_its_own_is_seen(scanner):
    # A WPS M1 writes identity evidence; ap.wps may already be True, so identity has
    # to carry itself in the signature rather than ride on the flag.
    app, screen = scanner
    ap = _ap()
    ap.wps = True
    _assert_cache_is_honest(screen, ap)
    ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "MikroTik")
    assert _assert_cache_is_honest(screen, ap).identity == "MikroTik"


async def test_wps_lock_flip_is_seen(scanner):
    app, screen = scanner
    ap = _ap()
    ap.wps = True
    _assert_cache_is_honest(screen, ap)
    ap.wps_locked = True
    assert _assert_cache_is_honest(screen, ap).wps_locked is True


async def test_wep_iv_counting_without_last_seen_is_seen(scanner):
    # WepStats.unique_ivs grows in place on every WEP data frame.
    from wifit3.models import WepStats
    app, screen = scanner
    ap = _ap()
    ap.encryption = "WEP"
    ap.akms = []
    ap.wep = WepStats()
    _assert_cache_is_honest(screen, ap)
    ap.wep.unique_ivs += 500
    assert _assert_cache_is_honest(screen, ap).encryption.wep_ivs == 500


async def test_encryption_upgrade_is_seen(scanner):
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    ap.last_seen = time.time()          # the sink rewrites this after every IE field
    ap.wpa3 = True
    ap.akms = ["SAE"]
    _assert_cache_is_honest(screen, ap)


async def test_channel_move_is_seen(scanner):
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    ap.last_seen = time.time()
    ap.channel = 11
    assert _assert_cache_is_honest(screen, ap).channel == 11


async def test_a_saved_capture_lights_the_badge(scanner):
    # The badges read the vault, which no frame touches — the AP's Vault.revision is their key.
    from wifit3.models import CaptureType, PersistedCapture
    app, screen = scanner
    ap = _ap()
    assert _assert_cache_is_honest(screen, ap).has_pmkid is False

    app.vault._index[ap.bssid] = [PersistedCapture(
        type=CaptureType.PMKID, timestamp=0, path="x", bssid=ap.bssid)]
    app.vault.notify_changed(ap.bssid)
    assert _assert_cache_is_honest(screen, ap).has_pmkid is True


async def test_another_aps_vault_change_reuses_the_row_object(scanner):
    app, screen = scanner
    ap = _ap()
    now = time.time()
    first = screen._row_for(ap, now, 0, None)
    app.vault.notify_changed("02:00:00:00:00:02")
    assert screen._row_for(ap, now, 0, None) is first
    app.vault.notify_changed(None)
    assert screen._row_for(ap, now, 0, None) is not first


async def test_a_recovered_wep_key_lights_the_badge(scanner):
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    ap.wep_key = b"\x01\x02\x03\x04\x05"
    assert _assert_cache_is_honest(screen, ap).has_wep_key is True


async def test_a_wps_psk_lights_the_badge(scanner):
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    ap.wps_pbc_psk = "hunter2hunter2"
    assert _assert_cache_is_honest(screen, ap).has_wps_psk is True


async def test_silencing_an_ap_is_seen(scanner):
    app, screen = scanner
    ap = _ap()
    _assert_cache_is_honest(screen, ap)
    Config.silenced_bssids = [ap.bssid.lower()]
    assert _assert_cache_is_honest(screen, ap).silenced is True


async def test_client_count_and_staleness_are_seen(scanner):
    app, screen = scanner
    ap = _ap()
    assert _assert_cache_is_honest(screen, ap, clients=0).clients == 0
    assert _assert_cache_is_honest(screen, ap, clients=3).clients == 3

    ap.last_seen = time.time() - 60
    assert _assert_cache_is_honest(screen, ap).is_stale is True


async def test_a_sibling_gaining_a_name_is_seen(scanner):
    app, screen = scanner
    ap = _ap()
    ap.ssid = None
    assert _assert_cache_is_honest(screen, ap, sibling=None).sibling_ssid is None
    assert _assert_cache_is_honest(screen, ap, sibling="Guest").sibling_ssid == "Guest"


async def test_the_live_tick_still_tracks_a_mutating_ap(scanner):
    """End to end: refresh_table over a changing AP keeps the table in step."""
    app, screen = scanner
    ap = _ap()
    app.array.access_points[ap.bssid] = ap
    client = Client(mac="11:22:33:44:55:66", bssid=ap.bssid)

    screen.refresh_table()
    table = screen.query_one("#ap-table")
    assert table.row_data(ap.bssid).clients == 0

    app.array.clients[client.mac] = client
    screen.refresh_table()
    assert table.row_data(ap.bssid).clients == 1

    ap.signal_by_card["card0"] = -31
    screen.refresh_table()
    assert table.row_data(ap.bssid).signal == -31
