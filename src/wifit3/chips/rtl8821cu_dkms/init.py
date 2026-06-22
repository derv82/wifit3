"""RTL8821CU pre-power-on system config — the HALMAC init run before the card-enable
power sequence.

``pre_init_system_cfg`` is the first thing ``rtw_halmac_poweron`` does (hal_halmac.c:2721),
before ``mac_pwr_switch`` runs the power sequence: it clears RSV_CTRL, sets the pin-mux
(PAD_CTRL1 / LED_CFG / GPIO_MUXCFG), disables BB/RF (so the power sequence brings them up
from a known-off state), and probes the test-mode bit. The BB/RF disable goes through the
generic ``enable_bb_rf`` helper that HALMAC's set_hw_value dispatches to.

Ported from:
  [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_init_8821c.c:975  pre_init_system_cfg_8821c
  [SRC] hal/halmac/halmac_88xx/halmac_cfg_wmac_88xx.c:637            enable_bb_rf_88xx
  [SRC] hal/halmac/halmac_88xx/halmac_common_88xx.c:533             set_hw_value(HW_EN_BB_RF)
Register addrs [SRC] hal/halmac/halmac_reg2.h ; BIT()s pasted from the source verbatim.
"""
from __future__ import annotations

REG_SYS_FUNC_EN = 0x0002        # [SRC] halmac_reg2.h:45
REG_RSV_CTRL = 0x001C           # [SRC] halmac_reg2.h:149
REG_RF_CTRL = 0x001F            # [SRC] halmac_reg2.h:166
REG_GPIO_MUXCFG = 0x0040        # [SRC] halmac_reg2.h:328
REG_LED_CFG = 0x004C            # [SRC] halmac_reg2.h:365
REG_PAD_CTRL1 = 0x0064          # [SRC] halmac_reg2.h:388
REG_WLRF1 = 0x00EC              # [SRC] halmac_reg2.h:798
REG_SYS_CFG1 = 0x00F0           # [SRC] halmac_reg2.h:814
REG_SYS_CFG2 = 0x00FC           # [SRC] halmac_reg2.h:817
REG_USB_DMA_AGG_TO = 0xFE5B     # [SRC] halmac_reg2.h:8540


def _enable_bb_rf(t, enable: bool) -> None:
    """Toggle BB (SYS_FUNC_EN BIT0/1), RF (RF_CTRL BIT0/1/2) and the WLRF1 RF-clock bits
    (BIT24/25/26) together. [SRC] enable_bb_rf_88xx halmac_cfg_wmac_88xx.c:637.
    (8821c skips board_rf_fine_tune — that arm is 8822B-only.)"""
    if enable:
        t.write8(REG_SYS_FUNC_EN, t.read8(REG_SYS_FUNC_EN) | (1 << 0) | (1 << 1))
        t.write8(REG_RF_CTRL, t.read8(REG_RF_CTRL) | (1 << 0) | (1 << 1) | (1 << 2))
        t.write32(REG_WLRF1, t.read32(REG_WLRF1) | (1 << 24) | (1 << 25) | (1 << 26))
    else:
        t.write8(REG_SYS_FUNC_EN, t.read8(REG_SYS_FUNC_EN) & ~((1 << 0) | (1 << 1)))
        t.write8(REG_RF_CTRL, t.read8(REG_RF_CTRL) & ~((1 << 0) | (1 << 1) | (1 << 2)))
        t.write32(REG_WLRF1, t.read32(REG_WLRF1) & ~((1 << 24) | (1 << 25) | (1 << 26)))


def pre_init_system_cfg(t) -> None:
    """[SRC] pre_init_system_cfg_8821c halmac_init_8821c.c:975 (USB path)."""
    t.write8(REG_RSV_CTRL, 0)

    if t.read8(REG_SYS_CFG2 + 3) == 0x20:
        t.write8(REG_USB_DMA_AGG_TO, t.read8(REG_USB_DMA_AGG_TO) | (1 << 4))

    v = t.read32(REG_PAD_CTRL1)
    t.write32(REG_PAD_CTRL1, v | (1 << 28) | (1 << 29))

    v = t.read32(REG_LED_CFG)
    t.write32(REG_LED_CFG, v & ~((1 << 25) | (1 << 26)))

    v = t.read32(REG_GPIO_MUXCFG)
    t.write32(REG_GPIO_MUXCFG, v | (1 << 2))

    _enable_bb_rf(t, False)

    t.read8(REG_SYS_CFG1 + 2)       # test-mode probe (BIT4); vendor only logs, no state change
