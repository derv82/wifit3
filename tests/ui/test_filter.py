"""ScanFilter predicate (text + encryption) and FilterBar message wiring."""
import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Input, Select

from wifit3.models import AccessPoint
from wifit3.ui.encryption_format import EncryptionType
from wifit3.ui.screens.filter import EncryptionFilter, FilterBar, ScanFilter, text_matches


def _ap(**kw) -> AccessPoint:
    return AccessPoint(bssid="AA:BB:CC:DD:EE:FF", **kw)


# ---- text_matches ----------------------------------------------------------

def test_empty_query_matches_everything():
    assert text_matches("", "AA:BB:CC:DD:EE:FF", None)
    assert text_matches("   ", "AA:BB:CC:DD:EE:FF", "anything")


def test_substring_is_case_insensitive_over_ssid_and_bssid():
    assert text_matches("net", "AA:BB:CC:DD:EE:FF", "NetGear")
    assert text_matches("bb:cc", "AA:BB:CC:DD:EE:FF", None)


def test_space_separated_tokens_are_anded():
    assert text_matches("net 5g", "AA:BB:CC:DD:EE:FF", "Net Home 5G")
    assert not text_matches("net xyz", "AA:BB:CC:DD:EE:FF", "Net Home 5G")


def test_single_char_token_matches_ssid_only():
    assert text_matches("f", "AA:BB:CC:DD:EE:FF", "wolf")
    # 'f' is all over the BSSID, but a lone char never matches the BSSID.
    assert not text_matches("f", "AA:BB:CC:DD:EE:FF", "xyz")


def test_not_fuzzy_subsequence():
    assert not text_matches("ntgr", "AA:BB:CC:DD:EE:FF", "netgear")


# ---- EncryptionType.from_ap (shared by the ENCRYPT column and the filter) --

@pytest.mark.parametrize("kw, expected", [
    (dict(encryption="WEP"), EncryptionType.WEP),
    (dict(akms=["PSK"]), EncryptionType.WPA2),
    (dict(wpa3=True), EncryptionType.WPA3),
    (dict(wpa3=True, transition_mode=True), EncryptionType.WPA3_TRANSITION),
    (dict(akms=["OWE"]), EncryptionType.OWE),
    (dict(encryption="OPEN"), EncryptionType.OPEN),
    (dict(encryption="WPA"), EncryptionType.WPA1),
    (dict(), EncryptionType.OPEN),  # default encryption "Unknown" -> OPEN
])
def test_encryption_type_from_ap(kw, expected):
    assert EncryptionType.from_ap(_ap(**kw)) is expected


# ---- EncryptionFilter.matches ----------------------------------------------

def test_all_matches_every_ap():
    for kw in (dict(), dict(encryption="WEP"), dict(wpa3=True), dict(akms=["PSK"])):
        assert EncryptionFilter.ALL.matches(_ap(**kw))


def test_encryption_filters_select_their_bucket():
    assert EncryptionFilter.OPEN.matches(_ap(encryption="OPEN"))
    assert EncryptionFilter.WEP.matches(_ap(encryption="WEP"))
    assert EncryptionFilter.WPA3.matches(_ap(wpa3=True))
    assert EncryptionFilter.WPA3_TRANSITION.matches(_ap(wpa3=True, transition_mode=True))
    assert not EncryptionFilter.WEP.matches(_ap(akms=["PSK"]))


def test_wpa_filter_merges_wpa1_and_wpa2():
    assert EncryptionFilter.WPA.matches(_ap(encryption="WPA"))   # legacy WPA1
    assert EncryptionFilter.WPA.matches(_ap(akms=["PSK"]))       # RSN WPA2
    assert not EncryptionFilter.WPA.matches(_ap(wpa3=True))
    assert not EncryptionFilter.WPA.matches(_ap(encryption="OPEN"))


def test_transition_ap_is_not_pure_wpa3():
    ap = _ap(wpa3=True, transition_mode=True)
    assert not EncryptionFilter.WPA3.matches(ap)
    assert not EncryptionFilter.WPA.matches(ap)


# ---- ScanFilter (text AND encryption) --------------------------------------

def test_default_scan_filter_matches_all():
    assert ScanFilter().matches(_ap(ssid="whatever", encryption="WEP"))


def test_scan_filter_ands_text_and_encryption():
    ap = _ap(ssid="netgear", akms=["PSK"])
    assert ScanFilter(text="net", encryption=EncryptionFilter.WPA).matches(ap)
    assert not ScanFilter(text="zzz", encryption=EncryptionFilter.WPA).matches(ap)
    assert not ScanFilter(text="net", encryption=EncryptionFilter.WPA3).matches(ap)


def test_hidden_ap_found_by_guessed_ssid():
    hidden = _ap(ssid=None)
    assert not ScanFilter(text="castle").matches(hidden)
    assert ScanFilter(text="castle").matches(hidden, ssid="Castle Crasher")


# ---- FilterBar message wiring ----------------------------------------------

class _Host(App):
    def __init__(self, supported):
        super().__init__()
        self._supported = supported
        self.events = []

    def compose(self) -> ComposeResult:
        yield FilterBar(self._supported)

    def on_filter_bar_scan_filter_changed(self, m: FilterBar.ScanFilterChanged):
        self.events.append(("scan", m.scan_filter))

    def on_filter_bar_edit_channels(self, m: FilterBar.EditChannels):
        self.events.append(("channels", None))


async def test_typing_emits_scan_filter():
    app = _Host([1, 6, 11, 36, 40])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.events.clear()
        app.query_one("#filter-text", Input).value = "net"
        await pilot.pause()
        scan = [e for e in app.events if e[0] == "scan"]
        assert scan and scan[-1][1].text == "net"


async def test_encryption_select_emits_scan_filter():
    app = _Host([1, 6, 11])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.events.clear()
        app.query_one("#filter-encryption", Select).value = EncryptionFilter.WPA
        await pilot.pause()
        scan = [e for e in app.events if e[0] == "scan"]
        assert scan and scan[-1][1].encryption is EncryptionFilter.WPA


async def test_channels_button_requests_dialog():
    app = _Host([1, 6, 11, 36, 40])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.events.clear()
        app.query_one("#filter-channels", Button).press()
        await pilot.pause()
        assert any(e[0] == "channels" for e in app.events)


async def test_channels_button_summarizes_active_set():
    app = _Host([1, 6, 11, 36, 40])
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one(FilterBar)
        button = app.query_one("#filter-channels", Button)
        assert "2.4G + 5G" in str(button.label)   # None -> all, by default
        bar.set_channels([6, 11])
        await pilot.pause()
        assert "6, 11" in str(button.label)
        bar.set_channels([1, 6, 11, 36, 40])      # == supported -> all
        await pilot.pause()
        assert "2.4G + 5G" in str(button.label)


def test_channels_label_drops_band_prefix_for_partial_sets():
    bar = FilterBar(list(range(1, 12)) + [36, 40, 44, 48])
    assert bar._channels_text(None).endswith("2.4G + 5G")            # all
    assert bar._channels_text([1, 3, 4, 6]).endswith("1, 3-4, 6")    # partial 2.4, no band name
    assert bar._channels_text(list(range(1, 12)) + [44]).endswith("2.4G + 44")
    assert bar._channels_text([36, 40, 44, 48]).endswith("5G")       # whole 5 GHz band


class _FocusHost(App):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield FilterBar([1, 6, 11])
            table = DataTable(id="ap-table", cursor_type="row")
            table.add_column("BSSID")
            yield table


async def test_focus_returns_to_table_after_interactions():
    app = _FocusHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#ap-table", DataTable)

        await pilot.click("#filter-encryption")
        await pilot.press("down", "enter")   # pick an option; the Select must not keep focus
        await pilot.pause()
        assert app.focused is table

        for key in ("enter", "escape"):
            app.query_one("#filter-text", Input).focus()
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert app.focused is table
