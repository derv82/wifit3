"""RTL8814AU EFUSE read + chip-param decode — vendor faithful port.

The probe phase reads the burned-in efuse to recover per-card parameters the BB
config needs: ``rfe_type`` (phy_cond walker discriminator), ``crystal_cap`` (AFE
trim), and the ``mac_address``. The vendor path is ReadAdapterInfo8814AU ->
hal_InitPGData_8814A -> EFUSE_ShadowMapUpdate -> hal_EfuseReadEFuse8814A.

Mechanism (all [SRC] hal_EfuseReadEFuse8814A:1646 + the per-byte EFUSE_CTRL
protocol; [WIRE] cap1 frames 51..5677, device 51, 312 physical bytes):

  * Physical read: each byte is a 9-transfer EFUSE_CTRL cycle (bank-select 0x34,
    address 0x31/0x32, trigger 0x33 bit7=0, poll 0x33 bit7, data 0x30).
  * Header unpacking: the physical efuse is a stream of PG blocks. Each block has
    a header (section offset + 4-bit word-enable); enabled words contribute two
    bytes to ``eFuseWord[section][word]``. The 16x4 words flatten into a 512 B
    logical map (``section*8 + word*2``).
  * Parse: rfe_type/crystal_cap/mac_address read at fixed logical offsets.

The whole read is gated by REG_EFUSE_ACCESS (0x69 on, 0x00 off). cut_version /
package_type come from REG_SYS_CFG1 (read but not decoded — they don't gate this
card's BB walker; see bb.py).
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from . import constants as C


class PathTxPwr(NamedTuple):
    """Per-RF-path 2.4 GHz TX-power base + nTX diffs (efuse PG data)."""
    cck_base: tuple    # 6 channel groups
    bw40_base: tuple   # 5 channel groups (also the OFDM/HT/VHT base)
    cck_diff: tuple    # [1TX, 2TX, 3TX]  (1TX has no efuse byte -> 0)
    ofdm_diff: tuple
    bw20_diff: tuple


class ChipParams(NamedTuple):
    rfe_type: int
    crystal_cap: int
    mac_address: Optional[str]
    chip_version: int
    autoload_fail: bool
    tx_power: tuple    # 4x PathTxPwr (paths A..D)
    bb_swing: tuple    # 4x 11-bit TxScale value (paths A..D), 2.4 GHz
    bb_swing_5g: tuple  # 4x 11-bit TxScale value (paths A..D), 5 GHz


def _efuse_one_byte_read(t, addr: int) -> int:
    """[SRC] efuse_OneByteRead (halmac per-byte EFUSE_CTRL protocol).

    Select the WIFI efuse bank, set the 10-bit physical address, trigger a read
    (clear EFUSE_CTRL+3 bit7), poll until it is set again, then read the byte.
    """
    v = t.read16(C.REG_EFUSE_TEST)
    t.write16(C.REG_EFUSE_TEST, v & ~C.EFUSE_SEL_MASK)          # WIFI bank 0
    t.write8(C.REG_EFUSE_CTRL + 1, addr & 0xFF)                 # addr[7:0]
    v = t.read8(C.REG_EFUSE_CTRL + 2)
    t.write8(C.REG_EFUSE_CTRL + 2, (v & 0xFC) | ((addr >> 8) & 0x03))  # addr[9:8]
    v = t.read8(C.REG_EFUSE_CTRL + 3)
    t.write8(C.REG_EFUSE_CTRL + 3, v & ~C.EFUSE_CTRL_VALID)     # trigger read
    for _ in range(1000):
        if t.read8(C.REG_EFUSE_CTRL + 3) & C.EFUSE_CTRL_VALID:
            break
    return t.read8(C.REG_EFUSE_CTRL)


def _read_logical_map(t) -> bytes:
    """[SRC] hal_EfuseReadEFuse8814A — physical efuse stream -> 512 B logical map."""
    word = [[0xFFFF] * C.EFUSE_MAX_WORD_UNIT for _ in range(C.EFUSE_MAX_SECTION)]
    addr = 0

    header = _efuse_one_byte_read(t, addr)
    addr += 1
    if header == 0xFF:
        return b"\xFF" * C.EFUSE_MAP_LEN          # empty efuse

    while header != 0xFF and addr < C.EFUSE_REAL_CONTENT_LEN:
        if (header & 0x1F) == 0x0F:               # EXT_HEADER
            offset_2_0 = (header & 0xE0) >> 5
            ext = _efuse_one_byte_read(t, addr)
            addr += 1
            if ext == 0xFF:
                break
            if (ext & 0x0F) == 0x0F:              # ALL_WORDS_DISABLED
                header = _efuse_one_byte_read(t, addr)
                addr += 1
                break
            offset = ((ext & 0xF0) >> 1) | offset_2_0
            wden = ext & 0x0F
        else:
            offset = (header >> 4) & 0x0F
            wden = header & 0x0F

        if offset < C.EFUSE_MAX_SECTION:
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & (1 << i):               # word disabled
                    continue
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] = data & 0xFF
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] |= (data << 8) & 0xFF00
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break
        else:                                     # invalid offset — skip its words
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & 0x01:
                    continue
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break

        # Read the next PG header at the current address (advance only if valid).
        header = _efuse_one_byte_read(t, addr)
        if header != 0xFF:
            addr += 1

    tbl = bytearray(b"\xFF" * C.EFUSE_MAP_LEN)
    for i in range(C.EFUSE_MAX_SECTION):
        for j in range(C.EFUSE_MAX_WORD_UNIT):
            tbl[i * 8 + j * 2] = word[i][j] & 0xFF
            tbl[i * 8 + j * 2 + 1] = (word[i][j] >> 8) & 0xFF
    return bytes(tbl)


def _parse_rfe_type(m: bytes) -> int:
    """[SRC] hal_ReadRFEType_8814A — efuse 0xCA[6:0], else 8814AU fallback (1)."""
    v = m[C.EEPROM_RFE_OPTION]
    if v == 0xFF or (v & 0x80):
        return C.RFE_TYPE_8814AU_FALLBACK
    return v & 0x7F


def _parse_crystal_cap(m: bytes) -> int:
    """[SRC] hal_EfuseParseXtal_8814A — efuse 0xB9, else default 0x20."""
    v = m[C.EEPROM_XTAL]
    return C.EEPROM_DEFAULT_CRYSTAL_CAP if v == 0xFF else v


# 2.4G PG TX-power block per path (18 B: 6 CCK base, 5 BW40 base, 7 diff bytes);
# per-path stride 0x2A (2G block + 5G block). [SRC] hal_load_pg_txpwr_info_path_2g,
# pg_txpwr_saddr=0x10. [WIRE] reproduces the cold-boot txagc (0x1998) writes.
_TXPWR_PATH_OFF = (0x10, 0x3A, 0x64, 0x8E)


def _s4(n: int) -> int:
    """Signed 4-bit nibble -> int (PG_TXPWR_*_DIFF_TO_S8BIT)."""
    return n - 16 if (n & 0x8) else n


def _parse_tx_power(m: bytes) -> tuple:
    """[SRC] hal_load_pg_txpwr_info_path_2g — per-path base + nTX diff nibbles.

    Diff bytes (offsets 11..17 of the path block) pack signed nibbles:
      11: MSB=BW20[1TX] LSB=OFDM[1TX]   12: MSB=BW40[2TX] LSB=BW20[2TX]
      13: MSB=OFDM[2TX] LSB=CCK[2TX]    14: MSB=BW40[3TX] LSB=BW20[3TX]
      15: MSB=OFDM[3TX] LSB=CCK[3TX]    16,17: 4TX (skipped, max_tx_cnt=3)
    CCK[1TX] has no byte (CCK base is the 1TX reference) -> 0.
    """
    paths = []
    for base in _TXPWR_PATH_OFF:
        cck_base = tuple(m[base + i] for i in range(6))
        bw40_base = tuple(m[base + 6 + i] for i in range(5))
        d = [m[base + 11 + i] for i in range(7)]
        bw20_diff = (_s4(d[0] >> 4), _s4(d[1] & 0xF), _s4(d[3] & 0xF))
        ofdm_diff = (_s4(d[0] & 0xF), _s4(d[2] >> 4), _s4(d[4] >> 4))
        cck_diff = (0, _s4(d[2] & 0xF), _s4(d[4] & 0xF))
        paths.append(PathTxPwr(cck_base, bw40_base, cck_diff, ofdm_diff, bw20_diff))
    return tuple(paths)


# Per-path BB-swing: a 2-bit index per path maps to an 11-bit TxScale value. The value
# table is band-independent; only the efuse byte differs (0xC6 = 2.4 GHz, 0xC7 = 5 GHz).
# [SRC] PHY_GetTxBBSwing_8814A: 0->0 dB, 1->-3 dB, 2->-6 dB, 3->-9 dB.
_BB_SWING = {0: C.BBSWING_DEFAULT, 1: 0x16A, 2: 0x101, 3: 0x0B6}


def _parse_bb_swing(m: bytes, byte_off: int) -> tuple:
    """[SRC] PHY_GetTxBBSwing_8814A (registry AUTO) — per-path TxScale value for one band.

    The efuse byte packs a 2-bit swing index per path (A[1:0], B[3:2], C[5:4], D[7:6]); an
    unburned byte (0xFF) means 0 dB on every path. Each index maps to the 11-bit TxScale
    value written into TXSCALE[31:21]. Only the efuse path is ported: the registry-override
    / autoload-fail / external-PA branches are config paths the cold-boot wire does not take.
    """
    swing = m[byte_off]
    if swing == 0xFF:                      # unburned -> 0 dB all paths
        swing = 0x00
    return tuple(_BB_SWING[(swing >> (2 * p)) & 0x3] for p in range(4))


def _parse_bb_swing_2g(m: bytes) -> tuple:
    """2.4 GHz per-path TxScale, from efuse byte 0xC6 (M4e)."""
    return _parse_bb_swing(m, C.EEPROM_TX_BBSWING_2G)


def _parse_bb_swing_5g(m: bytes) -> tuple:
    """5 GHz per-path TxScale, from efuse byte 0xC7 (M5e)."""
    return _parse_bb_swing(m, C.EEPROM_TX_BBSWING_5G)


def _parse_mac_address(m: bytes) -> Optional[str]:
    """[SRC] hal_config_macaddr — efuse 0xD8..0xDD; None if blank/invalid."""
    mac = m[C.EEPROM_MAC_ADDR:C.EEPROM_MAC_ADDR + 6]
    if len(mac) != 6 or all(b == 0xFF for b in mac) or all(b == 0 for b in mac):
        return None
    return ":".join(f"{b:02x}" for b in mac)


def read_chip_params(t) -> ChipParams:
    """Probe-phase chip-info + efuse read. [WIRE] cap1 frames 51..5677.

    Reproduces the capture's pre-power-on sequence: chip-version read, autoload
    check, EFUSE access-on, the byte loop, access-off. Returns the decoded params
    the BB config consumes.
    """
    chip_version = t.read32(C.REG_SYS_CFG1)                 # ReadChipVersion
    ee = t.read8(C.REG_9346CR)
    autoload_fail = not (ee & C.EEPROM_EN)

    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_ON)
    t.read16(0x0002)                                        # efuse power-on status
    t.read16(0x0008)
    m = _read_logical_map(t)
    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_OFF)

    return ChipParams(
        rfe_type=_parse_rfe_type(m),
        crystal_cap=_parse_crystal_cap(m),
        mac_address=_parse_mac_address(m),
        chip_version=chip_version,
        autoload_fail=autoload_fail,
        tx_power=_parse_tx_power(m),
        bb_swing=_parse_bb_swing_2g(m),
        bb_swing_5g=_parse_bb_swing_5g(m),
    )
