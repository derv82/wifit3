"""RF companion-chip identification for the standalone rt5572 driver.

Ported 1:1 from the rt2800usb parent's rt2800_init_eeprom decode (see
git ce8f3ef). The 148f:5572 card this driver claims reports RT5592 silicon,
which HARDCODES RF5592 — so for every admitted card the RF chip is fixed, not
EEPROM-derived. These tests pin that plus the inherited RT3572/RT5392 branches.
[SRC] data_dumps/rt2x00-source-v6.18/rt2800lib.c:11182-11235.
"""
from __future__ import annotations

from wifit3.chips.rt5572.constants import RT_RT3572, RT_RT5392, RT_RT5592
from wifit3.chips.rt5572.eeprom import (
    RF3022, RF3052, RF5392, RF5592, parse_eeprom, resolve_rf_chip,
)


def test_rf_type_decodes_nic_conf0_bits_11_8():
    """NIC_CONF0.RF_TYPE = FIELD16(0x0f00) — the RF-chip nibble a burned RT28xx/
    RT30xx EEPROM encodes (RF3052 = 0x9), independent of the antenna low byte."""
    buf = bytearray(0x200)
    # NIC_CONF0 word 0x1A → byte 0x34/0x35; RF3052 (0x9 high nibble) + 2T2R.
    buf[0x34] = 0x22   # txpath=2, rxpath=2
    buf[0x35] = 0x09   # RF_TYPE nibble = RF3052
    ee = parse_eeprom(bytes(buf))
    assert ee.rf_type == RF3052
    assert ee.txpath == 2 and ee.rxpath == 2


def test_chip_id_word_is_eeprom_word0():
    buf = bytearray(0x200)
    buf[0x00] = 0x92
    buf[0x01] = 0x53
    assert parse_eeprom(bytes(buf)).chip_id_word == 0x5392


def test_resolve_rf_chip_rt5592_hardcoded_rf5592_reference():
    """The reference PAU09 (148f:5572 → RT5592 silicon) hardcodes RF5592
    regardless of EEPROM contents — the whole point of the generalization is
    that this decode is fixed for every card the driver admits.
    [SRC] rt2800lib.c:11198-11199."""
    rf = resolve_rf_chip(RT_RT5592, parse_eeprom(bytes(0x200)))
    assert rf.rf_id == RF5592
    assert rf.name == "RF5592"
    assert rf.ported is True


def test_resolve_rf_chip_rt5592_ignores_burned_chip_id_word():
    """RT5592 must NOT read EEPROM_CHIP_ID — even a card whose word0 claims a
    different RF still resolves to the hardcoded RF5592."""
    buf = bytearray(0x200)
    buf[0x00] = 0x92   # word0 = 0x5392, a red herring for RT5592 silicon
    buf[0x01] = 0x53
    rf = resolve_rf_chip(RT_RT5592, parse_eeprom(bytes(buf)))
    assert rf.rf_id == RF5592
    assert rf.ported is True


def test_resolve_rf_chip_rt5392_reads_chip_id_word():
    """RT5390/RT5392 silicon take the RF id from EEPROM_CHIP_ID (word 0), not
    NIC_CONF0.RF_TYPE. [SRC] rt2800lib.c:11187-11191."""
    buf = bytearray(0x200)
    buf[0x00] = 0x92   # EEPROM_CHIP_ID = 0x5392 (RF5392)
    buf[0x01] = 0x53
    rf = resolve_rf_chip(RT_RT5392, parse_eeprom(bytes(buf)))
    assert rf.rf_id == RF5392
    assert rf.ported is True


def test_resolve_rf_chip_rt3572_burned_is_rf3052_ported():
    """Inherited branch: a burned RT3572 EEPROM reads RF3052 from RF_TYPE."""
    buf = bytearray(0x200)
    buf[0x34] = 0x22   # 2T2R
    buf[0x35] = 0x09   # RF3052
    rf = resolve_rf_chip(RT_RT3572, parse_eeprom(bytes(buf)))
    assert rf.rf_id == RF3052
    assert rf.name == "RF3052"
    assert rf.ported is True


def test_resolve_rf_chip_unburned_gives_zero_not_fail():
    """An unburned NIC_CONF0=0x0000 → RF_TYPE 0. The kernel would -ENODEV; we
    return rf_id=0 (ported=False, name '0x0000') and the caller runs the silicon
    default so an erased-EEPROM card still comes up."""
    rf = resolve_rf_chip(RT_RT3572, parse_eeprom(bytes(0x200)))
    assert rf.rf_id == 0
    assert rf.ported is False
    assert rf.name == "0x0000"


def test_resolve_rf_chip_unknown_rf_marked_unported():
    """A burned EEPROM claiming an RF the port has no tune path for (RF3022 =
    0x8) is flagged unported, not crashed — the driver still runs the silicon
    default and logs an 'untested variant' warning."""
    buf = bytearray(0x200)
    buf[0x34] = 0x22   # 2T2R
    buf[0x35] = 0x08   # RF3022 nibble — not a ported path
    rf = resolve_rf_chip(RT_RT3572, parse_eeprom(bytes(buf)))
    assert rf.rf_id == RF3022
    assert rf.name == "RF3022"
    assert rf.ported is False
