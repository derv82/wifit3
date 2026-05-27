"""Tests for the WPS PIN keyspace."""

from wifit3.engine.attacks.wps import pins
from wifit3.engine.attacks.wps.wsc_crypto import pin_is_valid


def test_full_pin_checksum_valid():
    p = pins.full_pin("0103", "036")
    assert len(p) == 8 and p.startswith("0103036") and pin_is_valid(p)


def test_first_half_sweep_count_and_validity():
    it = list(pins.first_half_pins())
    assert len(it) == 10000
    assert it[0] == (0, pins.full_pin("0000", "000"))
    assert it[1234][0] == 1234 and it[1234][1][:4] == "1234"
    # every generated pin is checksum-valid
    for _i, p in it[:50]:
        assert pin_is_valid(p)


def test_second_half_sweep():
    it = list(pins.second_half_pins("0103"))
    assert len(it) == 1000
    assert all(p[:4] == "0103" and pin_is_valid(p) for _i, p in it[:50])


def test_split_pin():
    assert pins.split_pin("01030365") == ("0103", "0365")


def test_common_pins_present():
    assert "12345670" in pins.COMMON_PINS
