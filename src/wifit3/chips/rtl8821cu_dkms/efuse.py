"""RTL8821CU EFUSE read — the indirect read loop over REG_EFUSE_CTRL (0x30), the
physical->logical decode, and the one register read in the EFUSE-parse chain.

The vendor reads the full 512-byte physical EFUSE through an indirect controller: write the
byte address into bits [17:8] with the ready flag (bit 31) cleared, poll until the chip sets
the flag, then take the data from the low byte. This is the bulk of the cold-boot prologue
(one 4-byte write + poll per byte). The packed physical bytes are then decoded into the
512-byte logical shadow map the rest of the HAL indexes, and the parse chain reads it —
only the BT-coexist parse touches a register (REG_WL_BT_PWR_CTRL), so that read is the sole
wire op of the whole parse step.

Ported from:
  [SRC] hal/rtl8821c/rtl8821c_ops.c:462          rtl8821c_read_efuse (autoload + dump + parse)
  [SRC] hal/rtl8821c/rtl8821c_ops.c:134          Hal_EfuseParseBTCoexistInfo (the 0x68 read)
  [SRC] hal/rtl8821c/rtl8821c_ops.c:80           Hal_EfuseParseIDCode (map-valid = ID 0x8129)
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1088  read_hw_efuse_88xx (the 0x30 loop)
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1198  eeprom_parser_88xx (phys->logical)
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c (switch_efuse_bank_88xx)
  [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_common_8821c.c:169  cfg_ldo25_8821c
  [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_8821c_cfg.h:47-50   sizes (below)
Bit fields [SRC] hal/halmac/halmac_bit_8821c.h: EF_FLAG :689, EF_ADDR shift/mask :727-731,
EF_DATA mask :739, AUTOLOAD_SUS :129, BT_FUNC_EN :1395.
"""
from __future__ import annotations

from dataclasses import dataclass

REG_SYS_EEPROM_CTRL = 0x000A
REG_EFUSE_CTRL = 0x0030
REG_LDO_EFUSE_CTRL = 0x0034
REG_WL_BT_PWR_CTRL = 0x0068        # [SRC] halmac_reg2.h:406

EFUSE_SIZE_8821C = 512             # physical [SRC] halmac_8821c_cfg.h:47 EFUSE_SIZE_8821C
EEPROM_SIZE_8821C = 512            # logical shadow-map [SRC] halmac_8821c_cfg.h:48 EEPROM_SIZE_8821C
_PRTCT_EFUSE_SIZE = 96             # [SRC] halmac_8821c_cfg.h:50 PRTCT_EFUSE_SIZE_8821C

_RTL_EEPROM_ID = 0x8129            # [SRC] include/hal_pg.h:824 — log_map[0:2] when autoload valid
_EEPROM_RF_BOARD_OPTION = 0xC1     # [SRC] include/hal_pg.h:509 EEPROM_RF_BOARD_OPTION_8821C
_EEPROM_RF_BT_SETTING = 0xC3       # [SRC] include/hal_pg.h:511 EEPROM_RF_BT_SETTING_8821C
_EEPROM_RFE_OPTION = 0xCA          # [SRC] include/hal_pg.h:518 EEPROM_RFE_OPTION_8821C
_BIT_BT_FUNC_EN = 1 << 18          # [SRC] halmac_bit_8821c.h:1395 BIT_BT_FUNC_EN_8821C

_BIT_AUTOLOAD_SUS = 1 << 5
_BIT_EF_FLAG = 1 << 31
_SHIFT_EF_ADDR = 8
_MASK_EF_ADDR = 0x3FF
_BITS_EF_ADDR = _MASK_EF_ADDR << _SHIFT_EF_ADDR
_MASK_EF_DATA = 0xFF
_EFUSE_POLL_CNT = 1000000          # [SRC] read_hw_efuse_88xx cnt; replay matches on read #1


def _cfg_ldo25(t, enable: bool) -> None:
    """Toggle the 2.5 V EFUSE LDO via REG_LDO_EFUSE_CTRL+3 bit7.
    [SRC] halmac_common_8821c.c:169 cfg_ldo25_8821c."""
    v = t.read8(REG_LDO_EFUSE_CTRL + 3)
    t.write8(REG_LDO_EFUSE_CTRL + 3, (v | 0x80) if enable else (v & ~0x80))


def _switch_efuse_bank_wifi(t) -> None:
    """Select the WIFI EFUSE bank (0) via REG_LDO_EFUSE_CTRL+1 bits[1:0].
    [SRC] halmac_efuse_88xx.c switch_efuse_bank_88xx — returns early when already WIFI."""
    rv = t.read8(REG_LDO_EFUSE_CTRL + 1)
    if (rv & 0x3) == 0:
        return
    t.write8(REG_LDO_EFUSE_CTRL + 1, rv & ~0x3)
    if (t.read8(REG_LDO_EFUSE_CTRL + 1) & 0x3) != 0:
        raise RuntimeError("RTL8821CU: switch efuse bank to WIFI failed")


def read_hw_efuse(t, offset: int = 0, size: int = EFUSE_SIZE_8821C) -> bytes:
    """[SRC] read_hw_efuse_88xx — read `size` physical EFUSE bytes via the 0x30 controller."""
    _cfg_ldo25(t, False)            # reading EFUSE needs no 2.5 V LDO
    value32 = t.read32(REG_EFUSE_CTRL)
    out = bytearray(size)
    for addr in range(offset, offset + size):
        value32 &= ~(_MASK_EF_DATA | _BITS_EF_ADDR)
        value32 |= (addr & _MASK_EF_ADDR) << _SHIFT_EF_ADDR
        t.write32(REG_EFUSE_CTRL, value32 & ~_BIT_EF_FLAG)
        for _ in range(_EFUSE_POLL_CNT):
            tmp32 = t.read32(REG_EFUSE_CTRL)
            if tmp32 & _BIT_EF_FLAG:
                break
        else:
            raise RuntimeError(f"RTL8821CU: efuse read addr 0x{addr:03x} timed out")
        out[addr - offset] = tmp32 & _MASK_EF_DATA
    return bytes(out)


def eeprom_parser(phy_map: bytes) -> bytes:
    """Decode the packed physical EFUSE into the 0xFF-initialised logical shadow map.

    Each block is a 1-byte header (or 2-byte, when ``hdr & 0x1F == 0x0F``) giving a block
    index + a 4-bit word-enable; each enabled 16-bit word is scattered to
    ``blk*8 + word*2``. A 0xFF header ends the map. [SRC] eeprom_parser_88xx
    halmac_efuse_88xx.c:1198 (sizes: efuse 512, eeprom 512, prtct 96)."""
    log_map = bytearray(b"\xff" * EEPROM_SIZE_8821C)
    end_m1 = EFUSE_SIZE_8821C - _PRTCT_EFUSE_SIZE - 1     # 415
    end = EFUSE_SIZE_8821C - _PRTCT_EFUSE_SIZE            # 416
    idx = 0
    while True:
        hdr = phy_map[idx]
        if (hdr & 0x1F) == 0x0F:
            idx += 1
            hdr2 = phy_map[idx]
            if hdr2 == 0xFF:
                break
            blk = ((hdr2 & 0xF0) >> 1) | ((hdr >> 5) & 0x07)
            word_en = hdr2 & 0x0F
        else:
            blk = (hdr & 0xF0) >> 4
            word_en = hdr & 0x0F
        if hdr == 0xFF:
            break
        idx += 1
        if idx >= end_m1:
            raise RuntimeError("RTL8821CU: efuse parse overran physical map (header)")
        for i in range(4):
            if (~(word_en >> i)) & 1:
                eep = (blk << 3) + (i << 1)
                if eep + 1 > EEPROM_SIZE_8821C:
                    raise RuntimeError("RTL8821CU: efuse parse logical idx overflow")
                log_map[eep] = phy_map[idx]
                idx += 1
                if idx > end_m1:
                    raise RuntimeError("RTL8821CU: efuse parse overran physical map (word lo)")
                log_map[eep + 1] = phy_map[idx]
                idx += 1
                if idx > end:
                    raise RuntimeError("RTL8821CU: efuse parse overran physical map (word hi)")
    return bytes(log_map)


@dataclass
class EfuseInfo:
    """The handful of parsed EFUSE fields the post-EFUSE bring-up consumes (BT-coex
    power-on, RF front-end). Read at runtime, never hardcoded — a sibling card differs."""
    autoload_ok: bool
    log_map: bytes
    bt_coexist: bool        # combo card with BT fused on -> rtw_hal_power_on runs btc PowerOnSetting
    rfe_type: int           # RF front-end module type (board_info->rfe_type)
    single_ant_path: int    # 0 = RF_PATH_A/aux, 1 = RF_PATH_B/main
    ant_num: int            # 1 or 2 BT/WL shared antennas
    phys_map: bytes = b""   # raw 512-B physical dump (cached; PPG trim bytes index into it)
    chip_ver: int = 0       # halmac chip_ver (cut), set by bring-up from mount_get_chip_info
    package_type: int = 0   # hal->PackageType from the MAC-hidden report; 0 until that read
    phydm_rfe_type: int = 0     # dm->rfe_type = rfe_type_expand >> 3 (PHYDM table discriminator)
    phydm_package_type: int = 0  # dm->package_type (phydm override; differs from hal->PackageType)
    default_rf_set: int = 1     # dm->default_rf_set_8821c (SWITCH_TO_BTG=0 / WLG=1) — picks AGC diff


def _parse_board_info(t, log_map: bytes, map_valid: bool) -> tuple[bool, int, int, int]:
    """The EFUSE-parse fields with side effects: `Hal_EfuseParseBTCoexistInfo` (the only
    register touch — REG_WL_BT_PWR_CTRL for BT_FUNC_EN), board RFE type, and the RF_BT_SETTING
    antenna byte. [SRC] rtl8821c_ops.c:134-175 (BT-coex) / :376-414 (RFE) / hal_pg.h offsets."""
    board_opt = log_map[_EEPROM_RF_BOARD_OPTION]
    bt_coexist = False
    if map_valid and board_opt != 0xFF:
        pwr_ctrl = t.read32(REG_WL_BT_PWR_CTRL)
        interface_sel = (board_opt & 0xE0) >> 5          # 0x01 == combo card
        bt_coexist = interface_sel == 0x01 and bool(pwr_ctrl & _BIT_BT_FUNC_EN)

    setting = log_map[_EEPROM_RF_BT_SETTING]
    if map_valid and setting != 0xFF:
        ant_num = 1 if (setting & (1 << 0)) else 2       # BIT0: 1 => 1-Ant (Ant_x1), 0 => 2-Ant
        single_ant_path = 1 if (setting & (1 << 6)) else 0   # BIT6: RF_PATH_B(1) / RF_PATH_A(0)
    else:
        ant_num, single_ant_path = 1, 0

    rfe_type = log_map[_EEPROM_RFE_OPTION] if map_valid else 0
    return bt_coexist, rfe_type, single_ant_path, ant_num


def read_efuse(t) -> EfuseInfo:
    """[SRC] rtl8821c_read_efuse: autoload-status check, the WIFI physical dump decoded to
    the logical shadow map, then the parse chain (only BT-coex touches a register). Returns
    the parsed fields the later bring-up consumes."""
    val8 = t.read8(REG_SYS_EEPROM_CTRL)
    autoload_ok = not (val8 & _BIT_AUTOLOAD_SUS)
    _switch_efuse_bank_wifi(t)
    phys_map = read_hw_efuse(t)
    log_map = eeprom_parser(phys_map)
    map_valid = int.from_bytes(log_map[0:2], "little") == _RTL_EEPROM_ID
    bt_coexist, rfe_type, single_ant_path, ant_num = _parse_board_info(t, log_map, map_valid)
    return EfuseInfo(autoload_ok, log_map, bt_coexist, rfe_type, single_ant_path, ant_num,
                     phys_map=phys_map)


# PPG (per-package-gain) physical EFUSE offsets for kfree trim [SRC] halrf_kfree.h:69-75
_PPG_THERMAL = 0x1EF
_PPG_2G_TXAB = 0x1EE
_PPG_5G = (0x1EC, 0x1E8, 0x1E4, 0x1E0, 0x1DC)


def read_phydm_trim(t, phys_map: bytes) -> None:
    """rtw_phydm_read_efuse [SRC] hal_dm.c:1832 -> phydm thermal + power trim (kfree): read the
    PPG physical EFUSE bytes. Each odm_efuse_one_byte_read does a bank-switch read (REG 0x35);
    the byte itself is served from the cached physical map, so the only wire op is the bank read.
    [SRC] halrf_kfree.c:127 (thermal) / :154 (power: 2G then, if present, five 5G sub-bands)."""
    _switch_efuse_bank_wifi(t)              # thermal trim @ 0x1EF
    _switch_efuse_bank_wifi(t)              # power trim 2G @ 0x1EE
    if phys_map[_PPG_2G_TXAB] != 0xFF:
        for _off in _PPG_5G:
            _switch_efuse_bank_wifi(t)
