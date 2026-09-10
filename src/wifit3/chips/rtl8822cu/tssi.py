"""RTL8822C TSSI DC calibration.

Nulls the DC offset the TSSI power detector sees, per RF path, by stepping the offset until
the detector reads mid-scale. [SRC hal/phydm/halrf/rtl8822c/halrf_tssi_8822c.c:900]
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .firmware import _ltecoex_read, _ltecoex_write
from .phy import (
    DBGPORT_PRI_2,
    MASKDWORD,
    get_bb_reg,
    get_bb_dbg_port_val,
    set_bb_reg,
    set_bb_dbg_port,
    set_rf_reg,
)
from .transport import RTL8822CUTransport

_BB_BACKUP = (0x1800, 0x4100, 0x0820, 0x1E2C, 0x1D08, 0x1C3C, 0x2DBC, 0x1E70)
_TSSI_SETTING = (0x1830, 0x4130)
_DC_OFFSET = (0x189C, 0x419C)
_PATH_SETTING = (0x1800, 0x4100)
_TSSI_COUNTER = (0x18A4, 0x41A4)
_TSSI_ENABLE = (0x180C, 0x410C)
_DEBUG_PORT = (0x0930, 0x0B30)
_ADDR_D = ((0x18A8, 0xFF000000), (0x1EEC, 0x3FC00000))
_ADDR_CCK_D = ((0x18E8, 0x01FE0000), (0x1EF0, 0x0001FE00))

_DC_OFFSET_MASK = 0x0003FF00
_LTECOEX_GNT = 0x38

# The 16-step gain ramp clocked into tssi_setting. Five steps differ between bands.
_GAIN_RAMP_2G = (0x700B8041, 0x701F0044, 0x702F0044, 0x703F0044, 0x704F0044, 0x705B8041,
                 0x706F0044, 0x707B8041, 0x708B8041, 0x709B8041, 0x70AB8041, 0x70BB8041,
                 0x70CB8041, 0x70DB8041, 0x70EB8041, 0x70FB8041)
_GAIN_RAMP_5G = (0x700B8041, 0x701F0042, 0x702F0042, 0x703F0042, 0x704F0042, 0x705B8041,
                 0x706F0042, 0x707B8041, 0x708B8041, 0x709B8041, 0x70AB8041, 0x70BB8041,
                 0x70CB8041, 0x70DB8041, 0x70EB8041, 0x70FB8041)


@dataclass
class GntState:
    """dpk_info->gnt_control / gnt_value, saved across one btc_set_gnt_wl_bt pair."""
    control: int = 0
    value: int = 0


def _btc_set_gnt_wl_bt(t: RTL8822CUTransport, gnt: GntState, *, before_k: bool) -> None:
    """btc_set_gnt_wl_bt_8822c: take the WL/BT antenna grant away from BT for the duration of a
    calibration, then hand it back. [SRC halrf_dpk_8822c.c:121]"""
    if before_k:
        gnt.control = get_bb_reg(t, 0x0070)
        gnt.value = _ltecoex_read(t, _LTECOEX_GNT)
        set_bb_reg(t, 0x0070, 1 << 26, 0x1)
        value = _ltecoex_read(t, _LTECOEX_GNT)
        _ltecoex_write(t, _LTECOEX_GNT, (value & ~0xFF00) | (0x77 << 8))
    else:
        _ltecoex_write(t, _LTECOEX_GNT, gnt.value)
        set_bb_reg(t, 0x0070, MASKDWORD, gnt.control)


def _disable_tssi(t: RTL8822CUTransport) -> None:
    """halrf_disable_tssi_8822c. [SRC halrf_tssi_8822c.c:1763]"""
    for counter, enable, avg in ((0x18A4, 0x180C, 0x18A0), (0x41A4, 0x410C, 0x41A0)):
        set_bb_reg(t, counter, 0x0003E000, 0x0)
        set_bb_reg(t, enable, 0x08000000, 0x0)
        set_bb_reg(t, enable, 0x40000000, 0x0)
        set_bb_reg(t, counter, 0x10000000, 0x0)
        set_bb_reg(t, 0x1E7C, 0x40000000, 0x0)
        set_bb_reg(t, avg, 0x0000007F, 0x0)
    set_bb_reg(t, 0x1C38, MASKDWORD, 0xFFA1005E)


def _read_dbg_port(t: RTL8822CUTransport, path: int) -> int:
    """Latch the TSSI accumulator through the BB debug port and read it back. Releasing the
    port is software-only on 8822C. [SRC halrf_tssi_8822c.c:1154-1160]"""
    set_bb_dbg_port(t, DBGPORT_PRI_2, _DEBUG_PORT[path])
    set_bb_reg(t, _TSSI_COUNTER[path], 0x10000000, 0x0)
    set_bb_reg(t, _TSSI_COUNTER[path], 0x10000000, 0x1)
    return get_bb_dbg_port_val(t) & 0x000003FF


def _dck_head(t: RTL8822CUTransport, gnt: GntState, path: int, *, band_2g: bool) -> None:
    """Arm the TSSI chain for one measurement attempt: gain ramp, RF front-end, path select,
    and (2.4 GHz only) the BT grant plus (5 GHz only) the OFDM packet template."""
    set_bb_reg(t, 0x1C38, MASKDWORD, 0xF7D5005E)
    set_bb_reg(t, 0x1D58, 0x00000008, 0x1)
    set_bb_reg(t, 0x1D58, 0x00000FF0, 0xFF)
    set_bb_reg(t, 0x1A00, 0x00000003, 0x2)
    for value in (_GAIN_RAMP_2G if band_2g else _GAIN_RAMP_5G):
        set_bb_reg(t, _TSSI_SETTING[path], MASKDWORD, value)

    def _path_select() -> None:
        # 0xc00 does not fit the 10-bit field and lands as 0, the same value the 5 GHz
        # branch writes outright. [SRC halrf_tssi_8822c.c:992]
        set_bb_reg(t, _DC_OFFSET[path], _DC_OFFSET_MASK, 0xC00 if band_2g else 0x0)
        set_bb_reg(t, 0x0820, 0x00000003, path + 1)
        set_bb_reg(t, 0x1E2C, MASKDWORD, 0xE4E40000)
        set_bb_reg(t, 0x1E28, 0x0000000F, 0x3)
        set_bb_reg(t, _PATH_SETTING[path], 0x000FFFFF, 0x33312)
        set_bb_reg(t, _PATH_SETTING[path], 0x80000000, 0x1)

    if not band_2g:
        _path_select()
    set_bb_reg(t, _TSSI_COUNTER[path], 0xE0000000, 0x0)
    time.sleep(0.0002)
    if band_2g:
        set_rf_reg(t, path, 0x7F, 0x00002, 0x1)
        set_rf_reg(t, path, 0x65, 0x03000, 0x3)
        set_rf_reg(t, path, 0x67, 0x0000C, 0x3)
        set_rf_reg(t, path, 0x67, 0x000C0, 0x0)
        set_rf_reg(t, path, 0x6E, 0x001E0, 0x0)
    else:
        set_rf_reg(t, path, 0x7F, 0x00100, 0x1)
        set_rf_reg(t, path, 0x65, 0x03000, 0x3)
        set_rf_reg(t, path, 0x67, 0x00003, 0x3)
        set_rf_reg(t, path, 0x67, 0x00030, 0x2)
        set_rf_reg(t, path, 0x6F, 0x001E0, 0x0)
    set_bb_reg(t, _TSSI_ENABLE[path], 0x08000000, 0x1)
    set_bb_reg(t, _TSSI_ENABLE[path], 0x40000000, 0x1)
    set_bb_reg(t, 0x1D08, 0x00000001, 0x1)
    set_bb_reg(t, 0x1CA4, 0x00000001, 0x1)
    set_bb_reg(t, 0x1B00, 0x00000006, path)
    set_bb_reg(t, 0x1BCC, 0x0000003F, 0x3F)
    set_rf_reg(t, path, 0xDE, 0x10000, 0x1)
    set_rf_reg(t, path, 0x56, 0x000FF, 0x0)
    if band_2g:
        _btc_set_gnt_wl_bt(t, gnt, before_k=True)
        _path_select()
    else:                                   # OFDM packet template the detector measures
        set_bb_reg(t, 0x0900, 0x00000004, 0x1)
        set_bb_reg(t, 0x0900, 0x30000000, 0x2)
        set_bb_reg(t, 0x0908, 0x00FFFFFF, 0x21B6B)
        set_bb_reg(t, 0x090C, 0x00FFFFFF, 0x800006)
        set_bb_reg(t, 0x0910, 0x00FFFFFF, 0x13600)
        set_bb_reg(t, 0x0914, 0x1FFFFFFF, 0x6000FA)
        set_bb_reg(t, 0x0938, 0x0000FFFF, 0x4B0F)
        set_bb_reg(t, 0x0940, MASKDWORD, 0x4EE33E41)
        set_bb_reg(t, 0x0A58, 0x003F8000, 0x2C)
    set_bb_reg(t, 0x1E70, 0x00000004, 0x1)
    set_bb_reg(t, _TSSI_COUNTER[path], 0x10000000, 0x1)


def _teardown(t: RTL8822CUTransport, path: int, *, band_2g: bool) -> None:
    """Per-path teardown. [SRC halrf_tssi_8822c.c:1055 / :1204]"""
    set_bb_reg(t, 0x1E70, 0x0000000F, 0x2)
    set_rf_reg(t, path, 0xDE, 0x10000, 0x0)
    set_bb_reg(t, 0x1BCC, 0x0000003F, 0x0)
    set_bb_reg(t, 0x1B00, 0x00000006, path)
    set_bb_reg(t, 0x1CA4, 0x00000001, 0x0)
    set_bb_reg(t, 0x1D08, 0x00000001, 0x0)
    set_rf_reg(t, path, 0x7F, 0x00002 if band_2g else 0x00100, 0x0)
    time.sleep(0.0001)
    set_bb_reg(t, _TSSI_ENABLE[path], 0x08000000, 0x0)
    set_bb_reg(t, _TSSI_ENABLE[path], 0x40000000, 0x0)
    time.sleep(0.0001)
    set_bb_reg(t, _TSSI_COUNTER[path], 0x10000000, 0x0)
    time.sleep(0.0001)
    set_bb_reg(t, 0x1D58, 0x00000008, 0x0)
    set_bb_reg(t, 0x1D58, 0x00000FF0, 0x0)
    set_bb_reg(t, 0x1A00, 0x00000003, 0x0)


def tssi_dck(t: RTL8822CUTransport, channel: int) -> None:
    """halrf_tssi_dck_8822c: per path, seed the TSSI DC offset from one detector reading, then
    nudge it by 4 until the detector settles mid-scale (0x1ff..0x202). At cold boot no channel
    has been tuned yet, so ``channel`` is 0 and the 5 GHz branch runs; the 2.4 GHz branch is
    ported but untested. [SRC halrf_tssi_8822c.c:900]"""
    band_2g = 1 <= channel <= 14
    gnt = GntState()
    backup = [get_bb_reg(t, address) for address in _BB_BACKUP]

    for (cck_addr, cck_mask), (d_addr, d_mask) in zip(_ADDR_CCK_D, _ADDR_D):
        set_bb_reg(t, cck_addr, cck_mask, 0x0)
        set_bb_reg(t, d_addr, d_mask, 0x0)
    _disable_tssi(t)

    for path in (0, 1):
        for _attempt in range(3):
            _dck_head(t, gnt, path, band_2g=band_2g)
            reg = _read_dbg_port(t, path)
            # Mirror the detector reading about mid-scale to get the offset that cancels it.
            reg = 1024 - (((reg - 512) * 4) & 0x000003FF) + (0 if band_2g else 5)
            set_bb_reg(t, _DC_OFFSET[path], _DC_OFFSET_MASK, reg & 0x03FF)
            settled = False
            for _ in range(3):
                dck_check = _read_dbg_port(t, path)
                if dck_check < 0x1FF:
                    reg = 0x3FF if reg >= 0x3FB else reg + 4
                elif dck_check > 0x202:
                    reg = 0 if reg <= 4 else reg - 4
                else:
                    settled = True
                    break
                set_bb_reg(t, _DC_OFFSET[path], _DC_OFFSET_MASK, reg)
            if band_2g:
                _btc_set_gnt_wl_bt(t, gnt, before_k=False)
            if settled:
                break
        _teardown(t, path, band_2g=band_2g)

    for address, value in zip(_BB_BACKUP, backup):
        set_bb_reg(t, address, MASKDWORD, value)
