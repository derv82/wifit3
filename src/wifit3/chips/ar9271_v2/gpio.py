"""GPIO / LED config over WMI — ported from hw.c + htc_drv_gpio.c.

Only the LED pin's output setup is on the cold-boot path: ath9k_htc_led_init requests the
AR9271 LED GPIO (pin 15) as an output and drives it. The pin sits in the WMAC GPIO mask, so
the ``cfg_wmac`` register path applies (the SoC/AR7010 paths are other silicon).
"""
from __future__ import annotations

from . import reg as R
from .hw import AthHw


def _cfg_output_mux(hw: AthHw, gpio: int, mux_type: int) -> None:
    """ath9k_hw_gpio_cfg_output_mux [SRC] hw.c:2690 — gpio 15 (>11) lands in MUX3, shift
    (gpio % 6) * 5. The non-MUX1 path is a plain RMW."""
    if gpio > 11:
        addr = R.AR_GPIO_OUTPUT_MUX3
    else:
        raise NotImplementedError("ar9271_v2: only the led pin (MUX3) is on the cold path")
    gpio_shift = (gpio % 6) * 5
    hw.rmw(addr, mux_type << gpio_shift, 0x1f << gpio_shift)


def gpio_request_out(hw: AthHw, gpio: int, signal_type: int) -> None:
    """ath9k_hw_gpio_request_out -> cfg_wmac (non-SoC) [SRC] hw.c:2738-2762: enable the 2-bit
    output driver in AR_GPIO_OE_OUT, then point the output mux at ``signal_type``."""
    gpio_shift = gpio << 1
    hw.rmw(R.AR_GPIO_OE_OUT, R.AR_GPIO_OE_OUT_DRV_ALL << gpio_shift,
           R.AR_GPIO_OE_OUT_DRV << gpio_shift)
    _cfg_output_mux(hw, gpio, signal_type)


def set_gpio(hw: AthHw, gpio: int, val: int) -> None:
    """ath9k_hw_set_gpio [SRC] hw.c:2835 — AR9271 inverts the output value."""
    val = 0 if val else 1                    # AR_SREV_9271 -> val = !val
    hw.rmw(R.AR_GPIO_IN_OUT, val << gpio, 1 << gpio)


def led_init(hw: AthHw) -> None:
    """ath9k_htc_led_init's hardware side [SRC] htc_drv_gpio.c:264-268."""
    gpio_request_out(hw, R.ATH_LED_PIN_9271, R.AR_GPIO_OUTPUT_MUX_AS_OUTPUT)
    set_gpio(hw, R.ATH_LED_PIN_9271, 1)
