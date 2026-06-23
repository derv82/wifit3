"""RTL8821CU hardware-LED config + the SW-LED blink — the USB ``hal_init_misc`` tail of
``rtl8821cu_hal_init`` plus the runtime blink the BlinkTimer drives.

The real hal op is ``rtl8821cu_hal_init`` ([SRC] hal/rtl8821c/usb/rtl8821cu_halinit.c:55), the
USB wrapper that runs the core ``rtl8821c_hal_init`` (everything in ``bringup.hal_init`` up to and
including BT-coex) then ``hal_init_misc`` ([SRC] :41). On this SW-LED card ``init_hwled`` is a
no-op (``LedStrategy != HW_LED``), so the only wire traffic is
``rtw_halmac_led_cfg(enable=TRUE, mode=3)`` ([SRC] hal_halmac.c:5094): pinmux the WL_LED GPIO
function on, then put the LED in software-control mode.

The LED then blinks at runtime: ``SwLedBlink1`` ([SRC] hal/led/hal_usb_led.c:112) is the
BlinkTimer callback, a SEPARATE async producer interleaved with the channel-hop loop. In the
no-link (monitor) state it runs ``LED_BLINK_SLOWLY`` — apply the pending on/off, then toggle for
the next tick (strict alternation). The LED is cosmetic, but the byte-for-byte gate reproduces it
as a registered async handler rather than stripping it.
"""
from __future__ import annotations

from dataclasses import dataclass

REG_LED_CFG = 0x004C            # REG_LEDCFG0; +2 = LEDCFG2 (0x4E) [SRC] hal_com_reg.h:73
REG_GPIO8_EXTWOL = 0x004A       # GPIO8 WL_EXT_WOL select reg [SRC] halmac_gpio_8821c.c:250


def _rmw8(t, addr: int, mask: int, value: int) -> None:
    """halmac pinmux read-modify-write: clear the field, OR the masked value back in."""
    cur = t.read8(addr)
    t.write8(addr, (cur & ~mask) | (value & mask))


def _pinmux_set_func_wl_led(t) -> None:
    """pinmux_switch_8821c(WL_LED) [SRC] halmac_gpio_8821c.c:889 — walk the GPIO8 pinmux list:
    each non-target row is deselected by writing ``~value & msk``, the target row (WL_LED) is
    selected with ``value & msk`` and the walk stops. The GPIO8 list is [WL_EXT_WOL, WL_LED, ...],
    so this is: deselect WL_EXT_WOL (0x4a[1:0] -> 0), then select WL_LED (0x4e[5] = 1)."""
    _rmw8(t, REG_GPIO8_EXTWOL, 0x3, (~0x3) & 0x3)      # row {0x4a, msk=0x3, val=0x3}: ~val -> 0
    _rmw8(t, REG_LED_CFG + 2, 1 << 5, 1 << 5)          # row {0x4e, msk=BIT5, val=BIT5}: select


def _pinmux_wl_led_mode_sw_ctrl(t) -> None:
    """pinmux_wl_led_mode_88xx(SW_CTRL) [SRC] halmac_gpio_88xx.c:29 on 0x4e (REG_LED_CFG+2):
    clear bit6, set bit3, clear bits[2:0]; SW_CTRL contributes no extra bits."""
    cur = t.read8(REG_LED_CFG + 2)
    t.write8(REG_LED_CFG + 2, ((cur & ~(1 << 6) & ~0x7) | (1 << 3)) & 0xFF)


def cfg_wl_led(t) -> None:
    """rtw_halmac_led_cfg(enable=TRUE, mode=3=SW_CTRL) [SRC] hal_halmac.c:5094 — the SW-LED arm of
    the USB ``hal_init_misc``, run right after the core ``rtl8821c_hal_init`` returns."""
    _pinmux_set_func_wl_led(t)
    _pinmux_wl_led_mode_sw_ctrl(t)


@dataclass
class LedBlinkState:
    """The ``PLED_USB`` blink state the BlinkTimer carries between ticks. ``cfg_wl_led`` leaves the
    LED off (0x4e[3] set), and the first no-link tick drives it on — so the pending state seeds ON
    and the actual-on flag seeds off."""
    blinking_led_state_on: bool = True      # pLed->BlinkingLedState (RTW_LED_ON)
    bled_on: bool = False                   # pLed->bLedOn (LED currently lit)


def _pinmux_wl_led_sw_ctrl(t, on: bool) -> None:
    """pinmux_wl_led_sw_ctrl_88xx [SRC] halmac_gpio_88xx.c:74 (via rtw_halmac_led_switch /
    SwLedOn/Off) — LED on/off on 0x4e[3], active-low: on clears bit3, off sets it."""
    cur = t.read8(REG_LED_CFG + 2)
    t.write8(REG_LED_CFG + 2, (cur & ~(1 << 3)) if on else (cur | (1 << 3)))


def led_blink(t, st: LedBlinkState) -> None:
    """SwLedBlink1 LED_BLINK_SLOWLY (no-link) tick [SRC] hal/led/hal_usb_led.c:131,155 — apply the
    pending BlinkingLedState (SwLedOn/Off), then set the next state from bLedOn. The no-link blink
    strictly alternates, so each tick toggles the LED. (TX/RX/site-survey LED_CTL events are a
    distinct, traffic-driven producer — they are not this timer tick.)"""
    _pinmux_wl_led_sw_ctrl(t, st.blinking_led_state_on)
    st.bled_on = st.blinking_led_state_on
    st.blinking_led_state_on = not st.bled_on
