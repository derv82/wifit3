"""RTL8814AU dynamic-mechanism (phydm) init seed (M3a) — vendor faithful port.

The hal_init tail after the channel tune: a small MISC11 block then
`rtl8814_InitHalDm` [SRC rtl8814a_dm.c:203] = `dm_InitGPIOSetting` (USB) +
`rtw_phydm_init` -> `odm_dm_init`. This seeds DIG/AGC/false-alarm/CCK-PD — the
initial state the runtime DIG/AGC watchdog (a later milestone) then adapts.

Most sub-inits are read-modify-writes with fixed masks (resolved from the phydm
source). The NHM thresholds are derived from the IGI read at 0xc50 (computed, not
hardcoded). The trailing per-path RF gain-table commit (RF 0xEF page open -> RF
0x18 rows -> close) uses values computed in the RF/AGC path that are absent from
the static tables, so they are reproduced from the wire (deterministic across all
three cold boots). Verified byte-for-byte; [WIRE] cap1 frames 14379-14563.
"""
from __future__ import annotations

from .bb import _set_reg_masked as _bb32
from .rf import _rf_read, _rf_write, set_rf_masked

_RF_PATHS = ("a", "b", "c", "d")
_REG_IGI = 0x0C50           # ODM_REG(IGI_A) — IGI/initial-gain, mask 0x7F
_CCA_CAP = 14               # phydm_ccx.h


def _misc11(t) -> None:
    """[SRC] usb_halinit.c:1242-1266 — security CAM clear + a few MAC defaults."""
    t.write32(0x0670, 0xC0000000)   # invalidate_cam_all: CAMCMD poll+clear
    t.write8(0x0423, 0xFF)          # REG_HWSEQ_CTRL = 0xFF
    t.write32(0x04CC, 0x0201FFFF)   # REG_BAR_MODE_CTRL
    t.write8(0x0577, 0x03)          # REG_SECONDARY_CCA_CTRL
    t.write8(0x0652, 0x00)          # Nav limit


def _init_gpio(t) -> None:
    """[SRC] dm_InitGPIOSetting — clear GPIOSEL_ENBT (BIT5) of REG_GPIO_IO_SEL+2."""
    v = t.read8(0x0040)
    t.write8(0x0040, v & ~(1 << 5))


def _config_cck_rx_antenna(t) -> None:
    """[SRC] phydm_config_cck_rx_antenna_init — 8814A CCK 2R single-path RX."""
    t.read32(0x0804)                       # cached BB read
    _bb32(t, 0x0A00, 1 << 15, 0)           # disable CCK ant-div
    _bb32(t, 0x0A70, 1 << 7, 0)            # concurrent-CCA off
    _bb32(t, 0x0A74, 1 << 8, 0)            # RX path-div off
    _bb32(t, 0x0A14, 1 << 7, 0)            # r_en_mrc_antsel off
    _bb32(t, 0x0A20, (1 << 5) | (1 << 4), 1)   # MBC weighting
    _bb32(t, 0x0A84, 1 << 28, 1)          # 8814A "2R CCA only"
    t.read32(0x0808)
    t.read32(0x0808)
    t.read32(0x080C)


def _dig_init(t) -> int:
    """[SRC] phydm_dig_init — read the AGC-default IGI (no writes here)."""
    return t.read32(_REG_IGI) & 0x7F


def _cck_pd_init(t) -> None:
    """[SRC] phydm_cck_pd_init — CCK PD level 0 (0xa0a = 0x40)."""
    t.write8(0x0A0A, 0x40)


def _env_monitor_init(t) -> None:
    """[SRC] phydm_env_monitor_init — CCX hw-restart, NHM thresholds, CLM."""
    _bb32(t, 0x0994, 0x7, 0)              # ccx hw-restart: clear bits[2:0]
    _bb32(t, 0x0994, 1 << 8, 0)           # toggle BIT8 off
    _bb32(t, 0x0994, 1 << 8, 1)           # toggle BIT8 on
    # NHM thresholds: th[i] = ((IGI - CCA_CAP) << 1) + 4*i  [IGI-derived].
    igi = t.read32(_REG_IGI) & 0x7F
    th = [(((igi - _CCA_CAP) << 1) + 4 * i) & 0xFF for i in range(11)]
    t.write32(0x0998, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    t.write32(0x099C, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    _bb32(t, 0x09A0, 0xFF, th[8])
    _bb32(t, 0x0994, 0xFFFF0000, th[9] | th[10] << 8)
    _bb32(t, 0x0990, 0xFFFF, 0xFFFF)      # CLM setting


def _adaptivity_init(t) -> None:
    """[SRC] phydm_adaptivity_init — EDCCA thresholds (no-link 0x7f/0x7f)."""
    _bb32(t, 0x0944, (1 << 29) | (1 << 28), 1)   # adaptivity en field
    _bb32(t, 0x08A4, 0xFF, 0x7F)          # L2H EDCCA threshold
    _bb32(t, 0x08A4, 0xFF00, 0x7F)        # H2L EDCCA threshold
    _bb32(t, 0x0520, 1 << 15, 0)          # MAC: don't ignore EDCCA
    _bb32(t, 0x0524, 1 << 11, 1)          # MAC: disable EDCCA countdown


# Per-path RF AGC gain-table rows (RF regs 0x30/0x31/0x32 with the 0xEF gain page
# open). These are computed in the RF/AGC path and absent from the static tables,
# so the values are reproduced from the cold-boot wire (deterministic). Path A gets
# one extra row.
_RF_GAIN_OPEN = 0x80000                  # RF 0xEF page-select bit
_RF_GAIN_BASE = (0x30, 0x18000)
_RF_GAIN_ROWS = ((0x31, 0xBE77F), (0x32, 0x226BF))
_RF_GAIN_A_EXTRA = (0x32, 0xE26BF)


def _rf_gain_table(t) -> None:
    """[SRC] RF/AGC gain-table commit via SIPI (RF 0xEF page open -> 0x30..0x32)."""
    t.read32(0x0440)
    t.read32(0x0C1C)
    for p in _RF_PATHS:
        set_rf_masked(t, p, 0xEF, _RF_GAIN_OPEN, 1)     # open gain page
    for p in _RF_PATHS:
        _rf_write(t, p, *_RF_GAIN_BASE)                 # gain-table base row
    for p in _RF_PATHS:
        _rf_read(t, p, 0x30)                            # readback
    for p in _RF_PATHS:
        for addr, data in _RF_GAIN_ROWS:
            _rf_write(t, p, addr, data)
    _rf_write(t, "a", *_RF_GAIN_A_EXTRA)                # path-A extra row
    for p in _RF_PATHS:
        set_rf_masked(t, p, 0xEF, _RF_GAIN_OPEN, 0)     # close gain page
    # BB rx-gain index commit (0x910). The 0x1994[3:0]=0xf that follows on the
    # wire belongs to PHY_SetRFEReg8814A(TRUE) (M3b, chan.set_rfe_reg_init), not
    # to this gain commit — it is the next hal_init step, not part of InitHalDm.
    for val in (0xFC00, 0xEC00, 0x2C00, 0x2C00, 0x2C00):
        _bb32(t, 0x0910, 0xFC00, (val >> 10) & 0x3F)


def init_hal_dm(t) -> int:
    """hal_init MISC11 + rtl8814_InitHalDm (phydm DIG/AGC/false-alarm seed).

    Returns the DIG seed (the AGC-default IGI read by phydm_dig_init); the runtime watchdog
    carries it as ``cur_ig_value`` and the chip is never re-read for it.
    """
    _misc11(t)
    _init_gpio(t)
    _config_cck_rx_antenna(t)
    igi_seed = _dig_init(t)
    _cck_pd_init(t)
    _env_monitor_init(t)
    _adaptivity_init(t)
    _rf_gain_table(t)
    return igi_seed
