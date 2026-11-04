"""RTL8822BU WLAN LED control."""
from __future__ import annotations

from .constants import BIT_WL_LED_GPIO8, REG_GPIO8_WL_EXT_WOL, REG_LED_CFG

HALMAC_WLLED_MODE_TRX = 0
HALMAC_WLLED_MODE_TX = 1
HALMAC_WLLED_MODE_RX = 2
HALMAC_WLLED_MODE_SW_CTRL = 3
LED_BLINK_NO_LINK_INTERVAL_ALPHA_S = 1.000   # hal_com_led.h:54 (1000 ms)


def _pinmux_set_wl_led(t) -> None:
    value8 = t.read8(REG_GPIO8_WL_EXT_WOL)
    value8 &= ~0x03
    t.write8(REG_GPIO8_WL_EXT_WOL, value8)

    value8 = t.read8(REG_LED_CFG + 2)
    value8 |= BIT_WL_LED_GPIO8
    t.write8(REG_LED_CFG + 2, value8)


def pinmux_wl_led_mode(t, mode: int) -> None:
    value8 = t.read8(REG_LED_CFG + 2)
    value8 &= ~(1 << 6)
    value8 |= 1 << 3
    value8 &= ~0x07
    if mode == HALMAC_WLLED_MODE_TRX:
        value8 |= 2
    elif mode == HALMAC_WLLED_MODE_TX:
        value8 |= 4
    elif mode == HALMAC_WLLED_MODE_RX:
        value8 |= 6
    elif mode != HALMAC_WLLED_MODE_SW_CTRL:
        raise ValueError(f"unsupported WLAN LED mode {mode}")
    t.write8(REG_LED_CFG + 2, value8)


def led_cfg(t, mode: int) -> None:
    """rtw_halmac_led_cfg(enable=1) (hal_halmac.c:5094): the GPIO8 WL_LED pinmux, then the LED mode."""
    _pinmux_set_wl_led(t)
    pinmux_wl_led_mode(t, mode)


def pinmux_wl_led_sw_ctrl(t, on: bool) -> None:
    """pinmux_wl_led_sw_ctrl_88xx (halmac_gpio_88xx.c:75): LED_CFG+2 BIT(3) clear = on."""
    value8 = t.read8(REG_LED_CFG + 2)
    if on:
        value8 &= ~(1 << 3)
    else:
        value8 |= 1 << 3
    t.write8(REG_LED_CFG + 2, value8)
