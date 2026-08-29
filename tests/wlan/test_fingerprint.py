"""Client device-class fingerprinting: high confidence for a small hand-curated table of
single-purpose vendors (the OUI names the actual device class), low confidence for everyone else
in the generated full IEEE registry (the OUI only names the vendor, not which of their many
device types this is)."""
from wifit3.wlan.fingerprint import fingerprint


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
    assert fp is not None and fp.emoji == "🔵" and "Samsung" in fp.label and fp.confidence == "low"


def test_unknown_vendor_gets_the_generic_low_confidence_icon():
    """A registered-but-unfamiliar vendor (not one of the hand-picked icon overrides) still gets
    named, just with a generic tag instead of a bespoke icon."""
    fp = fingerprint("74:3a:f4:aa:bb:cc")     # Intel Corporate, per the generated registry
    assert fp is not None and fp.confidence == "low" and fp.emoji == "🏷️"
    assert "Intel" in fp.label


def test_oui_not_in_the_ieee_registry_returns_none():
    assert fingerprint("02:00:00:00:00:01") is None


def test_case_and_separator_insensitive():
    dashed = fingerprint("18-7F-88-AA-BB-CC")
    upper = fingerprint("18:7F:88:AA:BB:CC")
    lower = fingerprint("18:7f:88:aa:bb:cc")
    assert dashed == upper == lower is not None
