"""Client device-class fingerprinting: high confidence for a small hand-curated table of
single-purpose vendors (the OUI names the actual device class), low confidence for everyone else
in the generated full IEEE registry (the OUI only names the vendor, not which of their many
device types this is)."""
from wifit3.wlan.fingerprint import fingerprint
from wifit3.wlan.fingerprint_vendors import VENDOR_BY_OUI


def test_ring_oui_recognized_high_confidence():
    fp = fingerprint("18:7f:88:aa:bb:cc")
    assert fp is not None and fp.emoji == "🔔" and "Ring" in fp.label
    assert fp.confidence == "high"


def test_roku_oui_recognized():
    fp = fingerprint("b0:a7:37:11:22:33")
    assert fp is not None and "Roku" in fp.label and fp.confidence == "high"


def test_sonos_oui_recognized():
    fp = fingerprint("b8:e9:37:00:00:00")
    assert fp is not None and "Sonos" in fp.label and fp.confidence == "high"


def test_nintendo_oui_recognized():
    fp = fingerprint("7c:bb:8a:aa:bb:cc")
    assert fp is not None and "Nintendo" in fp.label and fp.confidence == "high"


def test_playstation_oui_recognized():
    fp = fingerprint("00:04:1f:aa:bb:cc")
    assert fp is not None and "PlayStation" in fp.label and fp.confidence == "high"


def test_nest_oui_recognized():
    fp = fingerprint("18:b4:30:aa:bb:cc")
    assert fp is not None and "Nest" in fp.label and fp.confidence == "high"


def test_irobot_oui_recognized():
    fp = fingerprint("4c:b9:ea:aa:bb:cc")
    assert fp is not None and "iRobot" in fp.label and fp.confidence == "high"


def test_tesla_oui_recognized():
    fp = fingerprint("4c:fc:aa:aa:bb:cc")
    assert fp is not None and "Tesla" in fp.label and fp.confidence == "high"


def test_tesla_28_bit_sub_block_recognized_precisely():
    """DC:44:27 is a 24-bit block IEEE further split into 16 organizations; only the :1x nibble
    is Tesla's. A device in a neighboring sub-range must resolve to its real owner, not Tesla."""
    fp = fingerprint("dc:44:27:15:22:33")            # Tesla's actual :1x sub-range
    assert fp is not None and "Tesla" in fp.label and fp.confidence == "high"

    neighbor = fingerprint("dc:44:27:05:22:33")       # :0x -- a different, unrelated vendor
    assert neighbor is not None and neighbor.confidence == "low"
    assert neighbor.label == "Suritel device"


def test_nintendo_and_playstation_have_different_icons():
    """Regression: both used to render the same generic controller emoji, making them
    indistinguishable at a glance."""
    nintendo = fingerprint("7c:bb:8a:aa:bb:cc")
    playstation = fingerprint("00:04:1f:aa:bb:cc")
    assert nintendo.emoji != playstation.emoji


def test_high_confidence_wins_over_the_generated_low_confidence_table():
    """Ring's OUI 18:7F:88 is also just "Ring" in the generated registry -- the hand-curated
    high-confidence entry must win, not fall through to a low-confidence vendor-only match."""
    fp = fingerprint("18:7f:88:aa:bb:cc")
    assert fp.confidence == "high" and fp.label == "Ring device"


def test_apple_matched_via_the_generated_table_at_low_confidence():
    """Apple's OUI blocks span phones/laptops/tablets alike: matched via the generated registry,
    but only as a vendor-only guess, with its recognizable icon by name override."""
    fp = fingerprint("dc:a4:ca:11:22:33")     # a real Apple OUI
    assert fp is not None and fp.emoji == "🍎" and fp.label == "Apple device"
    assert fp.confidence == "low"


def test_samsung_matched_via_the_generated_table():
    fp = fingerprint("00:00:f0:aa:bb:cc")
    assert fp is not None and fp.emoji == "🔵" and fp.confidence == "low"
    assert fp.label == "SAMSUNG Electronics device"     # brand casing preserved, not title-cased


def test_low_confidence_resolves_a_36_bit_sub_block_too():
    """Not just Tesla's 28-bit case -- a 36-bit (finer still) sub-allocation must also resolve
    to its actual owner, distinct from its immediate neighbor in the same 24-bit block."""
    a = fingerprint("00:1b:c5:00:0a:bb")
    b = fingerprint("00:1b:c5:00:1a:bb")
    assert a is not None and a.label == "Converging device"
    assert b is not None and b.label == "OpenRB.com, Direct SIA device"


def test_generated_table_never_names_an_ieee_registration_authority_block():
    """IEEE further subdivides some OUI-24 blocks into per-organization OUI-28/36 allocations;
    the OUI-24 record itself just says so, naming no real vendor -- misleading if surfaced."""
    assert "IEEE Registration Authority" not in VENDOR_BY_OUI.values()


def test_amazon_icon_override_matches_the_brand_not_a_substring():
    """A plain substring check also matched unrelated "...Amazonia" companies; a real
    mid-string match ("Blink by Amazon") must still work, ruling out .startswith() too."""
    real_amazon = fingerprint("3c:a0:70:aa:bb:cc")      # Blink by Amazon
    assert real_amazon is not None and real_amazon.emoji == "🛒"

    unrelated = fingerprint("60:c7:27:aa:bb:cc")        # Digiboard Eletronica da Amazonia Ltda
    assert unrelated is not None and unrelated.emoji == "🏷️"


def test_uncategorized_vendor_gets_the_generic_low_confidence_icon():
    """A registered vendor with neither an icon override nor a known device category still gets
    named, just with a fully generic tag."""
    fp = fingerprint("00:00:0b:aa:bb:cc")     # Matrix Corporation -- no override, no category
    assert fp is not None and fp.confidence == "low" and fp.emoji == "🏷️"
    assert "Matrix" in fp.label


def test_categorized_vendor_gets_the_category_icon_not_the_generic_tag():
    """No specific-vendor icon override for Intel, but it's a real, verified entry in the
    device-category dataset (Computer) -- that should beat the fully generic tag."""
    fp = fingerprint("74:3a:f4:aa:bb:cc")     # Intel Corporate
    assert fp is not None and fp.confidence == "low" and fp.emoji == "💻"
    assert "Intel" in fp.label


def test_icon_override_still_wins_over_a_device_category():
    """A specific-vendor icon (Amazon's shopping cart) must not be shadowed by a broader
    category match (Blink is also classified "Camera") -- the more specific tier wins."""
    fp = fingerprint("70:ad:43:aa:bb:cc")     # Blink by Amazon
    assert fp is not None and fp.emoji == "🛒"


def test_oui_not_in_the_ieee_registry_returns_none():
    assert fingerprint("02:00:00:00:00:01") is None


def test_case_and_separator_insensitive():
    dashed = fingerprint("18-7F-88-AA-BB-CC")
    upper = fingerprint("18:7F:88:AA:BB:CC")
    lower = fingerprint("18:7f:88:aa:bb:cc")
    assert dashed == upper == lower is not None
