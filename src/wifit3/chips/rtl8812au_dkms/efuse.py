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
from ..rtl88xxau_base.phy_cond import JaguarParams
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

# Amplifier-type EFUSE offsets [SRC] hal_pg.h:144-146. PAType (2G+5G) at 0xBC; LNAType_2G
# at 0xBD; LNAType_5G at 0xBF. The per-path "ext type" sub-fields are packed in 0xBD/0xBF.
EEPROM_PA_TYPE = 0xBC
EEPROM_LNA_TYPE_2G = 0xBD
EEPROM_LNA_TYPE_5G = 0xBF

# ODM board_type bitfield [SRC] phydm_pre_define.h:862-870 — the GLNA/GPA/ALNA/APA/BT bits
# the JaguarSeries phy_cond walker matches against. Assembled in hal_dm.c:382-405.
ODM_BOARD_BT = 1 << 2
ODM_BOARD_EXT_PA_2G = 1 << 3      # ODM_BOARD_EXT_PA  (2G PA)
ODM_BOARD_EXT_LNA_2G = 1 << 4     # ODM_BOARD_EXT_LNA (2G LNA)
ODM_BOARD_EXT_PA_5G = 1 << 6
ODM_BOARD_EXT_LNA_5G = 1 << 7

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
    board_type: int                # ODM GLNA/GPA/ALNA/APA/BT bitfield (phy_cond walker input)
    type_glna: int                 # per-path ext-LNA/PA type sub-codes (BB/AGC table selector)
    type_gpa: int
    type_alna: int
    type_apa: int


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


def _parse_board_type(m: bytes) -> tuple:
    """[SRC] hal_ReadPAType_8812A (rtl8812a_hal_init.c:1126) + Hal_ReadAmplifierType_8812A
    (:1192) + hal_dm.c:382-405 board_type assembly.

    The 8812AU reads PAType from 0xBC (shared 2G/5G) and LNAType from 0xBD (2G) / 0xBF (5G),
    treating 0xFF as 0. A band is "external PA/LNA" only when BOTH of its two flag bits are
    set [SRC :1143-1144,1158-1159]:
      ExternalPA_2G  = PAType_2G[5] & PAType_2G[4]      ExternalLNA_2G = LNAType_2G[7] & LNAType_2G[3]
      external_pa_5g = PAType_5G[1] & PAType_5G[0]      external_lna_5g = LNAType_5G[7] & LNAType_5G[3]
    Each set flag lights its ODM_BOARD bit (GPA bit3 / GLNA bit4 / APA bit6 / ALNA bit7).
    BT (bit2) needs the chip-version-gated EEPROMBluetoothCoexist read (:821-890); the
    AWUS036ACH is a non-BT board, so BT stays 0 and the BT decode is not ported.

    The four type_* sub-codes [SRC :1200-1221] pack the per-path ext type, but only when a
    band has BOTH paths external; extType bits live in 0xBD/0xBF [6,2] (PA B/A) and [5:4,1:0]
    (LNA B/A). On this card all sub-fields read 0.
    """
    pa = 0 if m[EEPROM_PA_TYPE] == 0xFF else m[EEPROM_PA_TYPE]
    lna_2g = 0 if m[EEPROM_LNA_TYPE_2G] == 0xFF else m[EEPROM_LNA_TYPE_2G]
    lna_5g = 0 if m[EEPROM_LNA_TYPE_5G] == 0xFF else m[EEPROM_LNA_TYPE_5G]

    ext_pa_2g = (pa & (1 << 5)) and (pa & (1 << 4))
    ext_lna_2g = (lna_2g & (1 << 7)) and (lna_2g & (1 << 3))
    ext_pa_5g = (pa & (1 << 1)) and (pa & (1 << 0))
    ext_lna_5g = (lna_5g & (1 << 7)) and (lna_5g & (1 << 3))

    board_type = 0
    if ext_lna_2g:
        board_type |= ODM_BOARD_EXT_LNA_2G
    if ext_lna_5g:
        board_type |= ODM_BOARD_EXT_LNA_5G
    if ext_pa_2g:
        board_type |= ODM_BOARD_EXT_PA_2G
    if ext_pa_5g:
        board_type |= ODM_BOARD_EXT_PA_5G

    # type_*: per-path ext-type sub-codes, decoded only when BOTH paths of a band are ext.
    # [SRC] Hal_ReadAmplifierType_8812A:1211-1221.
    type_gpa = type_apa = type_glna = type_alna = 0
    if (pa & (1 << 5)) and (pa & (1 << 4)):            # 2G PA both paths ext
        type_gpa = (((m[EEPROM_LNA_TYPE_2G] >> 6) & 1) << 2) | ((m[EEPROM_LNA_TYPE_2G] >> 2) & 1)
    if (pa & (1 << 1)) and (pa & (1 << 0)):            # 5G PA both paths ext
        type_apa = (((m[EEPROM_LNA_TYPE_5G] >> 6) & 1) << 2) | ((m[EEPROM_LNA_TYPE_5G] >> 2) & 1)
    if (lna_2g & (1 << 7)) and (lna_2g & (1 << 3)):    # 2G LNA both paths ext
        type_glna = (((m[EEPROM_LNA_TYPE_2G] >> 4) & 3) << 2) | (m[EEPROM_LNA_TYPE_2G] & 3)
    if (lna_5g & (1 << 7)) and (lna_5g & (1 << 3)):    # 5G LNA both paths ext
        type_alna = (((m[EEPROM_LNA_TYPE_5G] >> 4) & 3) << 2) | (m[EEPROM_LNA_TYPE_5G] & 3)

    return board_type, type_glna, type_gpa, type_alna, type_apa


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


def _efuse_power_switch(t, on: bool) -> None:
    """[SRC] Hal_EfusePowerSwitch8812A (rtl8812a_hal_init.c:1525) — gate efuse access.

    ON asserts REG_EFUSE_BURN_GNT (0xCF=0x69), then makes the 1.2V power / ELDR reset /
    8M loader clock valid, writing a SYS reg only when its bit is clear (so a warm chip
    that already has them set emits read-only). The 1.2V (0x00) write is commented out in
    the vendor, so 0x00 is always read-only; the LDO-2.5V EFUSE_TEST poke is bWrite-only
    (skipped on the read path). OFF deasserts the gate.
    """
    if not on:
        t.write8(R.REG_EFUSE_ACCESS, R.EFUSE_ACCESS_OFF)
        return
    t.write8(R.REG_EFUSE_ACCESS, R.EFUSE_ACCESS_ON)
    t.read16(R.REG_SYS_ISO_CTRL)                     # PWC_EV12V check (vendor write commented)
    v = t.read16(R.REG_SYS_FUNC_EN)
    if not (v & R.FEN_ELDR):
        t.write16(R.REG_SYS_FUNC_EN, v | R.FEN_ELDR)
    v = t.read16(R.REG_SYS_CLKR)
    if (not (v & R.LOADER_CLK_EN)) or (not (v & R.ANA8M)):
        t.write16(R.REG_SYS_CLKR, v | R.LOADER_CLK_EN | R.ANA8M)


def _read_usb_type(t) -> None:
    """[SRC] hal_ReadUsbType_8812AU (rtl8812a_hal_init.c:1437) — antenna/wmode side-probe.

    Reads efuse 1019 then 1018 for the antenna code (stop once bits[7:5] or bits[3:1] are
    non-zero), then 1021/1020 for the wmode (stop once bits[3:2] non-zero). The AWUS036ACH
    reports 2T2R / 11ac, so each loop stops on its first read. The decoded values steer
    nothing wifit3 configures, but the live reads are part of the vendor bring-up stream.
    """
    for i in range(2):
        reg = base_efuse.efuse_one_byte_read_poll33(t, 1019 - i)
        if ((reg >> 5) & 0x7) != 0 or ((reg >> 1) & 0x7) != 0:
            break
    for i in range(2):
        reg = base_efuse.efuse_one_byte_read_poll33(t, 1021 - i)
        if ((reg >> 2) & 0x3) != 0:
            break


def read_chip_params(t) -> ChipParams:
    """Probe-phase chip-info + EFUSE read (2T2R), byte-faithful to the vendor
    ReadAdapterInfo8812AU -> Hal_ReadPROMContent_8812A -> InitAdapterVariablesByPROM_8812AU.
    """
    t.read32(R.REG_SYS_CFG)                         # 0xF0 read_chip_version
    t.read32(R.REG_MULTI_FUNC_CTRL)                 # 0x68 read_chip_version companion
    ee = t.read8(R.REG_9346CR)                       # 0x0A Hal_ReadPROMContent autoload check
    autoload_fail = not (ee & (1 << 5))             # bit5 = EEPROM present

    # phase-1: hal_InitPGData_8812A FW-offload probe [SRC usb_halinit.c:2009-2022] — the
    # 8812AU reads efuse 0x200/0x202/0x204/0x210 (legacy poll-0x33 reads) BEFORE powering
    # efuse access on; the result only toggles a compiled-out FW-offload flag.
    for a in (0x200, 0x202, 0x204, 0x210):
        base_efuse.efuse_one_byte_read_poll33(t, a)

    _efuse_power_switch(t, on=True)
    m = base_efuse.read_logical_map(t)              # phase-2: ReadEFuseByte PG-block walk
    _efuse_power_switch(t, on=False)

    _read_usb_type(t)                                # phase-3: hal_ReadUsbType_8812AU

    # PG TX-power: interleaved per path [A-2G, A-5G, B-2G, B-5G] from saddr.
    off = PG_TXPWR_SADDR
    tx2g, tx5g = [], []
    for _path in range(2):
        tx2g.append(_parse_tx_power_2g(m, off))
        off += _PG_2G_LEN
        tx5g.append(_parse_tx_power_5g(m, off))
        off += _PG_5G_LEN

    board_type, type_glna, type_gpa, type_alna, type_apa = _parse_board_type(m)

    return ChipParams(
        crystal_cap=_parse_crystal_cap(m),
        mac_address=_parse_mac_address(m),
        rfe_type=_parse_rfe_type(m),
        autoload_fail=autoload_fail,
        tx_power_2g=tx2g,
        tx_power_5g=tx5g,
        bb_swing_2g=[_parse_bb_swing(m, EEPROM_TX_BBSWING_2G, p) for p in range(2)],
        bb_swing_5g=[_parse_bb_swing(m, EEPROM_TX_BBSWING_5G, p) for p in range(2)],
        board_type=board_type,
        type_glna=type_glna,
        type_gpa=type_gpa,
        type_alna=type_alna,
        type_apa=type_apa,
    )


def build_jaguar_params(params: ChipParams, sys_cfg: int) -> JaguarParams:
    """Thread the EFUSE-decoded board params into the phy_cond walker inputs.

    The JaguarSeries BB/AGC/RADIO tables branch on board_type (ext-LNA/PA) and cut version.
    cut_version is REG_SYS_CFG[15:12] [SRC] ReadChipVersion (rtl8812a_hal_init.c); ODM remaps
    cut A (raw 0) to 15 so the value-defined cut check matches the A-cut rows [SRC]
    phydm walker check_positive cut field. support_interface/platform keep the USB/CE defaults.
    """
    raw_cut = (sys_cfg >> 12) & 0xF
    cut_version = 15 if raw_cut == 0 else raw_cut
    return JaguarParams(
        cut_version=cut_version,
        board_type=params.board_type,
        type_glna=params.type_glna,
        type_gpa=params.type_gpa,
        type_alna=params.type_alna,
        type_apa=params.type_apa,
    )
