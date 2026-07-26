"""MT7925AU per-band TX power tables (mt7925_mcu_rate_txpower_band, mt7925/mcu.c:3638).

The driver sends one batch of MCU_UNI_CMD(SET_POWER_LIMIT) commands per band, 3
channels each, every channel carrying a 449-byte per-rate SKU table. The SKU is built
by mt7925_mcu_build_sku (mcu.c:3593) from the per-rate limits, and every rate is
memset to the channel's regulatory power (mt76_get_rate_power_limits, eeprom.c:376 —
no device-tree/SAR override on a USB dongle). For the world "00" domain that power is
20 dBm = 40 in 0.5-dBm units (0x28); rates build_sku leaves untouched stay 0x7f.

We never propagate an OS regulatory domain, so we fix alpha2="00"/20 dBm and emit the
same deterministic tables every bring-up. Channel arrays + the build_sku byte order
are ported verbatim from the source.
"""
import struct

# ruff: noqa: F403, F405
from .constants import *
from . import mcu

# Hardcoded per-band channel lists (mt7925/mcu.c:3645-3675).
CHAN_LIST_2GHZ = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
CHAN_LIST_5GHZ = [36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58, 60, 62, 64,
                  100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120, 122, 124,
                  126, 128, 132, 134, 136, 138, 140, 142, 144, 149, 151, 153, 155,
                  157, 159, 161, 165, 167]
CHAN_LIST_6GHZ = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 33, 35, 37,
                  39, 41, 43, 45, 47, 49, 51, 53, 55, 57, 59, 61, 65, 67, 69, 71, 73,
                  75, 77, 79, 81, 83, 85, 87, 89, 91, 93, 97, 99, 101, 103, 105, 107,
                  109, 111, 113, 115, 117, 119, 121, 123, 125, 129, 131, 133, 135, 137,
                  139, 141, 143, 145, 147, 149, 151, 153, 155, 157, 161, 163, 165, 167,
                  169, 171, 173, 175, 177, 179, 181, 183, 185, 187, 189, 193, 195, 197,
                  199, 201, 203, 205, 207, 209, 211, 213, 215, 217, 219, 221, 225, 227,
                  229, 233]

BATCH_LEN = 3
SKU_POWER_LIMIT = 449         # MT_CONNAC3_SKU_POWER_LIMIT (mt7925/mcu.h:537)
SKU_UNLIMITED = 0x7f          # build_sku memset value for untouched rates
WORLD_POWER = 40              # 2 * 20 dBm; the world "00" regulatory limit (0x28)
TX_UNSET_POWER = 127          # get_ch_power returns the unset tx_power for a non-regdom chan

# Per-channel power = min(2*max_reg_power, tx_power) for a channel the world "00" regdom
# enables (all resolve to 20 dBm -> WORLD_POWER), else the unset tx_power (a whole 0x7f SKU).
# The enabled sets are the world-"00" valid 20 MHz control channels (cross-checkable against
# cfg80211 world_regdom): all of 2.4 GHz, the step-4 5 GHz channels, and NO 6 GHz.
WORLD_ENABLED_5GHZ = {36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
                      128, 132, 136, 140, 144, 149, 153, 157, 161, 165}


def _chan_power(band_code: int, channel: int) -> int:
    if band_code == 1:
        return WORLD_POWER                        # all 2.4 GHz channels enabled at 20 dBm
    if band_code == 2:
        return WORLD_POWER if channel in WORLD_ENABLED_5GHZ else TX_UNSET_POWER
    return TX_UNSET_POWER                          # 6 GHz: no world regdom rule


def build_sku(is_2ghz: bool, power: int = WORLD_POWER) -> bytes:
    """Port of mt7925_mcu_build_sku (mt7925/mcu.c:3593). Every per-rate limit is
    ``power`` (mt76_get_rate_power_limits memsets them all to the channel power); the
    rest of the 449-byte table stays 0x7f. Byte order mirrors the C memcpy sequence:
    cck(2.4 only) + ofdm(stride 40) + ht(8+8+1) + vht(4x10 stride 12) + he(7x12) +
    eht(16x16)."""
    v = power & 0xFF
    sku = bytearray([SKU_UNLIMITED] * SKU_POWER_LIMIT)
    if is_2ghz:
        sku[0:4] = bytes([v]) * 4                      # cck
    sku[4:12] = bytes([v]) * 8                          # ofdm (offset then += 8*5=40)
    off = 44
    sku[off:off + 8] = bytes([v]) * 8                   # ht mcs[0]
    sku[off + 8:off + 16] = bytes([v]) * 8              # ht mcs[1]
    sku[off + 16] = v                                   # ht extra (mcs[0][0])
    off = 61
    for _ in range(4):                                  # vht: 4 rows of 10, stride 12
        sku[off:off + 10] = bytes([v]) * 10
        off += 12
    for _ in range(7):                                  # he: 7 rows of 12
        sku[off:off + 12] = bytes([v]) * 12
        off += 12
    for _ in range(16):                                 # eht: 16 rows of 16
        sku[off:off + 16] = bytes([v]) * 16
        off += 16
    return bytes(sku)


def _tlv_header(n_chan: int, band_code: int, last_msg: int) -> bytes:
    """mt7925_tx_power_limit_tlv (mcu.h:543), 52 bytes. alpha2 "00" -> 30 30 00 00."""
    return struct.pack("<4xHHBxHBBBB4s32x",
                       0x1, 52,          # tag, len
                       0,                # ver (pad0 via x, rsv1 via H below)... see note
                       0,                # rsv1
                       n_chan, band_code, last_msg, 0,   # n_chan, band, last_msg, limit_type
                       b"00\x00\x00")    # alpha2[4]


def rate_txpower_band(band_code: int, chan_list):
    """Port of mt7925_mcu_rate_txpower_band: batch the band's channels by 3 and emit one
    SET_POWER_LIMIT command per batch, each channel's SKU built at its regdom power.
    Returns a list of (cmd, payload)."""
    is_2ghz = band_code == 1
    last_ch = chan_list[-1]
    cmds = []
    for i in range(0, len(chan_list), BATCH_LEN):
        batch = chan_list[i:i + BATCH_LEN]
        last_msg = 1 if last_ch in batch else 0
        hdr = _tlv_header(len(batch), band_code, last_msg)
        body = b"".join(bytes([ch]) + build_sku(is_2ghz, _chan_power(band_code, ch))
                        for ch in batch)
        cmds.append((mcu.MCU_UNI_CMD(MCU_UNI_CMD_SET_POWER_LIMIT), hdr + body))
    return cmds


def rate_txpower_all(has_6ghz: bool = True):
    """Port of mt7925_mcu_set_rate_txpower (mcu.c:3778): 2.4 then 5 then (if 6 GHz) 6."""
    cmds = rate_txpower_band(1, CHAN_LIST_2GHZ)
    cmds += rate_txpower_band(2, CHAN_LIST_5GHZ)
    if has_6ghz:
        cmds += rate_txpower_band(3, CHAN_LIST_6GHZ)
    return cmds
