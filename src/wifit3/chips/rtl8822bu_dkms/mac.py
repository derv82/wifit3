"""RTL8822BU MAC power-on — pre-init system cfg, the HALMAC power switch, init system cfg.

``rtw_halmac_poweron`` [SRC] hal/hal_halmac.c:2705 drives three steps:
  1. ``pre_init_system_cfg_8822b`` — RSV_CTRL clear, PIN-mux, BB/RF disabled for power-on.
  2. ``mac_pwr_switch_usb_8822b(POWER_ON)`` — probe the power state, then run the 8822b
     ``card_en_flow`` power sequence. On a *warm* chip (already on) it returns PWR_UNCHANGE
     and the caller forces a power-OFF (``card_dis_flow``) then power-ON again — the
     "warm reboot but device not power off" workaround. A cold chip reads REG_CR == 0xEA
     and runs card-enable directly (no reset), which is what the cold captures show.
  3. ``init_system_cfg_8822b`` — WL platform reset, SYS_FUNC_EN, disable boot-from-flash.

Ported from:
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_init_8822b.c:945  pre_init_system_cfg_8822b
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:32    mac_pwr_switch_usb_8822b
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_init_8822b.c:715  init_system_cfg_8822b
  [SRC] hal/halmac/halmac_88xx/halmac_cfg_wmac_88xx.c:637            enable_bb_rf_88xx
"""
from __future__ import annotations

from . import pwrseq
from .constants import (
    BIT_BOOT_FSPI_EN,
    BIT_FSPI_EN,
    BIT_WL_PLATFORM_RST,
    MCUFW_CTRL_FW_EXIST,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_CR_DISABLED,
    REG_GPIO_MUXCFG,
    REG_LED_CFG,
    REG_MCUFW_CTRL,
    REG_PAD_CTRL1,
    REG_PRE_INIT_FE5B,
    REG_RF_CTRL,
    REG_RPWM,
    REG_RSV_CTRL,
    REG_SW_MDIO,
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    REG_SYS_FUNC_EN,
    REG_SYS_STATUS1,
    REG_WLRF1,
    SYS_FUNC_EN,
)

_SYS_CFG2_USB3 = 0x20            # REG_SYS_CFG2+3 value that marks a USB3 link


def _enable_bb_rf(t, enable: bool) -> None:
    """enable_bb_rf_88xx [SRC] halmac_cfg_wmac_88xx.c:637 — gate BB/RF clocks.

    Power-on uses the disable path (enable=0): clear the BB enable bits of REG_SYS_FUNC_EN,
    REG_RF_CTRL and REG_WLRF1. The enable path additionally runs board_rf_fine_tune (a cached
    EFUSE read for the 2L-PCB XTAL tweak); it is wired at the MAC-init-for-RX milestone where
    it can be checked against the wire it produces."""
    if enable:
        raise NotImplementedError("RTL8822BU: enable_bb_rf(on) is wired at the MAC-init milestone")
    v = t.read8(REG_SYS_FUNC_EN)
    t.write8(REG_SYS_FUNC_EN, v & ~((1 << 0) | (1 << 1)))
    v = t.read8(REG_RF_CTRL)
    t.write8(REG_RF_CTRL, v & ~((1 << 0) | (1 << 1) | (1 << 2)))
    v = t.read32(REG_WLRF1)
    t.write32(REG_WLRF1, v & ~((1 << 24) | (1 << 25) | (1 << 26)))


def pre_init_system_cfg(t) -> None:
    """pre_init_system_cfg_8822b [SRC] halmac_init_8822b.c:945."""
    t.write8(REG_RSV_CTRL, 0)

    # USB: the 0xFE5B |= BIT(4) tweak is USB3-only (REG_SYS_CFG2+3 == 0x20). The cold
    # captures read 0x80 here, so it is skipped — its USB3 side stays source-ported-but-
    # uncaptured until a USB2 capture exists. (Counter-intuitively, 0x20 == USB3.)
    if t.read8(REG_SYS_CFG2 + 3) == _SYS_CFG2_USB3:
        t.write8(REG_PRE_INIT_FE5B, t.read8(REG_PRE_INIT_FE5B) | (1 << 4))

    # PIN-mux: PAD_CTRL1 set BIT28/29, LED_CFG clear BIT25/26, GPIO_MUXCFG set BIT2.
    v = t.read32(REG_PAD_CTRL1)
    t.write32(REG_PAD_CTRL1, (v & ~((1 << 28) | (1 << 29))) | (1 << 28) | (1 << 29))
    v = t.read32(REG_LED_CFG)
    t.write32(REG_LED_CFG, v & ~((1 << 25) | (1 << 26)))
    v = t.read32(REG_GPIO_MUXCFG)
    t.write32(REG_GPIO_MUXCFG, (v & ~(1 << 2)) | (1 << 2))

    _enable_bb_rf(t, enable=False)

    t.read8(REG_SYS_CFG1 + 2)            # test-mode check: BIT(4) set => WLAN-mode fail (not enforced)


def _mac_pwr_switch(t, chip_ver: int, power_on: bool) -> bool:
    """mac_pwr_switch_usb_8822b [SRC] halmac_usb_8822b.c:32. Returns True iff the chip was
    already in the requested ON state (HALMAC_RET_PWR_UNCHANGE), so the caller can reset."""
    rpwm = t.read8(REG_RPWM)
    if t.read16(REG_MCUFW_CTRL) == MCUFW_CTRL_FW_EXIST:
        t.write8(REG_RPWM, (rpwm ^ (1 << 7)) & 0x80)        # leave 32K

    if t.read8(REG_CR) == REG_CR_DISABLED:                   # 0xEA => disabled/off
        mac_on = False
    else:
        mac_on = not (t.read8(REG_SYS_STATUS1 + 1) & (1 << 0))

    if power_on and mac_on:
        return True                                          # PWR_UNCHANGE

    if not power_on:
        pwrseq.run_pwr_seq(t, pwrseq.CARD_DIS_FLOW, chip_ver)
    else:
        pwrseq.run_pwr_seq(t, pwrseq.CARD_EN_FLOW, chip_ver)
        v = t.read8(REG_SYS_STATUS1 + 1)                     # W8_CLR BIT(0)
        t.write8(REG_SYS_STATUS1 + 1, v & ~(1 << 0))
        t.read8(REG_SW_MDIO + 3)                             # post-power-on read-twice probe
    return False


def init_system_cfg(t) -> None:
    """init_system_cfg_8822b [SRC] halmac_init_8822b.c:715."""
    v = t.read32(REG_CPU_DMEM_CON) | BIT_WL_PLATFORM_RST
    t.write32(REG_CPU_DMEM_CON, v)

    v = t.read8(REG_SYS_FUNC_EN + 1) | SYS_FUNC_EN
    t.write8(REG_SYS_FUNC_EN + 1, v)

    # disable boot-from-flash so the driver can download its own FW
    tmp = t.read32(REG_MCUFW_CTRL)
    if tmp & BIT_BOOT_FSPI_EN:
        t.write32(REG_MCUFW_CTRL, tmp & ~BIT_BOOT_FSPI_EN)
        t.write32(REG_GPIO_MUXCFG, t.read32(REG_GPIO_MUXCFG) & ~BIT_FSPI_EN)


def power_on(t, chip_ver: int) -> None:
    """rtw_halmac_poweron [SRC] hal/hal_halmac.c:2705 — the full USB power-on, including the
    warm-reboot off->on workaround (which only fires on an already-powered chip)."""
    pre_init_system_cfg(t)
    if _mac_pwr_switch(t, chip_ver, power_on=True):
        # warm chip: force off then on again [SRC] hal_halmac.c:2768-2772
        _mac_pwr_switch(t, chip_ver, power_on=False)
        _mac_pwr_switch(t, chip_ver, power_on=True)
    init_system_cfg(t)
