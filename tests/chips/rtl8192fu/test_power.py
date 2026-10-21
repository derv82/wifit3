from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.power import power_off


class RegisterTransport:
    def __init__(self):
        self.memory = bytearray(0x1000)
        self.operations = []
        self.write16(C.REG_SYS_FUNC, C.SYS_FUNC_CPU_ENABLE | C.SYS_FUNC_BBRSTB | C.SYS_FUNC_BB_GLB_RSTN)
        self.write8(C.REG_MCU_FW_DL, C.MCU_FW_RAM_SEL)
        self.operations.clear()

    def _read(self, address, width):
        value = int.from_bytes(self.memory[address:address + width], "little")
        self.operations.append(("read", width, address, value))
        return value

    def _write(self, address, value, width):
        self.memory[address:address + width] = value.to_bytes(width, "little")
        self.operations.append(("write", width, address, value))
        if address == C.REG_HMTFR + 3:
            current = int.from_bytes(self.memory[C.REG_SYS_FUNC:C.REG_SYS_FUNC + 2], "little")
            self.memory[C.REG_SYS_FUNC:C.REG_SYS_FUNC + 2] = (
                current & ~C.SYS_FUNC_CPU_ENABLE
            ).to_bytes(2, "little")
        if address == C.REG_APS_FSMCO and width in (2, 4) and value & (1 << 9):
            current = int.from_bytes(self.memory[address:address + width], "little")
            self.memory[address:address + width] = (current & ~(1 << 9)).to_bytes(width, "little")

    def read8(self, address):
        return self._read(address, 1)

    def read16(self, address):
        return self._read(address, 2)

    def read32(self, address):
        value = self._read(address, 4)
        if address == C.REG_RXPKT_NUM:
            return value | C.RXPKT_NUM_RXDMA_IDLE
        return value

    def write8(self, address, value):
        self._write(address, value, 1)

    def write16(self, address, value):
        self._write(address, value, 2)

    def write32(self, address, value):
        self._write(address, value, 4)


def test_power_off_reaches_disabled_state(monkeypatch):
    monkeypatch.setattr("wifit3.chips.rtl8192fu.power.time.sleep", lambda _delay: None)
    transport = RegisterTransport()

    power_off(transport)

    assert transport.read8(C.REG_TXPAUSE) == 0xFF
    assert transport.read16(C.REG_RQPN_NPQ) == 0
    assert transport.read32(C.REG_RQPN) == 0x80000000
    assert transport.read8(C.REG_MCU_FW_DL) == 0
    assert not transport.read16(C.REG_SYS_FUNC) & C.SYS_FUNC_CPU_ENABLE
    assert transport.read8(C.REG_SYS_ISO_CTRL) & (1 << 5)
    assert not transport.read8(C.REG_LDOA15_CTRL) & 1
    assert transport.read16(C.REG_APS_FSMCO) & (1 << 11)
    assert not transport.read16(C.REG_APS_FSMCO) & (1 << 12)
    assert transport.read32(C.REG_GPIO_INTM) & (1 << 16)

    writes = [(width, address, value) for kind, width, address, value in transport.operations if kind == "write"]
    assert (1, C.REG_CR, 0) in writes
    assert writes.index((1, C.REG_CR, 0)) < writes.index((1, C.REG_MCU_FW_DL, 0))
