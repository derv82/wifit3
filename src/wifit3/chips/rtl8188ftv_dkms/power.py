"""RTL8188FTV DKMS power on/off.

``power_on`` ported from ``_InitPowerOn_8188FU``
(hal/rtl8188f/usb/usb_halinit.c:136-186); ``card_disable`` from
``CardDisableRTL8188FU`` (usb_halinit.c:1892-1952) with the LPS-enter and
card-disable flows; ``firmware_self_reset`` from
``rtl8188f_FirmwareSelfReset`` (hal/rtl8188f/rtl8188f_hal_init.c:374+).
"""
from __future__ import annotations

import time

from . import constants as C
from . import pwrseq


def BIT(n: int) -> int:
    return 1 << n


CR_INIT_POWER_ON = (
    C.HCI_TXDMA_EN | C.HCI_RXDMA_EN | C.TXDMA_EN | C.RXDMA_EN
    | C.PROTOCOL_EN | C.SCHEDULE_EN | C.ENSEC | C.CALTMR_EN
)

COLD_MCUFWDL = 0x05
COLD_CR = 0x0000


def is_chip_warm(t) -> bool:
    """Return True if the chip looks already initialized.

    Cold power-on reset reads MCUFWDL 0x05 with CR 0x0000 (intact
    enumeration prefix); any other value means a previous session left
    FW/power state behind. The bring-up attempts recovery over it (the
    FW download resets the 8051 when RAM_DL_SEL is set, and the power
    flows converge); replug if the scanner stays empty.
    """
    try:
        if t.read8(C.REG_MCUFWDL) != COLD_MCUFWDL:
            return True
        return t.read16(C.REG_CR_8188F) != COLD_CR
    except IOError:
        return False


def warm_state(t) -> tuple[int, int]:
    """Best-effort (mcufwdl, cr) snapshot for warm-prompt diagnostics."""
    try:
        return t.read8(C.REG_MCUFWDL), t.read16(C.REG_CR_8188F)
    except IOError:
        return -1, -1


def set_pll_ref_clk_sel(t, sel: int) -> None:
    value8 = t.read8(C.REG_MAC_PLL_CTRL_EXT_8188F)
    if (value8 & 0x0F) != (sel & 0x0F):
        value16 = t.read16(C.REG_RSV_CTRL_8188F)
        wlock = bool(value16 & BIT(8))
        if wlock:
            t.write16(C.REG_RSV_CTRL_8188F, value16 & ~BIT(8))
        t.write8(C.REG_MAC_PLL_CTRL_EXT_8188F, (value8 & 0xF0) | (sel & 0x0F))
        if wlock:
            t.write16(C.REG_RSV_CTRL_8188F, value16 | BIT(8))


def power_on(t, pll_ref_clk_sel: int = C.RTW_PLL_REF_CLK_SEL_DEFAULT) -> bool:
    if (pll_ref_clk_sel & 0x0F) != 0x0F:
        # TODO: verify, untested here, needs a card with a non-autoload pll_ref_clk_sel
        set_pll_ref_clk_sel(t, pll_ref_clk_sel)
    if not pwrseq.parse(t, pwrseq.CARD_ENABLE_FLOW):
        return False
    t.write8(C.REG_CR_8188F, 0x00)
    value16 = t.read16(C.REG_CR_8188F)
    t.write16(C.REG_CR_8188F, value16 | CR_INIT_POWER_ON)
    return True


def check_powered(t) -> tuple[int, int]:
    sys_clkr_1 = t.read8(C.REG_SYS_CLKR_8188F + 1)
    cr = t.read8(C.REG_CR_8188F)
    return sys_clkr_1, cr


def firmware_self_reset(t, signature: int, version: int, subversion: int) -> None:
    if (signature & 0xFFF0) == 0x88C0 and (
            version < 0x21 or (version == 0x21 and subversion < 0x01)):
        t.write8(C.REG_HMETFR + 3, 0x20)
        delay = 100
        value8 = t.read8(C.REG_SYS_FUNC_EN + 1)
        while value8 & BIT(2):
            delay -= 1
            if delay == 0:
                break
            time.sleep(50e-6)
            value8 = t.read8(C.REG_SYS_FUNC_EN + 1)
        if delay == 0:
            value8 = t.read8(C.REG_SYS_FUNC_EN + 1)
            t.write8(C.REG_SYS_FUNC_EN + 1, value8 & ~BIT(2))


def card_disable(t, fw_ready: bool, fw_sig: int = 0, fw_ver: int = 0,
                 fw_sub: int = 0) -> bool:
    value8 = t.read8(C.REG_TX_RPT_CTRL)
    t.write8(C.REG_TX_RPT_CTRL, value8 & ~BIT(1))
    t.write8(C.REG_CR_8188F, 0x00)
    if t.read8(C.REG_MCUFWDL) & BIT(7) and fw_ready:
        # TODO: verify, untested here, needs a card whose FW is 81xxC pre-v33.1
        firmware_self_reset(t, fw_sig, fw_ver, fw_sub)
    if not pwrseq.parse(t, pwrseq.ENTER_LPS_FLOW):
        return False
    value8 = t.read8(C.REG_SYS_FUNC_EN + 1)
    t.write8(C.REG_SYS_FUNC_EN + 1, value8 & ~BIT(2))
    t.write8(C.REG_MCUFWDL, 0x00)
    return pwrseq.parse(t, pwrseq.CARD_DISABLE_FLOW)
