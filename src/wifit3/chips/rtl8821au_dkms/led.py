"""RTL8821AU SW LED0 on/off [SRC] hal/rtl8812a/usb/rtl8812au_led.c SwLedOn_8821AU / SwLedOff_8821AU.

LED_PIN_LED0 only: SW_LED_MODE9's no-link blink (hal_usb_led.c:SwLedBlink9) never drives LED1/LED2.
"""
from __future__ import annotations

REG_MAC_PINMUX_CFG = 0x0043     # hal_com_reg.h:70
REG_LEDCFG2 = 0x004E            # hal_com_reg.h:75
REG_GPIO_PIN_CTRL_2 = 0x0060    # hal_com_reg.h:81
INTF_SEL2_MINICARD = 2          # hal_data.h:73-75
INTF_SEL3_USB_Solo = 3
INTF_SEL4_USB_Combo = 4
LED_BLINK_NO_LINK_INTERVAL_ALPHA_S = 1.000   # hal_com_led.h:54 (1000 ms)
BIT3 = 1 << 3
BIT5 = 1 << 5
BIT6 = 1 << 6
BIT_GPIO8_OUTPUT = 1 << 8
BIT_GPIO8_OUTPUT_ENABLE = 1 << 16
BIT_GPIO8_MODE = 1 << 24
TX_BLINK_INTERVAL_S = 0.05


def _is_minicard_or_slim(interface_sel: int) -> bool:
    return interface_sel in (INTF_SEL2_MINICARD, INTF_SEL3_USB_Solo, INTF_SEL4_USB_Combo)


def turn_on(t, interface_sel: int) -> None:
    """[SRC] rtl8812au_led.c:SwLedOn_8821AU:197, LED_PIN_LED0."""
    if _is_minicard_or_slim(interface_sel):
        t.read8(REG_LEDCFG2)
        ledcfg = t.read8(REG_LEDCFG2)
        t.write8(REG_LEDCFG2, (ledcfg & 0xF0) | BIT5 | BIT6)
    else:
        ledcfg = t.read8(REG_LEDCFG2) & 0xC0
        gpio8_cfg = t.read32(REG_GPIO_PIN_CTRL_2)
        t.write8(REG_LEDCFG2, ledcfg)
        t.write32(REG_GPIO_PIN_CTRL_2,
                  (gpio8_cfg | BIT_GPIO8_OUTPUT_ENABLE) & ~BIT_GPIO8_OUTPUT & ~BIT_GPIO8_MODE)


def turn_off(t, interface_sel: int) -> None:
    """[SRC] rtl8812au_led.c:SwLedOff_8821AU:263, LED_PIN_LED0; bLedOpenDrain is TRUE (usb_halinit.c:2123)."""
    if _is_minicard_or_slim(interface_sel):
        ledcfg = t.read8(REG_LEDCFG2) & 0x90
        t.write8(REG_LEDCFG2, ledcfg | BIT3)
        ledcfg = t.read8(REG_MAC_PINMUX_CFG) & 0xFE
        t.write8(REG_MAC_PINMUX_CFG, ledcfg)
    else:
        gpio8_cfg = t.read32(REG_GPIO_PIN_CTRL_2)
        t.write32(REG_GPIO_PIN_CTRL_2,
                  (gpio8_cfg | BIT_GPIO8_OUTPUT_ENABLE | BIT_GPIO8_OUTPUT) & ~BIT_GPIO8_MODE)
