"""RTL8814AU baseband (BB) configuration (M2b) — port of the vendor stack.

`PHY_BBConfig8814` [SRC rtl8814a_phycfg.c:334] brings the baseband up:

    prefix              SYS_FUNC_EN |= FEN_USBA; 0x1002 BB global reset;
                        RF_CTRL0/1/3 power-on (paths A..D)
    phy_BB8814A_Config_ParaFile   PHY_REG table then AGC_TAB table, each applied
                        through the phydm conditional walker
    suffix              crystal-cap set + TRX-path config            (later sub-step)

`phy_InitBBRFRegisterDefinition` runs first in the vendor function but only fills
the in-RAM PHYRegDef offset table (consumed by RF config), so it emits no USB I/O
and is deferred to the RF milestone.

Verified byte-for-byte against the cold-boot capture; [WIRE] cap1 frames 7105+.
"""
from __future__ import annotations

from . import constants as C
from .bb_agc_tab_tbl import AGC_TAB
from .bb_phy_reg_tbl import PHY_REG
from .phy_cond import build_driver1, walk_table


def _set_reg_masked(t, addr: int, mask: int, val: int) -> None:
    """odm_set_mac_reg / phy_set_bb_reg — masked read-modify-write32.

    ``val`` is the field value; it is shifted to the mask's lowest set bit.
    """
    shift = (mask & -mask).bit_length() - 1
    v = t.read32(addr)
    v = (v & ~mask) | ((val << shift) & mask)
    t.write32(addr, v & 0xFFFFFFFF)


def _bb_config_prefix(t) -> None:
    """[SRC] PHY_BBConfig8814 lines 345..363 — enable BB/RF analog + power on RF.

    REG_SYS_FUNC_EN |= FEN_USBA (8814AU); 0x1002 |= FEN_BB_GLB_RSTn | FEN_BBRSTB
    (BB global reset, same as 8812); then RF_CTRL0/1/3 = 0x07 to power on the
    SDM/RST/EN of RF paths A, B+C, and D.
    """
    v = t.read8(C.REG_SYS_FUNC_EN)
    t.write8(C.REG_SYS_FUNC_EN, v | C.FEN_USBA)

    v = t.read8(C.REG_BB_GLB_RST)
    t.write8(C.REG_BB_GLB_RST, v | C.FEN_BB_GLB_RSTn | C.FEN_BBRSTB)

    t.write8(C.REG_RF_CTRL0, C.RF_POWER_ON)        # PathA
    t.write16(C.REG_RF_CTRL1, 0x0707)              # PathB + PathC
    t.write8(C.REG_RF_CTRL3, C.RF_POWER_ON)        # PathD


def _bb_config_parafile(t, rfe_type: int) -> None:
    """[SRC] phy_BB8814A_Config_ParaFile — PHY_REG then AGC_TAB via the walker.

    Data rows are write32(addr, value) (odm_set_bb_reg with MASKDWORD); neither
    8814A table contains the 0xf9..0xfe delay pseudo-addresses, so every data row
    is a plain register write. (PHY_REG_MP is skipped: mp_mode is off here.)
    """
    driver1 = build_driver1(rfe_type)
    walk_table(PHY_REG, driver1, t.write32)
    walk_table(AGC_TAB, driver1, t.write32)


def _set_crystal_cap(t, crystal_cap: int) -> None:
    """[SRC] phydm_set_crystal_cap_reg (8814A branch) — 0x2C[26:15] = crystal_cap.

    8814A packs the 6-bit cap twice: reg_val = cap | (cap << 6), into mask
    0x07FF8000. crystal_cap comes from efuse EEPROM_XTAL_8814A.
    """
    cap = crystal_cap & 0x3F
    reg_val = cap | (cap << 6)
    _set_reg_masked(t, C.REG_XTAL_CTRL, C.CRYSTAL_CAP_MASK, reg_val)


def _config_trx_path(t) -> None:
    """[SRC] _rtw_config_trx_path_8814a — CCK path selection (same for all rf_type).

    Disable 2R CCA, pathB TX on (A/C/D off), pathB RX.
    """
    _set_reg_masked(t, C.rCCK0_FalseAlarmReport, (1 << 18) | (1 << 22), 0)
    _set_reg_masked(t, C.rCCK_RX_Jaguar, 0xF0000000, 0x4)   # pathB tx on
    _set_reg_masked(t, C.rCCK_RX_Jaguar, 0x0F000000, 0x5)   # pathB rx


def phy_bb_config(t, rfe_type: int, crystal_cap: int) -> None:
    """[SRC] PHY_BBConfig8814 — baseband bring-up: prefix, BB/AGC tables, suffix.

    ``rfe_type`` (phy_cond walker discriminator) and ``crystal_cap`` come from the
    efuse read (``efuse.read_chip_params``).
    """
    _bb_config_prefix(t)
    _bb_config_parafile(t, rfe_type)
    _set_crystal_cap(t, crystal_cap)
    _config_trx_path(t)
