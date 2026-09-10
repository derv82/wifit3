from wifit3.chips.rtl8821au_dkms import rf


class _FakeRegs:
    def __init__(self):
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []
        self.values = {
            0x0C00: 0x00000000,
            rf.REG_HSSI_READ: 0x00000000,
            0x0D08: 0x000ABCDE,
        }

    def read32(self, addr: int) -> int:
        self.reads.append(addr)
        return self.values.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFFFFFFFF))
        self.values[addr] = value & 0xFFFFFFFF


def test_masked_rf_write_clamps_shifted_field_to_mask():
    regs = _FakeRegs()

    rf.set_rf_reg(regs, rf.RF_PATH_A, 0x18, 0x000000F0, 0x123)

    assert regs.reads == [0x0C00, rf.REG_HSSI_READ, 0x0D08]
    assert regs.writes[0] == (rf.REG_HSSI_READ, 0x00000018)
    assert regs.writes[1] == (0x0C90, 0x018ABC3E)
