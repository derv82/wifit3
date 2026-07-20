"""Unit tests for ui/capture_log.py: short_sta. The per-field EAPOL markup moved out and is
covered via the EAPOL aggregator."""
from wifit3.ui.capture_log import short_sta


def test_short_sta_trims_to_last_three_octets():
    assert short_sta("11:22:33:44:55:66") == "…44:55:66"
    assert short_sta("") == "…"
