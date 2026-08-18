"""Spec tests for the channel -> operating-class + band helpers (IEEE 802.11-2020 Annex E)."""
import pytest

from wifit3.dot11.chan import channel_operating_class, same_band


def test_channel_operating_class_2ghz_and_5ghz():
    assert channel_operating_class(1) == 81
    assert channel_operating_class(13) == 81
    assert channel_operating_class(14) == 82
    assert channel_operating_class(36) == 115
    assert channel_operating_class(64) == 118
    assert channel_operating_class(100) == 121
    assert channel_operating_class(157) == 125


def test_channel_operating_class_rejects_unmapped():
    with pytest.raises(ValueError):
        channel_operating_class(200)


def test_same_band():
    assert same_band(1, 11) is True             # both 2.4 GHz
    assert same_band(36, 157) is True           # both 5 GHz
    assert same_band(6, 36) is False            # 2.4 -> 5 GHz
    assert same_band(149, 11) is False          # 5 -> 2.4 GHz
    assert same_band(14, 36) is False           # ch 14 is 2.4 GHz
