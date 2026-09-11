from wifit3.chips.rtl8821au_dkms import led


class _FakeRegs:
    def __init__(self, ledcfg2: int = 0, gpio8: int = 0):
        self.ledcfg2 = ledcfg2
        self.gpio8 = gpio8
        self.reads: list[tuple[int, int]] = []
        self.writes8: list[tuple[int, int]] = []
        self.writes32: list[tuple[int, int]] = []

    def read8(self, addr: int) -> int:
        self.reads.append((addr, 1))
        assert addr == led.REG_LEDCFG2
        return self.ledcfg2

    def read32(self, addr: int) -> int:
        self.reads.append((addr, 4))
        assert addr == led.REG_GPIO_PIN_CTRL_2
        return self.gpio8

    def write8(self, addr: int, value: int) -> None:
        self.writes8.append((addr, value & 0xFF))
        assert addr == led.REG_LEDCFG2
        self.ledcfg2 = value & 0xFF

    def write32(self, addr: int, value: int) -> None:
        self.writes32.append((addr, value & 0xFFFFFFFF))
        assert addr == led.REG_GPIO_PIN_CTRL_2
        self.gpio8 = value & 0xFFFFFFFF


def test_turn_on_uses_vendor_gpio8_led_sequence():
    regs = _FakeRegs(ledcfg2=0xAF, gpio8=0xA55A5BA5)

    led.turn_on(regs)

    assert regs.reads == [(led.REG_LEDCFG2, 1), (led.REG_GPIO_PIN_CTRL_2, 4)]
    assert regs.writes8 == [(led.REG_LEDCFG2, 0x80)]
    assert regs.writes32 == [(led.REG_GPIO_PIN_CTRL_2, 0xA45B5AA5)]
    assert regs.gpio8 & led.BIT_GPIO8_OUTPUT_ENABLE
    assert regs.gpio8 & led.BIT_GPIO8_OUTPUT == 0
    assert regs.gpio8 & led.BIT_GPIO8_MODE == 0


def test_turn_off_uses_vendor_gpio8_led_sequence():
    regs = _FakeRegs(gpio8=0xA55A5AA5)

    led.turn_off(regs)

    assert regs.reads == [(led.REG_GPIO_PIN_CTRL_2, 4)]
    assert regs.writes8 == []
    assert regs.writes32 == [(led.REG_GPIO_PIN_CTRL_2, 0xA45B5BA5)]
    assert regs.gpio8 & led.BIT_GPIO8_OUTPUT_ENABLE
    assert regs.gpio8 & led.BIT_GPIO8_OUTPUT
    assert regs.gpio8 & led.BIT_GPIO8_MODE == 0
