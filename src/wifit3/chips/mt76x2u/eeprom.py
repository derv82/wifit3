"""MT76x2U EEPROM (eFuse) reader.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

The EEPROM (or eFuse-backed virtual EEPROM) is read 4 bytes at a time via
vendor control transfer with bRequest = MT_VEND_READ_EEPROM (0x09). The
virtual-address-bit `MT_VEND_TYPE_EEPROM = BIT(31)` routes the read through
that path in `transport.read32`.

Layout (struct fields from mt76x02_eeprom.h):

    0x000  CHIP_ID         u16
    0x002  VERSION         u16
    0x004  MAC_ADDR        6 bytes
    0x022  ANTENNA         u16
    0x034  NIC_CONF_0      u16  (RX path / TX path / PA type / board)
    0x036  NIC_CONF_1      u16  (LNA_EXT_2G/5G / TX_ALC_EN / HW_RF_CTRL)
    0x03a  FREQ_OFFSET     u8
    ...
"""
from __future__ import annotations

import logging
import struct

from .constants import MT_VEND_TYPE_EEPROM
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# Field offsets used at the wifit3 layer — [SRC] mt76x02_eeprom.h.
EE_CHIP_ID      = 0x000
EE_VERSION      = 0x002
EE_MAC_ADDR     = 0x004
EE_NIC_CONF_0   = 0x034
EE_NIC_CONF_1   = 0x036
EE_FREQ_OFFSET  = 0x03A

# Standard mt76x2 EEPROM is 512 bytes; we don't need to slurp the whole thing
# for M2, only the header + key fields.


def read_block(transport: MT76x2UTransport, offset: int, length: int) -> bytes:
    """Read `length` bytes from EEPROM starting at `offset`.

    Mirrors kernel `mt76x02_eeprom_copy` semantics: arbitrary byte offset
    (the power-info tables sit at 0x56 / 0x5C / 0x62 / 0x80 — not 4-aligned).
    Rounds the read down to a 4-byte boundary, reads enough u32 words to
    cover the range, then slices the requested bytes out.
    """
    if length <= 0:
        return b""
    start_aligned = offset & ~0x3
    end = offset + length
    end_aligned = (end + 3) & ~0x3
    span = end_aligned - start_aligned
    buf = bytearray(span)
    for i in range(0, span, 4):
        word = transport.read32(MT_VEND_TYPE_EEPROM | (start_aligned + i))
        struct.pack_into("<I", buf, i, word)
    head = offset - start_aligned
    return bytes(buf[head:head + length])


def read_u16(transport: MT76x2UTransport, offset: int) -> int:
    """Read a u16 field from EEPROM."""
    # Round down to 4-byte boundary, then index into the word.
    aligned = offset & ~0x3
    word = transport.read32(MT_VEND_TYPE_EEPROM | aligned)
    shift = (offset - aligned) * 8
    return (word >> shift) & 0xFFFF


def read_mac_address(transport: MT76x2UTransport) -> str:
    """Return the MAC address as `AA:BB:CC:DD:EE:FF`.

    Layout: 6 bytes starting at EE_MAC_ADDR=0x004. Two u32 reads cover it
    (the second read gives 4 bytes but we only use the first 2).
    """
    blob = read_block(transport, EE_MAC_ADDR, 8)
    mac = blob[:6]
    return ":".join(f"{b:02X}" for b in mac)


def read_chip_id(transport: MT76x2UTransport) -> int:
    return read_u16(transport, EE_CHIP_ID)


def _sign_extend_4bit(val: int) -> int:
    """`mt76x02_sign_extend(val, 4)` — [SRC] mt76x02_eeprom.h:137.

    Bit 3 of `val` is the sign flag (1 = positive, 0 = negative); bits 0-2
    hold the magnitude. The kernel returns +mag if sign=1 else -mag.
    """
    val &= 0xF
    mag = val & 0x7
    return mag if (val & 0x8) else -mag


def _sign_extend(val: int, size: int) -> int:
    """`mt76x02_sign_extend(val, size)` — [SRC] mt76x02_eeprom.h:137. Bit
    ``size-1`` is the sign flag (1=positive); the low ``size-1`` bits hold
    the magnitude."""
    sign_bit = 1 << (size - 1)
    sign = val & sign_bit
    mag = val & (sign_bit - 1)
    return mag if sign else -mag


def _sign_extend_optional(val: int, size: int) -> int:
    """`mt76x02_sign_extend_optional` — [SRC] mt76x02_eeprom.h:147. Returns 0
    when the high bit isn't set (i.e. value not enabled)."""
    enable = val & (1 << size)
    return _sign_extend(val, size) if enable else 0


def _rate_power_val(val: int) -> int:
    """`mt76x02_rate_power_val` — [SRC] mt76x02_eeprom.h:154. Returns 0 if
    the byte is 0/0xFF (uninitialized), else 7-bit signed-optional."""
    val &= 0xFF
    if val == 0 or val == 0xFF:
        return 0
    return _sign_extend_optional(val, 7)


def _field_valid(val: int) -> bool:
    """`mt76x02_field_valid` — non-zero and non-0xFF."""
    val &= 0xFF
    return val != 0 and val != 0xFF


def read_rx_high_gain_2g(transport: MT76x2UTransport) -> tuple[int, int]:
    """`mt76x2_set_rx_gain_group` 2GHz branch — [SRC] mt76x2/eeprom.c:183-194,
    called from `mt76x2_read_rx_gain` at eeprom.c:264.

    Returns ``(high_gain_ch0, high_gain_ch1)`` in dBm-units used by
    `mt76x2_apply_gain_adj`. Kernel reads the EEPROM u16 at
    MT_EE_RF_2G_RX_HIGH_GAIN (0x0F8), takes the **high byte** (>>8), then
    splits the low / high nibble each as a sign-magnitude 4-bit value.

    If the high byte is 0x00 or 0xFF (mt76x02_field_valid → false), both
    gains default to 0.
    """
    val = read_u16(transport, 0x0F8) >> 8
    if val == 0 or val == 0xFF:
        return (0, 0)
    return (_sign_extend_4bit(val), _sign_extend_4bit(val >> 4))


def read_rx_high_gain_5g(transport: MT76x2UTransport,
                         channel: int) -> tuple[int, int]:
    """`mt76x2_set_rx_gain_group` 5GHz branch — [SRC] mt76x2/eeprom.c:227-252
    (`mt76x2_get_5g_rx_gain`) → 183-194. Picks the correct EEPROM byte for
    the channel's cal-group, then splits the nibbles like the 2 GHz path."""
    grp = _cal_channel_group(channel)
    if grp == 0:   # JAPAN (184-196)
        val = read_u16(transport, 0x0FA) & 0xFF
    elif grp == 1:  # UNII-1 (<=48)
        val = read_u16(transport, 0x0FA) >> 8
    elif grp == 2:  # UNII-2 (<=64)
        val = read_u16(transport, 0x0FC) & 0xFF
    elif grp == 3:  # UNII-2E_1 (<=114)
        val = read_u16(transport, 0x0FC) >> 8
    elif grp == 4:  # UNII-2E_2 (<=144)
        val = read_u16(transport, 0x0FE) & 0xFF
    else:           # UNII-3 (>=149)
        val = read_u16(transport, 0x0FE) >> 8
    if val == 0 or val == 0xFF:
        return (0, 0)
    return (_sign_extend_4bit(val), _sign_extend_4bit(val >> 4))


def _cal_channel_group(channel: int) -> int:
    """`mt76x2_get_cal_channel_group` — [SRC] mt76x2/eeprom.c:210-224.
    Returns the 0-5 group index for 5 GHz channel ``channel``."""
    if 184 <= channel <= 196:
        return 0   # MT_CH_5G_JAPAN
    if channel <= 48:
        return 1   # MT_CH_5G_UNII_1
    if channel <= 64:
        return 2   # MT_CH_5G_UNII_2
    if channel <= 114:
        return 3   # MT_CH_5G_UNII_2E_1
    if channel <= 144:
        return 4   # MT_CH_5G_UNII_2E_2
    return 5       # MT_CH_5G_UNII_3


# ---------------------------------------------------------------------------
# Per-rate TX power table + per-channel target power.
# These feed `mt76x2_phy_set_txpower` ([SRC] mt76x2/phy.c:137-181) which
# writes TX_PWR_CFG_0..9 + TX_ALC_CFG_0 on every channel-tune. The static
# 0x3a3a3a3a values in our initvals are wrong for any specific card; this
# port is required to make the chip TX at the EEPROM-calibrated power.
# ---------------------------------------------------------------------------


def read_rate_power(transport: MT76x2UTransport, band_2g: bool) -> dict:
    """`mt76x2_get_rate_power` — [SRC] mt76x2/eeprom.c:292-340.

    Reads per-rate TX power values from EEPROM, returns a dict with:
      cck:  [4 ints]  — CCK rates 0..3
      ofdm: [8 ints]  — OFDM rates 0..7
      ht:   [16 ints] — HT MCS 0..15
      vht:  [2 ints]  — VHT MCS 8/9 (chain 0 + 1 share both)

    Each value is `mt76x02_rate_power_val` (signed-magnitude 7-bit). Kernel
    zero-inits the struct first, so unread VHT slots stay 0.
    """
    cck = [0] * 4
    ofdm = [0] * 8
    ht = [0] * 16
    vht = [0] * 2

    val = read_u16(transport, 0x0A0)   # MT_EE_TX_POWER_CCK
    cck[0] = cck[1] = _rate_power_val(val & 0xFF)
    cck[2] = cck[3] = _rate_power_val(val >> 8)

    val = read_u16(transport, 0x0B2 if not band_2g else 0x0A2)
    ofdm[0] = ofdm[1] = _rate_power_val(val & 0xFF)
    ofdm[2] = ofdm[3] = _rate_power_val(val >> 8)

    val = read_u16(transport, 0x0B4 if not band_2g else 0x0A4)
    ofdm[4] = ofdm[5] = _rate_power_val(val & 0xFF)
    ofdm[6] = ofdm[7] = _rate_power_val(val >> 8)

    val = read_u16(transport, 0x0A6)
    ht[0] = ht[1] = _rate_power_val(val & 0xFF)
    ht[2] = ht[3] = _rate_power_val(val >> 8)

    val = read_u16(transport, 0x0A8)
    ht[4] = ht[5] = _rate_power_val(val & 0xFF)
    ht[6] = ht[7] = _rate_power_val(val >> 8)

    val = read_u16(transport, 0x0AA)
    ht[8] = ht[9] = _rate_power_val(val & 0xFF)
    ht[10] = ht[11] = _rate_power_val(val >> 8)

    val = read_u16(transport, 0x0AC)
    ht[12] = ht[13] = _rate_power_val(val & 0xFF)
    ht[14] = ht[15] = _rate_power_val(val >> 8)

    val = read_u16(transport, 0x0BE)
    if band_2g:
        val >>= 8
    vht[0] = vht[1] = _rate_power_val(val >> 8)

    logger.debug(
        "MT7612U: rate_power band_2g=%s → cck=%s ofdm=%s ht=%s vht=%s",
        band_2g, cck, ofdm[:4], ht[:4], vht,
    )
    return {"cck": cck, "ofdm": ofdm, "ht": ht, "vht": vht}


def _read_power_info_2g(transport: MT76x2UTransport, channel: int,
                         chain_offset: int) -> dict:
    """`mt76x2_get_power_info_2g` — [SRC] mt76x2/eeprom.c:343-371."""
    if channel < 6:
        delta_idx = 3
    elif channel < 11:
        delta_idx = 4
    else:
        delta_idx = 5
    data = read_block(transport, chain_offset, 6)
    chain = {
        "tssi_slope": data[0],
        "tssi_offset": data[1],
        "target_power": data[2],
        "delta": _sign_extend_optional(data[delta_idx], 7),
    }
    target_power = read_u16(transport, 0x0F6) >> 8   # MT_EE_RF_2G_TSSI_OFF_TXPOWER
    return {"chain": chain, "target_power": target_power}


def _read_power_info_5g(transport: MT76x2UTransport, channel: int,
                         chain_offset_base: int) -> dict:
    """`mt76x2_get_power_info_5g` — [SRC] mt76x2/eeprom.c:373-422."""
    group = _cal_channel_group(channel)
    offset = chain_offset_base + group * 5   # MT_TX_POWER_GROUP_SIZE_5G = 5
    # delta_idx selection — table from eeprom.c:388-411.
    if channel >= 192:
        delta_idx = 4
    elif channel >= 184:
        delta_idx = 3
    elif channel < 44:
        delta_idx = 3
    elif channel < 52:
        delta_idx = 4
    elif channel < 58:
        delta_idx = 3
    elif channel < 98:
        delta_idx = 4
    elif channel < 106:
        delta_idx = 3
    elif channel < 116:
        delta_idx = 4
    elif channel < 130:
        delta_idx = 3
    elif channel < 149:
        delta_idx = 4
    elif channel < 157:
        delta_idx = 3
    else:
        delta_idx = 4
    data = read_block(transport, offset, 5)
    chain = {
        "tssi_slope": data[0],
        "tssi_offset": data[1],
        "target_power": data[2],
        "delta": _sign_extend_optional(data[delta_idx], 7),
    }
    target_power = read_u16(transport, 0x0F8) & 0xFF   # MT_EE_RF_2G_RX_HIGH_GAIN low byte
    return {"chain": chain, "target_power": target_power}


def read_power_info(transport: MT76x2UTransport, channel: int,
                     band_2g: bool, tssi_enabled: bool) -> dict:
    """`mt76x2_get_power_info` — [SRC] mt76x2/eeprom.c:425-455.

    Returns a dict with:
      target_power: int    — overall target power (per-board)
      chain: list[dict]    — per-chain {tssi_slope, tssi_offset, target_power, delta}
      delta_bw40: int      — BW40 power adjustment (signed-magnitude)
      delta_bw80: int      — BW80 power adjustment
    """
    bw40 = read_u16(transport, 0x050)   # MT_EE_TX_POWER_DELTA_BW40
    bw80 = read_u16(transport, 0x052)   # MT_EE_TX_POWER_DELTA_BW80

    if band_2g:
        chain0 = _read_power_info_2g(transport, channel, 0x056)
        chain1 = _read_power_info_2g(transport, channel, 0x05C)
    else:
        bw40 >>= 8
        chain0 = _read_power_info_5g(transport, channel, 0x062)
        chain1 = _read_power_info_5g(transport, channel, 0x080)

    target_power = chain0["target_power"]   # kernel uses 2GHz/5GHz-specific source
    if tssi_enabled or not _field_valid(target_power):
        target_power = chain0["chain"]["target_power"]

    result = {
        "target_power": target_power,
        "chain": [chain0["chain"], chain1["chain"]],
        "delta_bw40": _rate_power_val(bw40),
        "delta_bw80": _rate_power_val(bw80),
    }
    logger.debug(
        "MT7612U: power_info ch=%d band_2g=%s tssi=%s → target=%d "
        "chain0(tp=%d, d=%d, tssi_slope=%d, tssi_offset=%d) "
        "chain1(tp=%d, d=%d) bw40_delta=%d",
        channel, band_2g, tssi_enabled, target_power,
        chain0["chain"]["target_power"], chain0["chain"]["delta"],
        chain0["chain"]["tssi_slope"], chain0["chain"]["tssi_offset"],
        chain1["chain"]["target_power"], chain1["chain"]["delta"],
        result["delta_bw40"],
    )
    return result


def has_ext_lna(transport: MT76x2UTransport, band_2g: bool) -> bool:
    """`mt76x2_has_ext_lna` — [SRC] mt76x2/eeprom.h:52-60."""
    val = read_u16(transport, 0x036)   # MT_EE_NIC_CONF_1
    mask = (1 << 2) if band_2g else (1 << 3)   # LNA_EXT_2G / 5G
    return bool(val & mask)


def temp_tx_alc_enabled(transport: MT76x2UTransport) -> bool:
    """`mt76x2_temp_tx_alc_enabled` — [SRC] mt76x2/eeprom.h:62-73. Gated on
    MT_EE_TX_POWER_EXT_PA_5G bit 15 AND NIC_CONF_1 TEMP_TX_ALC."""
    ext_pa_5g = read_u16(transport, 0x054)   # MT_EE_TX_POWER_EXT_PA_5G
    if not (ext_pa_5g & (1 << 15)):
        return False
    nic_conf_1 = read_u16(transport, 0x036)
    return bool(nic_conf_1 & (1 << 1))   # TEMP_TX_ALC


def tssi_enabled(transport: MT76x2UTransport) -> bool:
    """`mt76x2_tssi_enabled` — [SRC] mt76x2/eeprom.h:75-81. NIC_CONF_1
    TX_ALC_EN AND NOT temp_tx_alc."""
    if temp_tx_alc_enabled(transport):
        return False
    nic_conf_1 = read_u16(transport, 0x036)
    return bool(nic_conf_1 & (1 << 13))   # TX_ALC_EN


def read_nic_conf_0(transport: MT76x2UTransport) -> dict:
    """Decode MT_EE_NIC_CONF_0 into human-readable fields.

    [SRC] mt76x02_eeprom.h:100-106.
    """
    val = read_u16(transport, EE_NIC_CONF_0)
    return {
        "raw": val,
        "rx_path": val & 0xF,
        "tx_path": (val >> 4) & 0xF,
        "pa_int_2g": bool(val & (1 << 8)),
        "pa_int_5g": bool(val & (1 << 9)),
        "pa_io_current": bool(val & (1 << 10)),
        "board_type": (val >> 12) & 0x3,
    }


def read_nic_conf_1(transport: MT76x2UTransport) -> dict:
    val = read_u16(transport, EE_NIC_CONF_1)
    return {
        "raw": val,
        "hw_rf_ctrl": bool(val & (1 << 0)),
        "temp_tx_alc": bool(val & (1 << 1)),
        "lna_ext_2g": bool(val & (1 << 2)),
        "lna_ext_5g": bool(val & (1 << 3)),
        "tx_alc_en": bool(val & (1 << 13)),
    }
