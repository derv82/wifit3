"""ScanFilter predicate: text, encryption, power and silenced matching."""
import pytest

from wifit3.models import AccessPoint
from wifit3.persist.config import Config
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


def test_text_filter_matches_the_nickname_and_the_ssid():
    Config.set_nickname("aa:bb:cc:dd:ee:ff", "Kitchen")
    assert ScanFilter(text="kitch").matches(_ap(ssid="HomeNet"))
    assert ScanFilter(text="kitchen homenet").matches(_ap(ssid="HomeNet"))
    assert not ScanFilter(text="kitchenhomenet").matches(_ap(ssid="HomeNet"))


# ---- ScanFilter min_signal -------------------------------------------------

def test_min_signal_drops_aps_at_or_below_the_threshold():
    scan_filter = ScanFilter(min_signal=-80)
    assert scan_filter.matches(_ap(signal_by_card={"card": -79}))
    assert not scan_filter.matches(_ap(signal_by_card={"card": -80}))
    assert not scan_filter.matches(_ap(signal_by_card={"card": -81}))


def test_min_signal_uses_the_best_card():
    ap = _ap(signal_by_card={"near": -60, "far": -90})
    assert ScanFilter(min_signal=-70).matches(ap)


# ---- ScanFilter hide_silenced ----------------------------------------------

def test_hide_silenced_drops_only_silenced_aps():
    Config.silenced_bssids = ["aa:bb:cc:dd:ee:ff"]
    ap = _ap(ssid="neighbour")
    assert not ScanFilter(hide_silenced=True).matches(ap)
    assert ScanFilter().matches(ap)          # off by default: Silence only hides the row's badge


def test_hide_silenced_keeps_unsilenced_aps():
    Config.silenced_bssids = ["11:22:33:44:55:66"]
    assert ScanFilter(hide_silenced=True).matches(_ap(ssid="neighbour"))


def test_hide_silenced_combines_with_text_and_encryption():
    Config.silenced_bssids = ["aa:bb:cc:dd:ee:ff"]
    assert not ScanFilter(text="net", hide_silenced=True).matches(_ap(ssid="netgear", akms=["PSK"]))
    assert ScanFilter(text="net", encryption=EncryptionFilter.WPA, hide_silenced=True).matches(
        AccessPoint(bssid="11:22:33:44:55:66", ssid="netgear", akms=["PSK"]))


def test_hide_silenced_matches_config_case_insensitively():
    Config.silenced_bssids = ["aa:bb:cc:dd:ee:ff"]
    assert not ScanFilter(hide_silenced=True).matches(AccessPoint(bssid="AA:BB:CC:DD:EE:FF"))


# ---- FilterBar message wiring ----------------------------------------------


async def test_hide_silenced_follows_a_later_silence():
    """The scanner re-runs the predicate each tick, so silencing from Focus drops the row."""
    ap = _ap(ssid="neighbour")
    scan_filter = ScanFilter(hide_silenced=True)
    assert scan_filter.matches(ap)
    Config.silenced_bssids = ["aa:bb:cc:dd:ee:ff"]
    assert not scan_filter.matches(ap)


def test_channels_label_drops_band_prefix_for_partial_sets():
    bar = FilterBar(list(range(1, 12)) + [36, 40, 44, 48])
    assert bar._channels_text(None).endswith("2.4G + 5G")            # all
    assert bar._channels_text([1, 3, 4, 6]).endswith("1, 3-4, 6")    # partial 2.4, no band name
    assert bar._channels_text(list(range(1, 12)) + [44]).endswith("2.4G + 44")
    assert bar._channels_text([36, 40, 44, 48]).endswith("5G")       # whole 5 GHz band

