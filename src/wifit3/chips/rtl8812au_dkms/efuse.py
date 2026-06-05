"""RTL8812AU EFUSE parse — 2-path (2T2R), vendor faithful.

Uses the shared base EFUSE read mechanics (byte read + PG logical-map walk) and decodes
the 8812a logical offsets. The 8812 is 2T2R, so the PG TX-power block holds TWO paths
(interleaved per path: [A-2G, A-5G, B-2G, B-5G] from pg_txpwr_saddr=0x10), and the
TxBBSwing packs a 2-bit swing index per path. Deltas vs the 8821 (1T1R): MAC at 0xD7
(not 0x107), the rfe_type field at 0xCA (the 8821's RFE was inline), and both radios'
PG blocks + bb_swing.

[SRC] include/hal_pg.h (8812AU offsets), Hal_ReadRFEType_8812A (rtl8812a_hal_init.c:
1300), hal_load_pg_txpwr_info (hal_com_phycfg.c:1004), PHY_GetTxBBSwing_8812A.
"""
from __future__ import annotations

from typing import List, NamedTuple, Optional

from ..rtl88xxau_base import efuse as base_efuse
from ..rtl88xxau_base import registers as R
from . import constants as C

# Logical-map offsets [SRC] hal_pg.h (8812AU)
EEPROM_XTAL = 0xB9
EEPROM_TX_BBSWING_2G = 0xC6
EEPROM_TX_BBSWING_5G = 0xC7
EEPROM_RFE_OPTION = 0xCA
EEPROM_MAC_ADDR = C.EEPROM_MAC_ADDR_8812AU      # 0xD7
PG_TXPWR_SADDR = 0x10
_PG_2G_LEN = 18                                  # per-path 2.4 GHz PG block
_PG_5G_LEN = 24                                  # per-path 5 GHz PG block

# Per-path TxBBSwing: 2-bit index -> 11-bit TxScale. [SRC] PHY_GetTxBBSwing_8812A.
_BB_SWING = {0: 0x200, 1: 0x16A, 2: 0x101, 3: 0x0B6}


class PathTxPwr(NamedTuple):
    """One RF path / one band: per-group base + nTX diffs (efuse PG data)."""
    cck_base: tuple    # 6 channel groups (2.4 GHz only; () for 5 GHz)
    bw40_base: tuple   # 2.4 GHz: 5 groups; 5 GHz: 14 UNII groups (also the OFDM/HT/VHT base)
    cck_diff: tuple    # [1TX, 2TX, 3TX]  (1TX has no efuse byte -> 0)
    ofdm_diff: tuple
    bw20_diff: tuple


class ChipParams(NamedTuple):
    crystal_cap: int
    mac_address: Optional[str]
    rfe_type: int
    autoload_fail: bool
    tx_power_2g: List[PathTxPwr]   # [path A, path B]
    tx_power_5g: List[PathTxPwr]   # [path A, path B]
    bb_swing_2g: List[int]         # [path A, path B] TxScale
    bb_swing_5g: List[int]


def _s4(n: int) -> int:
    return base_efuse.s4(n)


def _parse_crystal_cap(m: bytes) -> int:
    v = m[EEPROM_XTAL]
    return C.EEPROM_DEFAULT_CRYSTAL_CAP if v == 0xFF else v


def _parse_mac_address(m: bytes) -> Optional[str]:
    mac = m[EEPROM_MAC_ADDR:EEPROM_MAC_ADDR + 6]
    if len(mac) != 6 or all(b == 0xFF for b in mac) or all(b == 0 for b in mac):
        return None
    return ":".join(f"{b:02x}" for b in mac)


def _parse_rfe_type(m: bytes) -> int:
    """[SRC] Hal_ReadRFEType_8812A — registry default is "use efuse"; for the 8812AU a
    blank (0xFF) efuse means rfe_type 0. The BIT7 external-PA/LNA encodings need the
    board-option flags we don't decode; on the AWUS036ACH 0xCA is a plain value."""
    rfe = m[EEPROM_RFE_OPTION]
    if rfe == 0xFF:
        return 0                       # 8812AU blank-efuse default
    if rfe & 0x80:
        return 0                       # external-PA/LNA encoded; 8812AU falls back to 0
    return rfe & 0x3F


def _parse_bb_swing(m: bytes, byte_off: int, path: int) -> int:
    """[SRC] PHY_GetTxBBSwing_8812A (registry AUTO) — 2 bits per path; 0xFF -> 0 dB."""
    sw = m[byte_off]
    if sw == 0xFF:
        sw = 0x00
    return _BB_SWING[(sw >> (2 * path)) & 0x3]


def _parse_tx_power_2g(m: bytes, base: int) -> PathTxPwr:
    """[SRC] hal_load_pg_txpwr_info_path_2g — one path's 18 B 2.4 GHz PG block.

    6 CCK group bases, 5 BW40 group bases, then 7 diff bytes packing signed nibbles
    (same nibble layout as the 8821). CCK[1TX] has no byte (the CCK base is the 1TX ref).
    """
    cck_base = tuple(m[base + i] for i in range(6))
    bw40_base = tuple(m[base + 6 + i] for i in range(5))
    d = [m[base + 11 + i] for i in range(7)]
    bw20_diff = (_s4(d[0] >> 4), _s4(d[1] & 0xF), _s4(d[3] & 0xF))
    ofdm_diff = (_s4(d[0] & 0xF), _s4(d[2] >> 4), _s4(d[4] >> 4))
    cck_diff = (0, _s4(d[2] & 0xF), _s4(d[4] & 0xF))
    return PathTxPwr(cck_base, bw40_base, cck_diff, ofdm_diff, bw20_diff)


def _parse_tx_power_5g(m: bytes, base: int) -> PathTxPwr:
    """[SRC] hal_load_pg_txpwr_info_path_5g — one path's 24 B 5 GHz PG block.

    14 BW40 group bases (the 14 UNII groups), then the diff bytes. No CCK on 5 GHz.
    """
    bw40_base = tuple(m[base + i] for i in range(14))
    b14, b18 = m[base + 14], m[base + 18]
    ofdm_diff = (_s4(b14 & 0xF), _s4(b18 >> 4), _s4(b18 & 0xF))   # 1T, 2T, 3T
    bw20_diff = (_s4(b14 >> 4), _s4(m[base + 15] & 0xF), _s4(m[base + 16] & 0xF))
    return PathTxPwr((), bw40_base, (), ofdm_diff, bw20_diff)


def read_chip_params(t) -> ChipParams:
    """Probe-phase chip-info + EFUSE read (2T2R). Mirrors the vendor pre-power-on reads
    (chip-version, autoload, access on/off + power-switch reads) around the byte loop."""
    t.read32(R.REG_SYS_CFG)                         # ReadChipVersion (0xF0)
    t.read32(0x0068)                                # version-id companion read
    ee = t.read8(R.REG_9346CR)
    autoload_fail = not (ee & (1 << 5))             # bit5 = EEPROM present

    t.write8(R.REG_EFUSE_ACCESS, R.EFUSE_ACCESS_ON)
    t.read16(0x0000)                                # EFUSE power-switch status reads
    t.read16(0x0002)
    t.read16(0x0008)
    m = base_efuse.read_logical_map(t)
    t.write8(R.REG_EFUSE_ACCESS, R.EFUSE_ACCESS_OFF)

    # PG TX-power: interleaved per path [A-2G, A-5G, B-2G, B-5G] from saddr.
    off = PG_TXPWR_SADDR
    tx2g, tx5g = [], []
    for _path in range(2):
        tx2g.append(_parse_tx_power_2g(m, off))
        off += _PG_2G_LEN
        tx5g.append(_parse_tx_power_5g(m, off))
        off += _PG_5G_LEN

    return ChipParams(
        crystal_cap=_parse_crystal_cap(m),
        mac_address=_parse_mac_address(m),
        rfe_type=_parse_rfe_type(m),
        autoload_fail=autoload_fail,
        tx_power_2g=tx2g,
        tx_power_5g=tx5g,
        bb_swing_2g=[_parse_bb_swing(m, EEPROM_TX_BBSWING_2G, p) for p in range(2)],
        bb_swing_5g=[_parse_bb_swing(m, EEPROM_TX_BBSWING_5G, p) for p in range(2)],
    )
