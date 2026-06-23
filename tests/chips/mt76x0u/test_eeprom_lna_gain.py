"""mt76x0u per-channel LNA-gain RX cal (the weak-5GHz-RX fix).

`lna_gain_for_channel` must mirror `mt76x02_get_rx_gain` + `mt76x02_get_lna_gain`
[SRC] mt76x02_eeprom.c:102-147: band / 5 GHz-subband selection by channel number,
the ``!= 0 && != 0xff`` validity fallback to lna_5g[0], the 0xff→0 guard, and s8
sign-extension (the kernel stores the result in `mt76x02_rx_freq_cal.lna_gain`).
"""
from wifit3.chips.mt76x0u.constants import (
    MT76X0_EEPROM_SIZE,
    MT_EE_LNA_GAIN,
    MT_EE_RSSI_OFFSET_2G_1,
    MT_EE_RSSI_OFFSET_5G_1,
)
from wifit3.chips.mt76x0u.eeprom import EEPROMCache, lna_gain_for_channel


def _cache(*, lna_2g=0, lna_5g0=0, lna_5g1=0, lna_5g2=0) -> EEPROMCache:
    """512-byte EEPROM with the four LNA-gain bytes set (high byte of each word
    except lna_2g, which is the MT_EE_LNA_GAIN low byte)."""
    data = bytearray(MT76X0_EEPROM_SIZE)
    data[MT_EE_LNA_GAIN] = lna_2g              # 0x044 lo
    data[MT_EE_LNA_GAIN + 1] = lna_5g0         # 0x045 hi
    data[MT_EE_RSSI_OFFSET_2G_1 + 1] = lna_5g1  # 0x049 hi
    data[MT_EE_RSSI_OFFSET_5G_1 + 1] = lna_5g2  # 0x04d hi
    return EEPROMCache(bytes(data))


def test_band_and_5ghz_subband_selection():
    c = _cache(lna_2g=4, lna_5g0=8, lna_5g1=10, lna_5g2=12)
    assert lna_gain_for_channel(c, 6) == 4      # 2.4 GHz → lna_2g
    assert lna_gain_for_channel(c, 36) == 8     # 5 GHz ch≤64 → lna_5g[0]
    assert lna_gain_for_channel(c, 100) == 10   # 5 GHz ch≤128 → lna_5g[1]
    assert lna_gain_for_channel(c, 157) == 12   # 5 GHz ch>128 → lna_5g[2] (the CH157 case)


def test_invalid_subband_falls_back_to_lna_5g0():
    # Both 0 and 0xff are invalid (mt76x02_field_valid) → use lna_5g[0].
    c = _cache(lna_5g0=9, lna_5g1=0x00, lna_5g2=0xFF)
    assert lna_gain_for_channel(c, 100) == 9    # subband[1] == 0   → fallback
    assert lna_gain_for_channel(c, 157) == 9    # subband[2] == 0xff → fallback


def test_selected_0xff_yields_zero():
    # lna_2g / lna_5g[0] aren't fallback-checked; a 0xff there → 0 via the guard.
    c = _cache(lna_2g=0xFF, lna_5g0=0xFF)
    assert lna_gain_for_channel(c, 6) == 0
    assert lna_gain_for_channel(c, 36) == 0


def test_high_bit_byte_sign_extends():
    # cal.rx.lna_gain is s8; a high-bit byte is a (rare but genuine) negative gain.
    assert lna_gain_for_channel(_cache(lna_2g=0xFE), 6) == -2
    assert lna_gain_for_channel(_cache(lna_2g=0x80), 6) == -128
