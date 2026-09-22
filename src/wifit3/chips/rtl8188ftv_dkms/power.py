"""RTL8188FTV DKMS power-on (M3).

Ported from ``_InitPowerOn_8188FU`` (hal/rtl8188f/usb/usb_halinit.c:136-186):
optional PLL-refclk override, the card-enable power sequence, then the MAC
DMA/WMAC/schedule/security/cal-timer enable in ``REG_CR``.
"""
from __future__ import annotations

from . import constants as C
from . import pwrseq


def BIT(n: int) -> int:
    return 1 << n


CR_INIT_POWER_ON = (
    C.HCI_TXDMA_EN | C.HCI_RXDMA_EN | C.TXDMA_EN | C.RXDMA_EN
    | C.PROTOCOL_EN | C.SCHEDULE_EN | C.ENSEC | C.CALTMR_EN
)


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
