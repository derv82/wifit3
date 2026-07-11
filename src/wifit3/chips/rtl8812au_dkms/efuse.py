"""RTL8812AU EFUSE parse — 2-path (2T2R), vendor port.

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
    sys_cfg: int                   # raw REG_SYS_CFG (0xF0); seeds build_jaguar_params' cut_version
    is_c_cut: bool                 # 8812a CUTVersion == C_CUT (gates phy_FixSpur + RF-read CCA)
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


C_CUT_VERSION = 2                  # [SRC] HalVerDef.h:58 (CUTVersion enum)


def _ext_amplifier_flags(m: bytes) -> tuple:
    """[SRC] hal_ReadPAType_8812A (rtl8812a_hal_init.c:1126) — the 4 external-PA/LNA flags.

    Registry amplifier type is AUTO (userland has no registry override), so PAType/LNAType come
    from efuse (0xFF -> 0). A band is "external" only when BOTH of its two flag bits are set
    [SRC :1143-1144,1158-1159]. Returns (ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g) —
    consumed by BOTH the board_type assembly and Hal_ReadRFEType_8812A's BIT7 decode.
    """
    pa = 0 if m[EEPROM_PA_TYPE] == 0xFF else m[EEPROM_PA_TYPE]
    lna_2g = 0 if m[EEPROM_LNA_TYPE_2G] == 0xFF else m[EEPROM_LNA_TYPE_2G]
    lna_5g = 0 if m[EEPROM_LNA_TYPE_5G] == 0xFF else m[EEPROM_LNA_TYPE_5G]
    ext_pa_2g = bool((pa & (1 << 5)) and (pa & (1 << 4)))
    ext_lna_2g = bool((lna_2g & (1 << 7)) and (lna_2g & (1 << 3)))
    ext_pa_5g = bool((pa & (1 << 1)) and (pa & (1 << 0)))
    ext_lna_5g = bool((lna_5g & (1 << 7)) and (lna_5g & (1 << 3)))
    return ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g


def _parse_is_c_cut(sys_cfg: int) -> bool:
    """[SRC] IS_VENDOR_8812A_C_CUT (HalVerDef.h:194) — 8812a silicon cut == C_CUT.

    read_chip_version_8812a sets version_id.CUTVersion = REG_SYS_CFG[15:12] + 1 for the 8812
    [SRC rtl8812a_hal_init.c:3383-3385]. C_CUT is CUTVersion 2, i.e. REG_SYS_CFG[15:12] == 1
    (the captured AWUS036ACH). Non-C-cut silicon takes the shorter phy_FixSpur branch (chan.py)
    and the RF-read CCA-on-secondary toggle (rf.py) — both gated on this."""
    return (((sys_cfg >> 12) & 0xF) + 1) == C_CUT_VERSION


def _parse_rfe_type(m: bytes, ext: tuple, autoload_fail: bool) -> int:
    """[SRC] Hal_ReadRFEType_8812A (rtl8812a_hal_init.c:1296), 8812AU, registry RFE = AUTO(64).

    A blank (0xFF) or autoload-fail efuse -> rfe_type 0 (the 8812AU default). A BIT7-set 0xCA
    encodes the RFE type from the external-PA/LNA flags (3/0/2/4). Otherwise rfe_type = 0xCA[5:0],
    with the 2013 workaround forcing a bogus type-4-with-external-amp burn back to 0 on the
    8812AU. ``ext`` = (ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g) from _ext_amplifier_flags.
    The captured AWUS036ACH burns 0xCA[5:0]=3 (BIT7 clear) -> rfe_type 3.
    """
    ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g = ext
    rfe = m[EEPROM_RFE_OPTION]
    if autoload_fail or rfe == 0xFF:
        return 0                                     # 8812AU blank / autoload-fail default
    if rfe & 0x80:                                   # external-PA/LNA-encoded rfe_type
        if ext_lna_5g:
            if ext_pa_5g:
                return 3 if (ext_lna_2g and ext_pa_2g) else 0
            return 2
        return 4
    rfe_type = rfe & 0x3F
    if rfe_type == 4 and (ext_pa_5g or ext_pa_2g or ext_lna_5g or ext_lna_2g):
        return 0                                     # 8812AU incorrect-EFUSE-map workaround
    return rfe_type


def _parse_board_type(m: bytes, ext: tuple) -> tuple:
    """[SRC] Hal_ReadAmplifierType_8812A (rtl8812a_hal_init.c:1192) + hal_dm.c:382-405
    board_type assembly.

    Each set external flag lights its ODM_BOARD bit (GPA bit3 / GLNA bit4 / APA bit6 / ALNA
    bit7). BT (bit2) needs the chip-version-gated EEPROMBluetoothCoexist read (:821-890); the
    AWUS036ACH is a non-BT board, so BT stays 0 and the BT decode is not ported.

    The four type_* sub-codes [SRC :1200-1221] pack the per-path ext type, but only when a
    band has BOTH paths external; extType bits live in 0xBD/0xBF [6,2] (PA B/A) and [5:4,1:0]
    (LNA B/A). On the AWUS036ACH all sub-fields read 0.
    """
    ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g = ext

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
    # [SRC] Hal_ReadAmplifierType_8812A:1211-1221 (extType bits from the RAW 0xBD/0xBF bytes).
    lna2, lna5 = m[EEPROM_LNA_TYPE_2G], m[EEPROM_LNA_TYPE_5G]
    type_gpa = type_apa = type_glna = type_alna = 0
    if ext_pa_2g:
        type_gpa = (((lna2 >> 6) & 1) << 2) | ((lna2 >> 2) & 1)
    if ext_pa_5g:
        type_apa = (((lna5 >> 6) & 1) << 2) | ((lna5 >> 2) & 1)
    if ext_lna_2g:
        type_glna = (((lna2 >> 4) & 3) << 2) | (lna2 & 3)
    if ext_lna_5g:
        type_alna = (((lna5 >> 4) & 3) << 2) | (lna5 & 3)

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
    """Probe-phase chip-info + EFUSE read (2T2R), byte-for-byte to the vendor
    ReadAdapterInfo8812AU -> Hal_ReadPROMContent_8812A -> InitAdapterVariablesByPROM_8812AU.
    """
    sys_cfg = t.read32(R.REG_SYS_CFG)               # 0xF0 read_chip_version (also seeds cut_version)
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

    ext = _ext_amplifier_flags(m)                       # Hal_ReadAmplifierType before ReadRFEType
    board_type, type_glna, type_gpa, type_alna, type_apa = _parse_board_type(m, ext)

    return ChipParams(
        crystal_cap=_parse_crystal_cap(m),
        mac_address=_parse_mac_address(m),
        rfe_type=_parse_rfe_type(m, ext, autoload_fail),
        autoload_fail=autoload_fail,
        sys_cfg=sys_cfg,
        is_c_cut=_parse_is_c_cut(sys_cfg),
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
