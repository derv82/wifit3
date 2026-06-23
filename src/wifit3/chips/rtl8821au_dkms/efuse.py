"""RTL8821AU EFUSE read + chip-param decode — vendor port.

The probe phase reads the burned-in efuse to recover per-card parameters: the
``crystal_cap`` (AFE trim, replaces the M3 hardcode), the ``mac_address``, and the
per-rate-group 2.4 GHz TX-power base + nTX diffs that feed the M-TXPWR txagc sweep.
Vendor path: ReadAdapterInfo8812AU -> hal_ReadPROMContent_8812A ->
Hal_EfuseParseTxPowerInfo_8812A, over efuse_ReadEFuse + ReadEFuseByte.

Mechanism ([SRC] ReadEFuseByte rtw_efuse.c:2209 + the PG-block format; [WIRE] cap1
frames 65..2475, device 39):

  * Physical read: each byte writes the 10-bit address (EFUSE_CTRL+1 / +2), clears
    EFUSE_CTRL+3 bit7 to trigger, polls EFUSE_CTRL bit31 for ready, then takes the low
    byte of the 32-bit EFUSE_CTRL.
  * Header unpacking: the physical efuse is a stream of PG blocks (header = section
    offset + 4-bit word-enable, or an EXT_HEADER); enabled words flatten into a 512 B
    logical map (``section*8 + word*2``). Identical PG format to the rtl8814au_dkms
    sibling.
  * Parse: crystal_cap / mac / TX-power read at fixed logical offsets (path A, 1T1R).

Gated by REG_EFUSE_ACCESS (0x69 on, 0x00 off). The 8821au is 1T1R, so only path A's
PG TX-power block (starting at pg_txpwr_saddr=0x10) is decoded.
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
    crystal_cap: int
    mac_address: Optional[str]
    chip_version: int
    autoload_fail: bool
    tx_power: PathTxPwr      # path A, 2.4 GHz
    tx_power_5g: PathTxPwr   # path A, 5 GHz (bw40_base = 14 UNII groups; no CCK)
    bb_swing_2g: int         # path-A TxScale (0x0C1C[31:21]) for 2.4 GHz
    bb_swing_5g: int         # path-A TxScale for 5 GHz


def _efuse_one_byte_read(t, addr: int) -> int:
    """[SRC] ReadEFuseByte (rtw_efuse.c:2209) — one physical efuse byte.

    Write the 10-bit address (EFUSE_CTRL+1 low, +2 high preserving the top 6 bits),
    clear EFUSE_CTRL+3 bit7 to trigger, poll EFUSE_CTRL bit31 (ready), take the low byte.
    """
    t.write8(C.REG_EFUSE_CTRL + 1, addr & 0xFF)
    v = t.read8(C.REG_EFUSE_CTRL + 2)
    t.write8(C.REG_EFUSE_CTRL + 2, ((addr >> 8) & 0x03) | (v & 0xFC))
    v = t.read8(C.REG_EFUSE_CTRL + 3)
    t.write8(C.REG_EFUSE_CTRL + 3, v & 0x7F)
    value32 = t.read32(C.REG_EFUSE_CTRL)
    retry = 0
    while not ((value32 >> 24) & 0x80) and retry < 10000:
        value32 = t.read32(C.REG_EFUSE_CTRL)
        retry += 1
    value32 = t.read32(C.REG_EFUSE_CTRL)   # re-read after the HW settle delay
    return value32 & 0xFF


def _read_logical_map(t) -> bytes:
    """[SRC] efuse_ReadEFuse — physical efuse PG stream -> 512 B logical map.

    Same PG-block format as the rtl8814au_dkms sibling: each block has a header
    (section offset + 4-bit word-enable, or an EXT_HEADER form) and contributes two
    bytes per enabled word to eFuseWord[section][word]; the 64x4 words flatten into the
    logical map at ``section*8 + word*2``.
    """
    word = [[0xFFFF] * C.EFUSE_MAX_WORD_UNIT for _ in range(C.EFUSE_MAX_SECTION_JAGUAR)]
    addr = 0

    header = _efuse_one_byte_read(t, addr)
    addr += 1
    if header == 0xFF:
        return b"\xFF" * C.EFUSE_MAP_LEN_JAGUAR          # empty efuse

    while header != 0xFF and addr < C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
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

        if offset < C.EFUSE_MAX_SECTION_JAGUAR:
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & (1 << i):               # word disabled
                    continue
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] = data & 0xFF
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] |= (data << 8) & 0xFF00
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
        else:                                     # invalid offset — skip its words
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & 0x01:
                    continue
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break

        header = _efuse_one_byte_read(t, addr)
        if header != 0xFF:
            addr += 1

    tbl = bytearray(b"\xFF" * C.EFUSE_MAP_LEN_JAGUAR)
    for i in range(C.EFUSE_MAX_SECTION_JAGUAR):
        for j in range(C.EFUSE_MAX_WORD_UNIT):
            tbl[i * 8 + j * 2] = word[i][j] & 0xFF
            tbl[i * 8 + j * 2 + 1] = (word[i][j] >> 8) & 0xFF
    return bytes(tbl)


def _parse_crystal_cap(m: bytes) -> int:
    """[SRC] Hal_EfuseParseXtal_8812A — efuse 0xB9, else default 0x20."""
    v = m[C.EEPROM_XTAL]
    return C.EEPROM_DEFAULT_CRYSTAL_CAP if v == 0xFF else v


def _s4(n: int) -> int:
    """Signed 4-bit nibble -> int (PG_TXPWR_*_DIFF_TO_S8BIT)."""
    return n - 16 if (n & 0x8) else n


def _parse_tx_power(m: bytes) -> PathTxPwr:
    """[SRC] hal_load_pg_txpwr_info_path_2g — path-A base + nTX diff nibbles.

    The path-A 2.4 GHz PG block is 18 B at pg_txpwr_saddr (0x10): 6 CCK group bases, 5
    BW40 group bases, then 7 diff bytes packing signed nibbles —
      11: MSB=BW20[1TX] LSB=OFDM[1TX]   12: MSB=BW40[2TX] LSB=BW20[2TX]
      13: MSB=OFDM[2TX] LSB=CCK[2TX]    14: MSB=BW40[3TX] LSB=BW20[3TX]
      15: MSB=OFDM[3TX] LSB=CCK[3TX]    16,17: 4TX
    CCK[1TX] has no byte (CCK base is the 1TX reference) -> 0. The 8821au is 1T1R, so
    only the 1TX diffs are ever applied (phy_get_pg_txpwr_idx with ntx_idx==1).
    """
    base = C.PG_TXPWR_SADDR
    cck_base = tuple(m[base + i] for i in range(6))
    bw40_base = tuple(m[base + 6 + i] for i in range(5))
    d = [m[base + 11 + i] for i in range(7)]
    bw20_diff = (_s4(d[0] >> 4), _s4(d[1] & 0xF), _s4(d[3] & 0xF))
    ofdm_diff = (_s4(d[0] & 0xF), _s4(d[2] >> 4), _s4(d[4] >> 4))
    cck_diff = (0, _s4(d[2] & 0xF), _s4(d[4] & 0xF))
    return PathTxPwr(cck_base, bw40_base, cck_diff, ofdm_diff, bw20_diff)


def _parse_tx_power_5g(m: bytes) -> PathTxPwr:
    """[SRC] hal_load_pg_txpwr_info_path_5g — path-A 5 GHz base + nTX diff nibbles.

    The 24 B 5 GHz PG block follows the 18 B 2.4G block (so it starts at 0x22 for path A):
    14 BW40 group bases (the 14 UNII groups), then the diff bytes —
      14: MSB=BW20[1T] LSB=OFDM[1T]   15: MSB=BW40[2T] LSB=BW20[2T]
      16: MSB=BW40[3T] LSB=BW20[3T]   18: MSB=OFDM[2T] LSB=OFDM[3T]
    No CCK on 5 GHz. For 20 MHz only the BW40 base + OFDM/BW20 1TX diffs are used.
    """
    b5 = C.PG_TXPWR_SADDR + 18
    bw40_base = tuple(m[b5 + i] for i in range(14))
    b14, b18 = m[b5 + 14], m[b5 + 18]
    ofdm_diff = (_s4(b14 & 0xF), _s4(b18 >> 4), _s4(b18 & 0xF))   # 1T, 2T, 3T
    bw20_diff = (_s4(b14 >> 4), _s4(m[b5 + 15] & 0xF), _s4(m[b5 + 16] & 0xF))
    return PathTxPwr((), bw40_base, (), ofdm_diff, bw20_diff)


# Per-path BB-swing: a 2-bit index per path maps to an 11-bit TxScale value, band-
# independent (only the efuse byte differs: 0xC6 = 2.4 GHz, 0xC7 = 5 GHz). [SRC]
# PHY_GetTxBBSwing_8812A: 0->0 dB, 1->-3 dB, 2->-6 dB, 3->-9 dB.
_BB_SWING = {0: 0x200, 1: 0x16A, 2: 0x101, 3: 0x0B6}


def _parse_bb_swing(m: bytes, byte_off: int) -> int:
    """[SRC] PHY_GetTxBBSwing_8812A (registry AUTO) — path-A TxScale for one band.

    The efuse byte packs a 2-bit swing index per path (A = bits[1:0]); an unburned byte
    (0xFF) means 0 dB. The 8821au is 1T1R, so only path A is read.
    """
    sw = m[byte_off]
    if sw == 0xFF:
        sw = 0x00
    return _BB_SWING[sw & 0x3]


def _parse_mac_address(m: bytes) -> Optional[str]:
    """[SRC] Hal_GetEfuseDefinition / hal_config_macaddr — efuse 0x107..0x10C."""
    mac = m[C.EEPROM_MAC_ADDR_8821AU:C.EEPROM_MAC_ADDR_8821AU + 6]
    if len(mac) != 6 or all(b == 0xFF for b in mac) or all(b == 0 for b in mac):
        return None
    return ":".join(f"{b:02x}" for b in mac)


def read_chip_params(t) -> ChipParams:
    """Probe-phase chip-info + efuse read. [WIRE] cap1 frames 65..2475.

    Reproduces the capture's pre-power-on sequence: chip-version read, autoload check,
    EFUSE access-on + power-switch reads, the byte loop, access-off.
    """
    chip_version = t.read32(C.REG_SYS_CFG)         # ReadChipVersion (0xF0)
    t.read32(0x0068)                               # version-id companion read
    ee = t.read8(C.REG_9346CR)
    autoload_fail = not (ee & (1 << 5))            # bit5 = EEPROM present

    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_ON)
    t.read16(0x0000)                               # EFUSE power-switch status reads
    t.read16(0x0002)
    t.read16(0x0008)
    m = _read_logical_map(t)
    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_OFF)

    return ChipParams(
        crystal_cap=_parse_crystal_cap(m),
        mac_address=_parse_mac_address(m),
        chip_version=chip_version,
        autoload_fail=autoload_fail,
        tx_power=_parse_tx_power(m),
        tx_power_5g=_parse_tx_power_5g(m),
        bb_swing_2g=_parse_bb_swing(m, C.EEPROM_TX_BBSWING_2G),
        bb_swing_5g=_parse_bb_swing(m, C.EEPROM_TX_BBSWING_5G),
    )
