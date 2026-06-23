"""RTL8821CU PHYDM dynamic-mechanism init — ``rtl8821c_phy_init_haldm``.

[SRC] hal/rtl8821c/rtl8821c_dm.c:174 ``rtl8821c_phy_init_haldm`` -> ``rtw_phydm_init``
(hal_dm.c:1594) -> ``odm_dm_init`` (phydm.c:1786). ``odm_dm_init`` calls ~35 sub-inits; on this
1T1R 8821C most are software-only state (no register I/O — not ported), and a handful write/read
BB regs. We port the wire-touching ones in the order the cold-boot capture shows.

Two traps this phase carries (vs the flat tables already done): the writes derive from chip/EFUSE
state (port the computing logic, not a transcription), and the wire order is NOT the source
call-list order (e.g. 0x19a8 is written by ``phydm_init_soft_ml_setting``, reached from
``phydm_common_info_self_init``, so it lands right after the opening CCK reads).

Wire-silent for this card, in call order before the first op (verified against source, not ported):
``halrf_init`` (every sub-fn IC-gated to non-8821C or mp_mode-gated), ``phydm_supportability_init``,
``phydm_pause_func_init``, ``phydm_rfe_init`` — none touch a register here. No IQK/calibration in
this window (triggered later, on channel-set / watchdog).
"""
from __future__ import annotations

from dataclasses import dataclass

from .bb import set_bb_reg

_MASKDWORD = 0xFFFFFFFF

# --- phydm_common_info_self_init register I/O [SRC] phydm.c:238 -------------
R_0xa9c = 0x0A9C                   # CCK new-AGC check [SRC] phydm.c phydm_cck_new_agc_chk
_BIT_CCK_NEW_AGC = 1 << 17
R_0x804 = 0x0804                   # CCK_RPT_FORMAT [SRC] phydm_regdefine11ac.h:35
_BIT_CCK_RPT_FORMAT = 1 << 16      # [SRC] phydm_regdefine11ac.h:104
R_0x808 = 0x0808                   # BB_RX_PATH [SRC] phydm_regdefine11ac.h:36
_MASK_BB_RX_PATH = 0xF             # [SRC] phydm_regdefine11ac.h:105
R_0x19a8 = 0x19A8                  # soft-ML setting [SRC] phydm_soml.c phydm_init_soft_ml_setting
_SOML_MASK = (1 << 31) | (1 << 30) | (1 << 29) | (1 << 28)
_SOML_VAL = 0xD


def get_bb_reg(t, addr: int, mask: int) -> int:
    """odm_get_bb_reg — full-dword read masked + right-shifted to the mask's lowest set bit."""
    shift = (mask & -mask).bit_length() - 1
    return (t.read32(addr) & mask) >> shift


@dataclass
class DmState:
    """PHYDM ``dm_struct`` state read/computed during ``odm_dm_init`` that later sub-inits key
    on (e.g. ``cck_new_agc`` selects CCK-PD register addresses). Filled incrementally."""
    cck_new_agc: bool = False
    is_cck_high_power: bool = False
    rf_path_rx_enable: int = 0


def _common_info_self_init(t, info, st: DmState) -> None:
    """phydm_common_info_self_init [SRC] phydm.c:238. ``phydm_init_cck_setting`` reads the
    CCK new-AGC flag (0xa9c) + the CCK report format (0x804); its CCK rx-antenna/path/lna/rssi
    helpers are all 1SS- or non-8821C-gated no-ops here. Then the BB_RX_PATH read (0x808) and
    ``phydm_init_soft_ml_setting`` (0x19a8). ``phydm_trx_antenna_setting_init`` is a 1SS no-op."""
    st.cck_new_agc = bool(get_bb_reg(t, R_0xa9c, _BIT_CCK_NEW_AGC))
    st.is_cck_high_power = bool(get_bb_reg(t, R_0x804, _BIT_CCK_RPT_FORMAT))
    st.rf_path_rx_enable = get_bb_reg(t, R_0x808, _MASK_BB_RX_PATH)
    set_bb_reg(t, R_0x19a8, _SOML_MASK, _SOML_VAL)


def phy_init_haldm(t, info) -> DmState:
    """rtl8821c_phy_init_haldm [SRC] rtl8821c_dm.c:174 -> rtw_phydm_init -> odm_dm_init. The
    8821C path of ``odm_dm_init``, wire-touching sub-inits only, in capture order. Returns the
    accumulated ``DmState`` (callers downstream — channel tune — may need it)."""
    st = DmState()
    _common_info_self_init(t, info, st)
    return st
