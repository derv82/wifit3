"""RTL8812AU SW LED0 on/off [SRC] hal/rtl8812a/usb/rtl8812au_led.c SwLedOn_8812AU / SwLedOff_8812AU.

LED_PIN_LED0 only: SW_LED_MODE9's no-link blink (hal_usb_led.c:SwLedBlink9) never drives LED1/LED2.
AntDivCfg is 0: CONFIG_ANTENNA_DIVERSITY is off (include/autoconf.h:107), so its LEDCFG2 branch is dead.
"""
from __future__ import annotations

REG_MAC_PINMUX_CFG = 0x0043     # hal_com_reg.h:70
REG_LEDCFG0 = 0x004C            # hal_com_reg.h:73
REG_LEDCFG2 = 0x004E            # hal_com_reg.h:75
INTF_SEL2_MINICARD = 2          # hal_data.h:73-75
INTF_SEL3_USB_Solo = 3
INTF_SEL4_USB_Combo = 4
LED_BLINK_NO_LINK_INTERVAL_ALPHA_S = 1.000   # hal_com_led.h:54 (1000 ms)
BIT3 = 1 << 3
BIT5 = 1 << 5
BIT6 = 1 << 6


def _is_minicard_or_slim(interface_sel: int) -> bool:
    return interface_sel in (INTF_SEL2_MINICARD, INTF_SEL3_USB_Solo, INTF_SEL4_USB_Combo)


def sw_led_on_8812au(t, interface_sel: int) -> None:
    """[SRC] rtl8812au_led.c:SwLedOn_8812AU:40, LED_PIN_LED0."""
    if _is_minicard_or_slim(interface_sel):
        t.read8(REG_LEDCFG2)
        led_cfg = t.read8(REG_LEDCFG2)
        t.write8(REG_LEDCFG2, (led_cfg & 0xF0) | BIT5 | BIT6)
    else:
        led_cfg = t.read8(REG_LEDCFG0)
        t.write8(REG_LEDCFG0, (led_cfg & 0x70) | BIT5)


def sw_led_off_8812au(t, interface_sel: int) -> None:
    """[SRC] rtl8812au_led.c:SwLedOff_8812AU:110, LED_PIN_LED0; bLedOpenDrain is TRUE (usb_halinit.c:2123)."""
    if _is_minicard_or_slim(interface_sel):
        led_cfg = t.read8(REG_LEDCFG2) & 0x90
        t.write8(REG_LEDCFG2, led_cfg | BIT3)
        led_cfg = t.read8(REG_MAC_PINMUX_CFG) & 0xFE
        t.write8(REG_MAC_PINMUX_CFG, led_cfg)
    else:
        led_cfg = t.read8(REG_LEDCFG0) & 0x70
        t.write8(REG_LEDCFG0, led_cfg | BIT3 | BIT5)
