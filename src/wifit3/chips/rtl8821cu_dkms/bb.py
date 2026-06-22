"""RTL8821CU BB (baseband) init — the BB half of ``rtl8821c_phy_init``.

[SRC] hal/rtl8821c/rtl8821c_phy.c: ``init_bb_rf`` (enable BB+RF), ``config_phydm_parameter_init
_8821c`` PRE/POST setting, and ``_init_phy_parameter_bb`` (the PHY_REG then AGC PHYDM tables).
The BB tables carry cut/rfe/package conditional rows, so they run through ``phy_cond.walk``
(``odm_read_and_config_mp_8821c_*``) rather than a flat loop. A row write is ``odm_config_bb_phy
_8821c`` [SRC] phydm_regconfig8821c.c:171 — a full-dword BB write, except addresses 0xF9-0xFE
which are pure delays (no register I/O).
"""
from __future__ import annotations

from . import phy_cond
from .bb_phy_reg_tbl import PHY_REG_TBL

# --- registers / bits [SRC] halmac_reg_8821c.h, halmac_bit_8821c.h ----------
REG_SYS_FUNC_EN = 0x0002
REG_RF_CTRL = 0x001F
REG_WLRF1 = 0x00EC
_BIT_FEN_USBA = 1 << 2              # :55
_BIT_FEN_BB_GLB_RSTN = 1 << 1      # :56
_BIT_FEN_BBRSTB = 1 << 0           # :57
_BIT_RF_SDMRSTB = 1 << 2           # :371
_BIT_RF_RSTB = 1 << 1              # :372
_BIT_RF_EN = 1 << 0                # :373
_RF_ON = _BIT_RF_EN | _BIT_RF_RSTB | _BIT_RF_SDMRSTB

R_0x808 = 0x0808                   # OFDM/CCK block enable [SRC] phydm_hal_api8821c.c
_MASKDWORD = 0xFFFFFFFF

_DELAY_LO, _DELAY_HI = 0xF9, 0xFE  # 0xF9-0xFE rows are delays, not registers


def set_bb_reg(t, addr: int, mask: int, val: int) -> None:
    """rtl8821c_write_bb_reg [SRC] rtl8821c_phy.c:347 — full-dword writes go straight out; a
    partial mask is read-modify-write, shifting val to the mask's lowest set bit."""
    if mask != _MASKDWORD:
        shift = (mask & -mask).bit_length() - 1
        val = (t.read32(addr) & ~mask) | ((val << shift) & mask)
    t.write32(addr, val)


def init_bb_rf(t) -> None:
    """init_bb_rf [SRC] rtl8821c_phy.c:57 — enable the BB analog (USBA), pulse the BB global +
    BB reset bits, then power the path-A RF on (RF_CTRL + WLRF1 BTG)."""
    val8 = t.read8(REG_SYS_FUNC_EN) | _BIT_FEN_USBA          # IS_HARDWARE_TYPE_8821CU
    t.write8(REG_SYS_FUNC_EN, val8)
    val8 |= _BIT_FEN_BB_GLB_RSTN | _BIT_FEN_BBRSTB
    t.write8(REG_SYS_FUNC_EN, val8)
    val8 &= ~(_BIT_FEN_BB_GLB_RSTN | _BIT_FEN_BBRSTB)
    t.write8(REG_SYS_FUNC_EN, val8)
    val8 |= _BIT_FEN_BB_GLB_RSTN | _BIT_FEN_BBRSTB
    t.write8(REG_SYS_FUNC_EN, val8)
    t.write8(REG_RF_CTRL, _RF_ON)                            # 0x1F = path-A RF power on
    t.write8(REG_WLRF1 + 3, _RF_ON)                          # 0xEF = BTG RF power on


def phy_parameter_init(t, post: bool) -> None:
    """config_phydm_parameter_init_8821c [SRC] phydm_hal_api8821c.c — gate the OFDM/CCK blocks
    in 0x808 BIT28|29 (off before the tables, on after); POST also caches three AGC regs."""
    set_bb_reg(t, R_0x808, (1 << 28) | (1 << 29), 0x3 if post else 0x0)
    if post:
        t.read32(0x0A24)
        t.read32(0x0A28)
        t.read32(0x0AAC)


def _apply_phy(t, addr: int, val: int) -> None:
    """odm_config_bb_phy_8821c [SRC] phydm_regconfig8821c.c:171 — 0xF9-0xFE are delay opcodes
    (no I/O during replay); every other row is a full-dword BB write."""
    if _DELAY_LO <= addr <= _DELAY_HI:
        return
    set_bb_reg(t, addr, _MASKDWORD, val)


def phy_bb_config(t, cfg: phy_cond.PhyCondConfig) -> None:
    """_init_phy_parameter_bb step 1 — apply the PHY_REG table via the conditional walker."""
    phy_cond.walk(PHY_REG_TBL, cfg, lambda addr, val: _apply_phy(t, addr, val))
