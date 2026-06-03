"""RTL8814AU baseband (BB) configuration (M2b) — vendor faithful port.

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

# --- phydm conditional-table walker -----------------------------------------
# The PHY_REG / AGC_TAB tables interleave (addr, value) data rows with condition
# control words. A condition is a positive word (BIT31, the IF/ELSE-IF/ELSE/ENDIF
# selector in bits[29:28]) optionally followed by a negative word (BIT30). Data
# rows are emitted only while the current IF branch matches the chip. The match
# (check_positive) compares the IF word's low 28 bits against ``driver1``.
# [SRC] halhwimg8814a_bb.c check_positive:31 + odm_read_and_config_mp_8814a_*.
_BIT31, _BIT30, _BIT29, _BIT28 = 1 << 31, 1 << 30, 1 << 29, 1 << 28
_COND_ELSE, _COND_ENDIF = 2, 3  # [SRC] phydm_types.h:301 (IF=0, ELSE_IF=1)

# This card: cut=A_CUT, package_type=0, interface=USB. check_positive folds
# cut==ODM_CUT_A -> 15 and package_type==0 -> 15 before building driver1; both are
# fixed for the 8814AU and (brute-forced) don't gate this card's taken path, so
# only rfe_type varies and it comes from efuse (efuse.read_chip_params). ODM_ITRF_USB
# =0x2, ODM_CE platform=0x8. [WIRE] driver1 0x0F08F201 (rfe=1) reproduces all 2102
# cold-boot BB writes; efuse independently decodes rfe_type=1 (verify_efuse_pcap.py).
_CUT_FOR_PARA = 0xF       # A_CUT -> 15
_PKG_FOR_PARA = 0xF       # package_type 0 -> 15
_SUPPORT_INTERFACE = 0x2  # ODM_ITRF_USB
_SUPPORT_PLATFORM = 0x8   # ODM_CE


def _build_driver1(rfe_type: int) -> int:
    """[SRC] check_positive preamble — assemble the match word from chip params."""
    return (
        (_CUT_FOR_PARA & 0xFF) << 24
        | (_SUPPORT_INTERFACE & 0xF0) << 16
        | _SUPPORT_PLATFORM << 16
        | (_PKG_FOR_PARA & 0xF) << 12
        | (_SUPPORT_INTERFACE & 0x0F) << 8
        | (rfe_type & 0xFF)
    )


def _check_positive(driver1: int, cond1: int) -> bool:
    """[SRC] check_positive — only cond1 (the IF word) is matched against driver1.

    Cut[27:24], package[15:12] and interface[11:8] nibbles are checked only when
    non-zero in the condition; the rfe byte[7:0] must always match. Bits[31:28]
    (the BIT31/30 + selector) are masked off by the nibble comparisons.
    """
    if (cond1 & 0x0F000000) and (cond1 & 0x0F000000) != (driver1 & 0x0F000000):
        return False
    if (cond1 & 0x0000F000) and (cond1 & 0x0000F000) != (driver1 & 0x0000F000):
        return False
    if (cond1 & 0x00000F00) and (cond1 & 0x00000F00) != (driver1 & 0x00000F00):
        return False
    return (cond1 & 0xFF) == (driver1 & 0xFF)


def _walk_table(t, table, driver1: int) -> None:
    """[SRC] odm_read_and_config_mp_8814a_phy_reg / _agc_tab.

    Data rows write32(addr, value) (odm_set_bb_reg with MASKDWORD). Neither 8814A
    table contains the 0xf9..0xfe delay pseudo-addresses, so every data row is a
    plain register write.
    """
    i, n = 0, len(table)
    is_matched, is_skipped, pre_v1 = True, False, 0
    while i + 1 < n:
        v1, v2 = table[i], table[i + 1]
        if v1 & (_BIT31 | _BIT30):
            if v1 & _BIT31:                       # positive condition
                c_cond = (v1 & (_BIT29 | _BIT28)) >> 28
                if c_cond == _COND_ENDIF:
                    is_matched, is_skipped = True, False
                elif c_cond == _COND_ELSE:
                    is_matched = not is_skipped
                else:                             # IF / ELSE IF
                    pre_v1 = v1
            elif v1 & _BIT30:                     # negative condition (pairing word)
                if not is_skipped:
                    if _check_positive(driver1, pre_v1):
                        is_matched, is_skipped = True, True
                    else:
                        is_matched, is_skipped = False, False
                else:
                    is_matched = False
        else:
            if is_matched:
                t.write32(v1, v2)
        i += 2


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

    (PHY_REG_MP is skipped: mp_mode is off in the captured run.)
    """
    driver1 = _build_driver1(rfe_type)
    _walk_table(t, PHY_REG, driver1)
    _walk_table(t, AGC_TAB, driver1)


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
