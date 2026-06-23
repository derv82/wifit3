"""RTL8812A power-sequence tables (the chip-specific rows; runtime is in the base).

Rows transcribed verbatim from ``include/Hal8812PwrSeq.h``; the enable flow is the
concatenation the vendor builds in ``hal/rtl8812a/Hal8812PwrSeq.c``:

    rtl8812_card_enable_flow = CARDDIS_TO_CARDEMU + CARDEMU_TO_ACT + END

``_InitPowerOn_8812AU`` drives this as ``Rtl8812_NIC_ENABLE_FLOW`` with
**cut = PWR_CUT_ALL, fab = ALL, intf = USB** ([SRC] usb_halinit.c:327, the 8812 `else`
branch — distinct from the 8821's cut-A MP path). The C array's terminating END row is
a no-op terminator and is omitted (it emits no register transfer); the PCI-only rows
fall out under intf=USB. The 8812's CARDEMU_TO_ACT is markedly shorter than the 8821's
(no LDO/SPS regulator block) — a real chip difference, ported as-is.
"""
from __future__ import annotations

from ..rtl88xxau_base.pwrseq import (  # noqa: F401
    PWR_CMD_DELAY,
    PWR_CMD_POLLING,
    PWR_CMD_WRITE,
    PWR_CUT_ALL_MSK,
    PWR_FAB_ALL_MSK,
    PWR_INTF_USB_MSK,
    PWRSEQ_DELAY_MS,
    PWRSEQ_DELAY_US,
    B0, B1, B2, B3, B4, B5, B6, B7,
    _ALL, _CA, _FA, _MAC, _PCI,
)

_W = PWR_CMD_WRITE
_P = PWR_CMD_POLLING

# [SRC] include/Hal8812PwrSeq.h:131 RTL8812_TRANS_CARDDIS_TO_CARDEMU
RTL8812_TRANS_CARDDIS_TO_CARDEMU = [
    (0x0012, _CA, _FA, _ALL, _MAC, _W, B0, B0),    # force PWM mode
    (0x0014, _CA, _FA, _ALL, _MAC, _W, 0x80, 0),   # turn off ZCD
    (0x0015, _CA, _FA, _ALL, _MAC, _W, 0x01, 0),   # turn off ZCD
    (0x0023, _CA, _FA, _ALL, _MAC, _W, 0x10, 0),   # hpon LDO leave sleep mode
    (0x0046, _CA, _FA, _ALL, _MAC, _W, 0xFF, 0x00),  # gpio0~7 input mode
    (0x0043, _CA, _FA, _ALL, _MAC, _W, 0xFF, 0x00),  # gpio11/10~8 input mode
    (0x0005, _CA, _FA, _PCI, _MAC, _W, B2, 0),     # enable SW LPS (PCIe only)
    (0x0005, _CA, _FA, _ALL, _MAC, _W, B3, 0),     # 0x04[11] enable WL suspend
    (0x0003, _CA, _FA, _ALL, _MAC, _W, B2, B2),    # 0x03[2] enable 8051
    (0x0301, _CA, _FA, _PCI, _MAC, _W, 0xFF, 0),   # PCIe DMA start
    (0x0024, _CA, _FA, _ALL, _MAC, _W, B1, B1),    # xosc buffer = schmitt trigger
    (0x0028, _CA, _FA, _ALL, _MAC, _W, B3, B3),    # xosc buffer = schmitt trigger
]

# [SRC] include/Hal8812PwrSeq.h:54 RTL8812_TRANS_CARDEMU_TO_ACT
RTL8812_TRANS_CARDEMU_TO_ACT = [
    (0x0005, _CA, _FA, _ALL, _MAC, _W, B2, 0),     # disable SW LPS 0x04[10]=0
    (0x0006, _CA, _FA, _ALL, _MAC, _P, B1, B1),    # wait 0x04[17]=1 power ready
    (0x0005, _CA, _FA, _ALL, _MAC, _W, B3, 0),     # disable WL suspend
    (0x0005, _CA, _FA, _ALL, _MAC, _W, B0, B0),    # 0x04[8]=1
    (0x0005, _CA, _FA, _ALL, _MAC, _P, B0, 0),     # poll until 0x04[8]=0
    (0x0024, _CA, _FA, _ALL, _MAC, _W, B1, 0),     # 0x24[1] xosc buffer = nand
    (0x0028, _CA, _FA, _ALL, _MAC, _W, B3, 0),     # 0x28[3] xosc buffer = nand
]

# Flow [SRC] hal/rtl8812a/Hal8812PwrSeq.c:42 rtl8812_card_enable_flow (END omitted).
CARD_ENABLE_FLOW = RTL8812_TRANS_CARDDIS_TO_CARDEMU + RTL8812_TRANS_CARDEMU_TO_ACT
