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


def test_enable_tx_blink_selects_gpio8_wl_led_and_tx_mode():
    t = _Regs({0x4A: 0xFF, 0x4E: 0xFF})

    led.enable_tx_blink(t)

    assert t.regs[0x4A] == 0xFC
    assert t.regs[0x4E] == 0xBC
    assert t.ops == [
        ("R8", 0x4A),
        ("W8", 0x4A, 0xFC),
        ("R8", 0x4E),
        ("W8", 0x4E, 0xFF),
        ("R8", 0x4E),
        ("W8", 0x4E, 0xBC),
    ]
