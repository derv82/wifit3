from wifit3.chips.rtl8822bu_dkms import led


class _Regs:
    def __init__(self, regs: dict[int, int]):
        self.regs = regs
        self.ops = []

    def read8(self, addr):
        self.ops.append(("R8", addr))
        return self.regs.get(addr, 0)

    def write8(self, addr, val):
        self.ops.append(("W8", addr, val & 0xFF))
        self.regs[addr] = val & 0xFF


def test_led_cfg_sw_ctrl_selects_gpio8_wl_led_and_leaves_the_led_off():
    t = _Regs({0x4A: 0x00, 0x4E: 0x62})

    led.led_cfg(t, led.HALMAC_WLLED_MODE_SW_CTRL)

    assert t.ops == [
        ("R8", 0x4A),
        ("W8", 0x4A, 0x00),
        ("R8", 0x4E),
        ("W8", 0x4E, 0x62),
        ("R8", 0x4E),
        ("W8", 0x4E, 0x28),
    ]


def test_pinmux_wl_led_sw_ctrl_clears_bit3_for_on_and_sets_it_for_off():
    t = _Regs({0x4E: 0x28})

    led.pinmux_wl_led_sw_ctrl(t, True)
    led.pinmux_wl_led_sw_ctrl(t, False)

    assert t.ops == [("R8", 0x4E), ("W8", 0x4E, 0x20), ("R8", 0x4E), ("W8", 0x4E, 0x28)]
