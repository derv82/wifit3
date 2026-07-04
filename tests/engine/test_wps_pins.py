"""Tests for the WPS PIN keyspace."""

from wifit3.engine.attacks.wps import pins
from wifit3.engine.attacks.wps.wsc_crypto import pin_is_valid


def test_full_pin_checksum_valid():
    p = pins.full_pin("0103", "036")
    assert len(p) == 8 and p.startswith("0103036") and pin_is_valid(p)


def test_split_pin():
    assert pins.split_pin("01030365") == ("0103", "0365")


def test_common_pins_present():
    assert "12345670" in pins.COMMON_PINS
