"""RTL8821AU-DKMS WLAN LED control."""
from __future__ import annotations

REG_LEDCFG2 = 0x004E
REG_GPIO_PIN_CTRL_2 = 0x0060
BIT_GPIO8_OUTPUT = 1 << 8
BIT_GPIO8_OUTPUT_ENABLE = 1 << 16
BIT_GPIO8_MODE = 1 << 24
TX_BLINK_INTERVAL_S = 0.05


def turn_on(t) -> None:
    """Port of SwLedOn_8821AU's GPIO8-backed LED path."""
    ledcfg = t.read8(REG_LEDCFG2) & 0xC0
    gpio8_cfg = t.read32(REG_GPIO_PIN_CTRL_2)
    t.write8(REG_LEDCFG2, ledcfg)
    t.write32(REG_GPIO_PIN_CTRL_2,
              (gpio8_cfg | BIT_GPIO8_OUTPUT_ENABLE) & ~BIT_GPIO8_OUTPUT & ~BIT_GPIO8_MODE)


def turn_off(t) -> None:
    """Port of SwLedOff_8821AU's GPIO8-backed LED path."""
    gpio8_cfg = t.read32(REG_GPIO_PIN_CTRL_2)
    t.write32(REG_GPIO_PIN_CTRL_2,
              (gpio8_cfg | BIT_GPIO8_OUTPUT_ENABLE | BIT_GPIO8_OUTPUT) & ~BIT_GPIO8_MODE)
