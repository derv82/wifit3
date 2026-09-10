"""RTL8822BU WLAN LED control."""
from __future__ import annotations

from .constants import BIT_WL_LED_GPIO8, REG_GPIO8_WL_EXT_WOL, REG_LED_CFG

HALMAC_WLLED_MODE_TX = 1


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
    if mode == HALMAC_WLLED_MODE_TX:
        value8 |= 4
    else:
        raise ValueError(f"unsupported WLAN LED mode {mode}")
    t.write8(REG_LED_CFG + 2, value8)


def enable_tx_blink(t) -> None:
    _pinmux_set_wl_led(t)
    pinmux_wl_led_mode(t, HALMAC_WLLED_MODE_TX)
