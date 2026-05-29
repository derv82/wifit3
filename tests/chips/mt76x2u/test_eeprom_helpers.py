"""mt76x2u EEPROM math helpers + `read_block` slicing.

Covers the parts that bit us in production:
  - `read_block` with non-4-aligned offsets (the power-info tables sit at
    0x56 / 0x5C / 0x62 / 0x80 — the bug that crashed connect() in the
    field).
  - The signed-magnitude unpackers used to decode EEPROM bytes.
  - The cal-channel-group classifier for 5 GHz high-gain table selection.
"""
from unittest.mock import MagicMock

import pytest

from wifit3.chips.mt76x2u import eeprom


def _word_le(b: bytes, offset: int = 0) -> int:
    return int.from_bytes(b[offset : offset + 4], "little")


class FakeTransport:
    """Records read32 calls + serves pre-stuffed EEPROM words."""

    def __init__(self, contents: bytes):
        self._buf = bytearray(contents)
        self.read_calls: list[int] = []

    def read32(self, addr: int) -> int:
        self.read_calls.append(addr)
        # MT_VEND_TYPE_EEPROM marker comes through OR'd into addr; mask it.
        base = addr & 0x0FFFFFFF
        return _word_le(bytes(self._buf), base)


# ---------------------------------------------------------------------------
# read_block — the bug from the field
# ---------------------------------------------------------------------------

def test_read_block_aligned_offset():
    """Aligned offset + aligned length reads each word exactly once."""
    t = FakeTransport(bytes(range(64)))
    got = eeprom.read_block(t, 0x00, 8)
    assert got == bytes(range(8))
    assert t.read_calls == [
        eeprom.MT_VEND_TYPE_EEPROM | 0x00,
        eeprom.MT_VEND_TYPE_EEPROM | 0x04,
    ]


def test_read_block_unaligned_offset_no_crash():
    """The bug: kernel-faithful power-info reads at 0x56 / 0x5C must work.
    Previously raised ValueError; now slices out of the surrounding words."""
    t = FakeTransport(bytes(range(0x80)))
    got = eeprom.read_block(t, 0x56, 6)
    # offsets 0x56..0x5B = bytes 0x56..0x5B inclusive
    assert got == bytes(range(0x56, 0x5C))


def test_read_block_unaligned_offset_and_length():
    """Span that touches 3 words — start mid-word, end mid-word."""
    t = FakeTransport(bytes(range(64)))
    got = eeprom.read_block(t, 0x05, 6)   # bytes 5..10 inclusive (touches 3 words)
    assert got == bytes(range(5, 11))


def test_read_block_zero_length():
    t = FakeTransport(b"")
    assert eeprom.read_block(t, 0x100, 0) == b""


def test_read_block_single_byte_unaligned():
    t = FakeTransport(bytes(range(64)))
    got = eeprom.read_block(t, 0x07, 1)
    assert got == bytes([7])


def test_read_block_5g_chain_offsets():
    """The other field-bug magnet: 5 GHz chain offsets 0x62 + group*5 land
    at 0x62, 0x67, 0x6C, 0x71, 0x76, 0x7B — most are NOT 4-aligned."""
    t = FakeTransport(bytes(range(0x100)))
    for group in range(6):
        offset = 0x62 + group * 5
        got = eeprom.read_block(t, offset, 5)
        assert got == bytes(range(offset, offset + 5)), (
            f"group {group} @ 0x{offset:02x}: {got!r}"
        )


# ---------------------------------------------------------------------------
# Signed-magnitude unpackers
# ---------------------------------------------------------------------------

def test_sign_extend_4bit():
    # bit 3 = 1 → positive, value = low 3 bits
    assert eeprom._sign_extend_4bit(0b1000) == 0
    assert eeprom._sign_extend_4bit(0b1011) == 3
    # bit 3 = 0 → negative
    assert eeprom._sign_extend_4bit(0b0011) == -3
    assert eeprom._sign_extend_4bit(0b0000) == 0


def test_sign_extend_7bit():
    # size=7 → bit 6 is the sign
    assert eeprom._sign_extend(0b1000000, 7) == 0
    assert eeprom._sign_extend(0b1000001, 7) == 1
    assert eeprom._sign_extend(0b0000001, 7) == -1


def test_sign_extend_optional():
    # Bit `size` (the one ABOVE the sign bit) gates the value.
    # For size=7, bit 7 (0x80) enables.
    assert eeprom._sign_extend_optional(0x00, 7) == 0   # disabled → 0
    assert eeprom._sign_extend_optional(0xC1, 7) == 1   # 0x80 | (sign=1) | mag=1
    assert eeprom._sign_extend_optional(0x81, 7) == -1  # enabled + sign=0 + mag=1


def test_rate_power_val_field_invalid():
    # 0 and 0xFF return 0 (mt76x02_field_valid → false).
    assert eeprom._rate_power_val(0x00) == 0
    assert eeprom._rate_power_val(0xFF) == 0


def test_rate_power_val_valid():
    # Same encoding as sign_extend_optional(7)
    assert eeprom._rate_power_val(0xC2) == 2     # enabled + positive + mag=2
    assert eeprom._rate_power_val(0x82) == -2    # enabled + negative + mag=2


def test_field_valid():
    assert not eeprom._field_valid(0x00)
    assert not eeprom._field_valid(0xFF)
    assert eeprom._field_valid(0x01)
    assert eeprom._field_valid(0x80)


# ---------------------------------------------------------------------------
# 5 GHz cal channel groups — the table from eeprom.c:210-224
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("channel,group", [
    (190, 0),   # JAPAN (184..196)
    (36, 1),    # UNII-1 (<=48)
    (48, 1),
    (52, 2),    # UNII-2 (<=64)
    (64, 2),
    (100, 3),   # UNII-2E_1 (<=114)
    (114, 3),
    (120, 4),   # UNII-2E_2 (<=144)
    (144, 4),
    (149, 5),   # UNII-3 (>=149, <184)
    (165, 5),
])
def test_cal_channel_group(channel, group):
    assert eeprom._cal_channel_group(channel) == group


# ---------------------------------------------------------------------------
# read_rx_high_gain_5g — channel-group routes to the right EEPROM word
# ---------------------------------------------------------------------------

def test_read_rx_high_gain_5g_group_routing():
    """The 5 GHz table picks which of three EEPROM u16s + which byte half to
    sign-extend. We stuff each slot with a byte whose two nibbles decode to
    a distinct gain pair, so the wrong route gives an obviously wrong pair.
    Nibble decode (_sign_extend_4bit): bit 3 = positive sign, low 3 bits = mag.
    """
    # 0x9A = nibbles (0xA, 0x9) → gains (+2, +1)
    # 0xBC = nibbles (0xC, 0xB) → gains (+4, +3)
    # 0xDE = nibbles (0xE, 0xD) → gains (+6, +5)
    # 0xF8 = nibbles (0x8, 0xF) → gains (+0, +7)  (low nibble is signed-zero)
    # 0x6A = nibbles (0xA, 0x6) → gains (+2, -6)
    # 0x4C = nibbles (0xC, 0x4) → gains (+4, -4)
    buf = bytearray(0x100)
    # GRP0_1 word @ 0xFA: low byte = JAPAN (0x9A → (2,1)), high = UNII-1 (0xBC → (4,3))
    buf[0xFA], buf[0xFB] = 0x9A, 0xBC
    # GRP2_3 word @ 0xFC: low = UNII-2 (0xDE → (6,5)), high = UNII-2E_1 (0xF8 → (0,7))
    buf[0xFC], buf[0xFD] = 0xDE, 0xF8
    # GRP4_5 word @ 0xFE: low = UNII-2E_2 (0x6A → (2,-6)), high = UNII-3 (0x4C → (4,-4))
    buf[0xFE], buf[0xFF] = 0x6A, 0x4C
    t = FakeTransport(bytes(buf))
    assert eeprom.read_rx_high_gain_5g(t, 190) == (2, 1)    # JAPAN
    assert eeprom.read_rx_high_gain_5g(t, 36)  == (4, 3)    # UNII-1
    assert eeprom.read_rx_high_gain_5g(t, 60)  == (6, 5)    # UNII-2
    assert eeprom.read_rx_high_gain_5g(t, 110) == (0, 7)    # UNII-2E_1
    assert eeprom.read_rx_high_gain_5g(t, 140) == (2, -6)   # UNII-2E_2
    assert eeprom.read_rx_high_gain_5g(t, 165) == (4, -4)   # UNII-3


def test_read_rx_high_gain_5g_invalid_returns_zero():
    buf = bytearray(0x100)
    # All 0xFF → invalid for every group.
    for i in range(0xFA, 0x100):
        buf[i] = 0xFF
    t = FakeTransport(bytes(buf))
    assert eeprom.read_rx_high_gain_5g(t, 36) == (0, 0)


# ---------------------------------------------------------------------------
# tssi / has_ext_lna — NIC_CONF_1 bit decode
# ---------------------------------------------------------------------------

def test_has_ext_lna_per_band():
    """NIC_CONF_1 bits: 2 = LNA_EXT_2G, 3 = LNA_EXT_5G."""
    buf = bytearray(0x100)
    # NIC_CONF_1 is at 0x36 — set bit 2 (LNA_EXT_2G), clear bit 3.
    buf[0x36] = 0b0000_0100
    buf[0x37] = 0
    t = FakeTransport(bytes(buf))
    assert eeprom.has_ext_lna(t, band_2g=True)
    assert not eeprom.has_ext_lna(t, band_2g=False)


def test_tssi_enabled_gated_by_temp_tx_alc():
    """tssi_enabled = NOT temp_tx_alc_enabled AND NIC_CONF_1.TX_ALC_EN."""
    buf = bytearray(0x100)
    # NIC_CONF_1 = 0x36: set TX_ALC_EN bit 13 (=0x2000)
    buf[0x36] = 0x00
    buf[0x37] = 0x20
    # MT_EE_TX_POWER_EXT_PA_5G @ 0x054 — bit 15 (high byte's bit 7) drives
    # temp_tx_alc_enabled when paired with NIC_CONF_1.TEMP_TX_ALC.
    # Clear bit 15 → temp_tx_alc disabled → tssi_enabled = True.
    buf[0x54] = 0x00
    buf[0x55] = 0x00
    t = FakeTransport(bytes(buf))
    assert eeprom.tssi_enabled(t)

    # Set EXT_PA_5G bit 15 + NIC_CONF_1.TEMP_TX_ALC (bit 1) → temp_tx_alc
    # enabled → tssi_enabled = False.
    buf[0x55] = 0x80
    buf[0x36] = 0x02
    t = FakeTransport(bytes(buf))
    assert not eeprom.tssi_enabled(t)
