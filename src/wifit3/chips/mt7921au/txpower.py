"""
MT7921AU TX-power SKU limits — a port of mt76_connac_mcu_set_rate_txpower
/ mt76_connac_mcu_rate_txpower_band / mt76_connac_mcu_build_sku.

The host sends *regulatory* per-rate power limits (the firmware applies the
per-card EFUSE power calibration internally), so this is deterministic: with no
device-tree power-limits node, mt76_get_rate_power_limits fills every rate with
the channel's target power, and build_sku lays it out into the 161-byte SKU. For
the world ('00') domain every enabled channel caps at 20 dBm (-> 40 in the s8
half-dBm-ish units the firmware expects here); disabled channels keep the 127
"max power" default. Verified byte-identical across both units and all captures.

One important wire fact: the kernel does mt76_connac_mcu_reg_rr(MT_PSE_BASE) after
each batch, but that REG_READ (MCU_CE_QUERY(REG_READ), cid 0xc0) does NOT appear
anywhere in the capture — the Kali mt7921u build omits it. The wire is ground
truth, so we send the batches back-to-back with no interleaved reg read.
"""
import struct

# mt76_connac_mcu_rate_txpower_band channel lists — the full hardware lists
# (NOT the regdomain), copied verbatim from mt76_connac_mcu.c.
CHAN_2GHZ = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
CHAN_5GHZ = [
    36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58, 60, 62, 64, 100, 102, 104,
    106, 108, 110, 112, 114, 116, 118, 120, 122, 124, 126, 128, 132, 134, 136,
    138, 140, 142, 144, 149, 151, 153, 155, 157, 159, 161, 165, 169, 173, 177,
]
CHAN_6GHZ = [
    1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 33, 35, 37, 39, 41,
    43, 45, 47, 49, 51, 53, 55, 57, 59, 61, 65, 67, 69, 71, 73, 75, 77, 79, 81,
    83, 85, 87, 89, 91, 93, 97, 99, 101, 103, 105, 107, 109, 111, 113, 115, 117,
    119, 121, 123, 125, 129, 131, 133, 135, 137, 139, 141, 143, 145, 147, 149,
    151, 153, 155, 157, 161, 163, 165, 167, 169, 171, 173, 175, 177, 179, 181,
    183, 185, 187, 189, 193, 195, 197, 199, 201, 203, 205, 207, 209, 211, 213,
    215, 217, 219, 221, 225, 227, 229, 233,
]

MT_SKU_POWER_LIMIT = 161   # bytes per channel SKU
_BATCH_LEN = 8             # is_connac2(dev) ? 8 : 16
_MAX_POWER = 127           # is_connac2(dev) ? 127 : 63  (build_sku memset value)
_WORLD_MAX_EIRP = 20       # world '00' regdomain max_eirp (dBm), every channel
_TX_POWER = 127            # 2 * conf.power_level; power_level 0 -> 127


def _enabled(band_idx, hw_value):
    """A channel is enabled iff cfg80211's world domain kept it — i.e. it appears
    in our regdomain table. No 6 GHz channels are enabled in the world domain."""
    from . import regdomain as rd
    if band_idx == 0:
        return any(hw == hw_value for hw, _ in rd.CHANNELS_2GHZ)
    if band_idx == 1:
        return any(hw == hw_value for hw, _ in rd.CHANNELS_5GHZ)
    return False


def _target_power(band_idx, hw_value):
    """mt76_connac_get_ch_power: enabled -> min(2*max_reg_power, tx_power),
    disabled (or not found) -> tx_power unchanged."""
    if _enabled(band_idx, hw_value):
        return min(2 * _WORLD_MAX_EIRP, _TX_POWER)
    return _TX_POWER


def _build_sku(band_idx, tp):
    """mt76_connac_mcu_build_sku with uniform limits = tp (no DT node) and the
    connac2 layout. cck is filled only for 2.4 GHz; the 2-byte gaps the kernel
    leaves between the 10-wide mcs copies and 12-wide slots stay at _MAX_POWER."""
    b = tp & 0xFF
    sku = bytearray([_MAX_POWER] * MT_SKU_POWER_LIMIT)
    if band_idx == 0:                       # cck (2.4 GHz only)
        sku[0:4] = bytes([b]) * 4
    off = 4                                 # sizeof(cck)
    sku[off:off + 8] = bytes([b]) * 8       # ofdm
    off += 8                                # 12
    for _ in range(2):                      # ht: mcs[0..1][0:8]
        sku[off:off + 8] = bytes([b]) * 8
        off += 8
    sku[off] = b                            # mcs[0][0]
    off += 1                                # 29
    for _ in range(4):                      # vht: mcs[0..3], copy 10 advance 12
        sku[off:off + 10] = bytes([b]) * 10
        off += 12
    for _ in range(7):                      # he: ru[0..6], copy 12 advance 12
        sku[off:off + 12] = bytes([b]) * 12
        off += 12
    return bytes(sku)


def rate_txpower_payloads(caps=None):
    """One SET_RATE_TX_POWER payload per 8-channel batch, in wire order. Each payload =
    tx_power_tlv(44) + n_chan x sku_tlv(1+161).

    The band set is gated on the runtime NIC caps: 2.4/5/6 GHz is emitted iff
    has_2ghz/has_5ghz/has_6ghz, and last_ch (which flags the last batch's last_msg)
    is the last channel of the highest present band — a 1:1 port of
    mt76_connac_mcu_set_rate_txpower + the last_ch pick in mt76_connac_mcu_rate_txpower_band
    [SRC mt76_connac_mcu.c:2183-2188,2260-2284]. ``caps=None`` (or the reference units,
    all bands present) reproduces the captured wire: 2.4+5+6 GHz, last_ch 233."""
    from . import regdomain as rd
    has_2 = True if caps is None else caps.has_2ghz
    has_5 = True if caps is None else caps.has_5ghz
    has_6 = True if caps is None else caps.has_6ghz
    bands = []
    if has_2:
        bands.append((0, CHAN_2GHZ, 1))
    if has_5:
        bands.append((1, CHAN_5GHZ, 2))
    if has_6:
        bands.append((2, CHAN_6GHZ, 3))
    if has_6:
        last_ch = CHAN_6GHZ[-1]
    elif has_5:
        last_ch = CHAN_5GHZ[-1]
    elif has_2:
        last_ch = CHAN_2GHZ[-1]
    else:
        return []
    payloads = []
    for band_idx, ch_list, tlv_band in bands:
        n = len(ch_list)
        n_batch = (n + _BATCH_LEN - 1) // _BATCH_LEN
        idx = 0
        for b in range(n_batch):
            num_ch = (n - b * _BATCH_LEN) if b == n_batch - 1 else _BATCH_LEN
            body = bytearray()
            last_msg = 0
            for _ in range(num_ch):
                ch = ch_list[idx]
                idx += 1
                tp = _target_power(band_idx, ch)
                body += bytes([ch & 0xFF]) + _build_sku(band_idx, tp)
                last_msg = 1 if ch == last_ch else 0
            tlv = struct.pack("<BBHBBBB4s32x", 0, 0, 0, num_ch, tlv_band,
                              last_msg, 0, rd.WORLD_ALPHA2)
            payloads.append(bytes(tlv) + bytes(body))
    return payloads
