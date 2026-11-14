"""RTL8822C GPIO8 hardware WLAN LED control."""
from wifit3.chips.rtw88_base.registers import REG_LED_CFG

from .transport import RTL8822CUTransport

# GPIO8_WL_EXT_WOL_8822C / GPIO8_WL_LED_8822C [SRC
# hal/halmac/halmac_88xx/halmac_8822c/halmac_gpio_8822c.c:349-354]
_REG_GPIO8_WL_EXT_WOL = 0x004A
_BIT_GPIO8_WL_EXT_WOL = 0x03
_BIT_GPIO8_WL_LED = 1 << 5

# pinmux_wl_led_mode_88xx [SRC hal/halmac/halmac_88xx/halmac_gpio_88xx.c:29-64]
_BIT_WL_LED_DISABLE = 1 << 6
_BIT_WL_LED_SW_CTRL = 1 << 3
_BITS_WL_LED_MODE = 0x07
_HALMAC_WLLED_MODE_TX = 0x04
_HALMAC_WLLED_MODE_SW_CTRL = 0x00


def pinmux_set_func_wl_led(transport: RTL8822CUTransport) -> None:
    """HALMAC ``pinmux_set_func_8822c(HALMAC_GPIO_FUNC_WL_LED)``."""
    value8 = transport.read8(_REG_GPIO8_WL_EXT_WOL)
    value8 &= ~_BIT_GPIO8_WL_EXT_WOL
    transport.write8(_REG_GPIO8_WL_EXT_WOL, value8)

    led_cfg2 = REG_LED_CFG + 2
    value8 = transport.read8(led_cfg2)
    value8 &= ~_BIT_GPIO8_WL_LED
    value8 |= _BIT_GPIO8_WL_LED
    transport.write8(led_cfg2, value8)


def pinmux_wl_led_mode_tx(transport: RTL8822CUTransport) -> None:
    """HALMAC ``pinmux_wl_led_mode_88xx(HALMAC_WLLED_MODE_TX)``."""
    led_cfg2 = REG_LED_CFG + 2
    value8 = transport.read8(led_cfg2)
    value8 &= ~_BIT_WL_LED_DISABLE
    value8 |= _BIT_WL_LED_SW_CTRL
    value8 &= ~_BITS_WL_LED_MODE
    value8 |= _HALMAC_WLLED_MODE_TX
    transport.write8(led_cfg2, value8)


def enable_tx_blink(transport: RTL8822CUTransport) -> None:
    """Port ``rtw_halmac_led_cfg(d, 1, 1)`` for TX-only hardware blinking."""
    pinmux_set_func_wl_led(transport)
    pinmux_wl_led_mode_tx(transport)


def disable_tx_blink(transport: RTL8822CUTransport) -> None:
    """Port ``rtw_halmac_led_cfg(d, 0, 3)`` through its hardware writes."""
    led_cfg2 = REG_LED_CFG + 2
    value8 = transport.read8(led_cfg2)
    value8 &= ~_BIT_WL_LED_DISABLE
    value8 |= _BIT_WL_LED_SW_CTRL
    value8 &= ~_BITS_WL_LED_MODE
    value8 |= _HALMAC_WLLED_MODE_SW_CTRL
    transport.write8(led_cfg2, value8)

    value8 = transport.read8(led_cfg2)
    value8 |= _BIT_WL_LED_SW_CTRL
    transport.write8(led_cfg2, value8)
