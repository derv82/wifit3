"""PHYDM conditional-table walker for the 8822b init tables (AGC, RF-A, RF-B).

The phy_reg and MAC tables are plain (addr, value) lists, but the AGC and RF tables embed
per-cut/rfe branches as BIT31/BIT30 marker rows. The walker mirrors
odm_read_and_config_mp_8822b_* (one per table) and check_positive (one shared evaluator).

[SRC] hal/phydm/rtl8822b/halhwimg8822b_bb.c:
  - check_positive(): builds driver1 from the chip's cut/interface/platform/package/rfe and
    keeps a row's IF-condition only when cut[27:24], package[15:12], interface[11:8] each
    match (a zero field is "don't care") AND rfe[7:0] matches exactly. cond2/3/4 are computed
    by the vendor but unused, so we evaluate cond1 alone.
  - odm_read_and_config_mp_8822b_agc_tab(): the walk loop below. BIT31 rows are positive
    markers (bits 29:28 select IF/ELSEIF=0/1, ELSE=2, ENDIF=3); BIT30 rows are negative
    conditions fed to check_positive against the preceding IF's cond1; all other rows are
    (addr, value) writes applied only while the current branch is matched.
"""
from __future__ import annotations

from dataclasses import dataclass

BIT31, BIT30, BIT29, BIT28 = 1 << 31, 1 << 30, 1 << 29, 1 << 28
COND_ELSE = 2
COND_ENDIF = 3

ODM_CUT_A = 0
ODM_ITRF_USB = 0x2   # [SRC] phydm_pre_define.h enum odm_interface
ODM_CE = 0x04        # [SRC] phydm_types.h DM_ODM_SUPPORT_TYPE platform bit


@dataclass(frozen=True)
class PhyCondConfig:
    """The chip discriminators check_positive evaluates a table row against."""
    cut: int              # dm->cut_version (ODM_CUT_D = 3 on this card)
    rfe: int              # dm->rfe_type (EFUSE-read; 3 on this card)
    package: int = 0      # dm->package_type (0 -> treated as 15)
    interface: int = ODM_ITRF_USB
    platform: int = ODM_CE


def check_positive(cfg: PhyCondConfig, cond1: int) -> bool:
    """[SRC] check_positive: cond1 vs driver1 (cut/package/interface value checks + rfe exact)."""
    cut = 15 if cfg.cut == ODM_CUT_A else cfg.cut
    pkg = 15 if cfg.package == 0 else cfg.package
    driver1 = ((cut << 24) | ((cfg.interface & 0xF0) << 16) | (cfg.platform << 16)
               | (pkg << 12) | ((cfg.interface & 0x0F) << 8) | cfg.rfe) & 0xFFFFFFFF
    if (cond1 & 0x0F000000) and (cond1 & 0x0F000000) != (driver1 & 0x0F000000):
        return False
    if (cond1 & 0x0000F000) and (cond1 & 0x0000F000) != (driver1 & 0x0000F000):
        return False
    if (cond1 & 0x00000F00) and (cond1 & 0x00000F00) != (driver1 & 0x00000F00):
        return False
    return (cond1 & 0xFF) == (driver1 & 0xFF)


def walk(table, cfg: PhyCondConfig, apply) -> None:
    """Run odm_read_and_config_mp_8822b_*: call apply(addr, value) for each in-branch row."""
    matched, skipped, pre_cond1 = True, False, 0
    for v1, v2 in table:
        if v1 & (BIT31 | BIT30):
            if v1 & BIT31:                                  # positive marker
                c_cond = (v1 & (BIT29 | BIT28)) >> 28
                if c_cond == COND_ENDIF:
                    matched, skipped = True, False
                elif c_cond == COND_ELSE:
                    matched = not skipped
                else:                                       # IF / ELSE IF: remember the cond
                    pre_cond1 = v1
            else:                                           # BIT30: negative condition
                if not skipped:
                    if check_positive(cfg, pre_cond1):
                        matched, skipped = True, True
                    else:
                        matched, skipped = False, False
                else:
                    matched = False
        elif matched:
            apply(v1, v2)
