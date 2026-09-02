from rich.cells import cell_len

from wifit3.wlan.fingerprint import _GENERIC_EMOJI, _RULES, fingerprint
from wifit3.wlan.fingerprint_vendors import VENDOR_BY_OUI

_GENERIC = "🔖"


def test_ring_oui_recognized():
    fp = fingerprint("18:7f:88:aa:bb:cc")
    assert fp is not None and fp.emoji == "🔔" and fp.label == "Ring device"


def test_roku_oui_recognized():
    fp = fingerprint("b0:a7:37:11:22:33")
    assert fp is not None and fp.emoji == "📺" and fp.label == "Roku"


def test_sonos_oui_recognized():
    fp = fingerprint("b8:e9:37:00:00:00")
    assert fp is not None and "Sonos" in fp.label


def test_nintendo_oui_recognized():
    fp = fingerprint("7c:bb:8a:aa:bb:cc")
    assert fp is not None and "Nintendo" in fp.label


def test_playstation_oui_recognized():
    fp = fingerprint("00:04:1f:aa:bb:cc")
    assert fp is not None and "PlayStation" in fp.label


def test_nest_oui_recognized():
    fp = fingerprint("18:b4:30:aa:bb:cc")
    assert fp is not None and "Nest" in fp.label


def test_irobot_oui_recognized():
    fp = fingerprint("4c:b9:ea:aa:bb:cc")
    assert fp is not None and "iRobot" in fp.label


def test_tesla_oui_recognized():
    fp = fingerprint("4c:fc:aa:aa:bb:cc")
    assert fp is not None and "Tesla" in fp.label


def test_tesla_28_bit_sub_block_recognized_precisely():
    fp = fingerprint("dc:44:27:15:22:33")            # Tesla's actual :1x sub-range
    assert fp is not None and "Tesla" in fp.label

    neighbor = fingerprint("dc:44:27:05:22:33")       # :0x -- a different, unrelated vendor
    assert neighbor is not None and neighbor.emoji == _GENERIC
    assert "Suritel" in neighbor.label


def test_hard_coded_rule_wins_over_the_vendor_table():
    fp = fingerprint("18:7f:88:aa:bb:cc")
    assert fp.emoji == "🔔" and "Ring" in fp.label


def test_apple_matched_by_name_regex():
    fp = fingerprint("dc:a4:ca:11:22:33")     # a real Apple OUI
    assert fp is not None and fp.emoji == "🍎" and "Apple" in fp.label


def test_samsung_matched_by_name_regex():
    fp = fingerprint("00:00:f0:aa:bb:cc")
    assert fp is not None and fp.emoji == "🔵"
    assert "SAMSUNG Electronics".lower() in fp.label.lower()


def test_low_confidence_resolves_a_36_bit_sub_block():
    """A 36-bit sub-allocation resolves to its actual owner, distinct from its 24-bit neighbor."""
    a = fingerprint("00:1b:c5:00:0a:bb")
    b = fingerprint("00:1b:c5:00:1a:bb")
    assert a is not None and "Converging" in a.label
    assert b is not None and "OpenRB" in b.label


def test_generated_table_never_names_an_ieee_registration_authority_block():
    assert "IEEE Registration Authority" not in VENDOR_BY_OUI.values()
    assert "Private" not in VENDOR_BY_OUI.values()


def test_amazon_regex_matches_the_brand_not_a_substring():
    """The word-boundary regex matches "Blink by Amazon" but not unrelated "...Amazonia" companies."""
    real_amazon = fingerprint("3c:a0:70:aa:bb:cc")      # Blink by Amazon
    unrelated = fingerprint("60:c7:27:aa:bb:cc")        # Digiboard Eletronica da Amazonia Ltda
    assert real_amazon.emoji != unrelated.emoji


def test_uncategorized_vendor_gets_the_generic_tag():
    fp = fingerprint("00:00:0b:aa:bb:cc")     # Matrix Corporation
    assert fp is not None and fp.emoji == _GENERIC and "Matrix" in fp.label


def test_intel_falls_through_to_generic():
    """Intel was a category-table hit before; with that data removed it is now just a named vendor."""
    fp = fingerprint("74:3a:f4:aa:bb:cc")     # Intel Corporate
    assert fp is not None and fp.emoji == _GENERIC and "Intel" in fp.label


def test_all_rule_emojis_are_fixed_double_width():
    for emoji in [r.emoji for r in _RULES] + [_GENERIC_EMOJI]:
        assert "️" not in emoji, f"{emoji!r} carries a variation selector; renders 1 cell"
        assert cell_len(emoji) == 2, f"{emoji!r} is not 2 cells wide"


def test_oui_not_in_the_ieee_registry_returns_none():
    assert fingerprint("02:00:00:00:00:01") is None


def test_case_and_separator_insensitive():
    dashed = fingerprint("18-7F-88-AA-BB-CC")
    upper = fingerprint("18:7F:88:AA:BB:CC")
    lower = fingerprint("18:7f:88:aa:bb:cc")
    assert dashed == upper == lower is not None
