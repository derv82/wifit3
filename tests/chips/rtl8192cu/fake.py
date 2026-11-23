"""A register-level fake RTL8192CU: plain registers plus the EFUSE_CTRL byte-read protocol."""
from __future__ import annotations

from wifit3.chips.rtl8192cu import constants as C

FAKE_MAC = bytes.fromhex("02aabbccddee")


def logical_map(*, did: int = 0x8176, svid: int = 0x10EC, smid: int = 0x0000, oemid: int = C.EEPROM_CID_DEFAULT,
                rf_opt1: int = 0x00, thermal: int = 0x0C) -> bytearray:
    """A 128-byte EFUSE logical map with a valid RTL8190_EEPROM_ID and FAKE_MAC."""
    m = bytearray(b"\xff" * C.HWSET_MAX_SIZE)
    m[0:2] = C.RTL8190_EEPROM_ID.to_bytes(2, "little")
    m[C.EEPROM_VID:C.EEPROM_VID + 2] = (0x0BDA).to_bytes(2, "little")
    m[C.EEPROM_DID:C.EEPROM_DID + 2] = did.to_bytes(2, "little")
    m[C.EEPROM_SVID:C.EEPROM_SVID + 2] = svid.to_bytes(2, "little")
    m[C.EEPROM_SMID:C.EEPROM_SMID + 2] = smid.to_bytes(2, "little")
    m[C.EEPROM_MAC_ADDR:C.EEPROM_MAC_ADDR + 6] = FAKE_MAC
    m[C.EEPROM_TXPOWERCCK:C.EEPROM_TXPOWERCCK + 6] = bytes([0x20, 0x21, 0x22, 0x23, 0x24, 0x25])
    m[C.EEPROM_TXPOWERHT40_1S:C.EEPROM_TXPOWERHT40_1S + 6] = bytes([0x1A, 0x1B, 0x1C, 0x02, 0x03, 0x04])
    m[C.EEPROM_TXPOWERHT40_2SDIFF:C.EEPROM_TXPOWERHT40_2SDIFF + 3] = bytes([0x21, 0x00, 0x00])
    m[C.EEPROM_TXPOWERHT20DIFF:C.EEPROM_TXPOWERHT20DIFF + 3] = bytes([0x8F, 0x00, 0x00])
    m[C.EEPROM_TXPOWER_OFDMDIFF:C.EEPROM_TXPOWER_OFDMDIFF + 3] = bytes([0x00, 0x00, 0x53])
    m[C.EEPROM_TXPWR_GROUP:C.EEPROM_TXPWR_GROUP + 6] = bytes([0x12, 0x00, 0x00, 0x34, 0x00, 0x00])
    m[C.EEPROM_CHANNELPLAN] = 0x0A
    m[C.EEPROM_TSSI_A] = 0x11
    m[C.EEPROM_TSSI_B] = 0x22
    m[C.EEPROM_THERMAL_METER] = thermal
    m[C.EEPROM_RF_OPT1] = rf_opt1
    m[C.EEPROM_VERSION:C.EEPROM_VERSION + 2] = bytes([0x03, 0x00])
    m[C.EEPROM_CUSTOMER_ID] = oemid
    return m


def encode_physical(logical: bytearray, extended_sections: tuple[int, ...] = ()) -> bytearray:
    """Pack every section with all four words present; ``extended_sections`` use the 2-byte header."""
    phys = bytearray()
    for section in range(C.EFUSE_MAX_SECTION):
        body = logical[section * 8:section * 8 + 8]
        if body == b"\xff" * 8:
            continue
        if section in extended_sections:
            phys += bytes([((section & 0x07) << 5) | 0x0F, ((section >> 3) << 4) | 0x0])
        else:
            phys.append((section << 4) | 0x0)
        phys += body
    return phys + b"\xff" * (C.EFUSE_REAL_CONTENT_LEN - len(phys))


class FakeRegisters:
    """Register file with the EFUSE_CTRL protocol; ``log`` records (kind, addr, width, value)."""

    def __init__(self, regs: dict[int, int] | None = None, efuse: bytearray | None = None) -> None:
        self.regs = dict(regs or {})
        self.efuse = efuse if efuse is not None else bytearray(b"\xff" * C.EFUSE_REAL_CONTENT_LEN)
        self.log: list[tuple[str, int, int, int]] = []
        self.chunks: list[tuple[int, bytes]] = []
        self._efuse_addr = 0

    def _value(self, addr: int, width: int) -> int:
        if addr == C.REG_EFUSE_CTRL and width == 4:
            return 0x80000000 | (self._efuse_addr << 8) | self.efuse[self._efuse_addr]
        if addr == C.REG_EFUSE_CTRL and width == 1:
            return self.efuse[self._efuse_addr]
        if addr == C.REG_EFUSE_CTRL + 3 and width == 1:
            return 0x80
        return self.regs.get(addr, 0) & ((1 << (8 * width)) - 1)

    def _read(self, addr: int, width: int) -> int:
        value = self._value(addr, width)
        self.log.append(("R", addr, width, value))
        return value

    def _write(self, addr: int, width: int, value: int) -> None:
        self.log.append(("W", addr, width, value))
        self.regs[addr] = value
        if addr == C.REG_EFUSE_CTRL + 1:
            self._efuse_addr = (self._efuse_addr & 0x300) | value
        elif addr == C.REG_EFUSE_CTRL + 2:
            self._efuse_addr = (self._efuse_addr & 0xFF) | ((value & 0x03) << 8)

    def read8(self, addr: int) -> int:
        return self._read(addr, 1)

    def read16(self, addr: int) -> int:
        return self._read(addr, 2)

    def read32(self, addr: int) -> int:
        return self._read(addr, 4)

    def write8(self, addr: int, value: int) -> None:
        self._write(addr, 1, value)

    def write16(self, addr: int, value: int) -> None:
        self._write(addr, 2, value)

    def write32(self, addr: int, value: int) -> None:
        self._write(addr, 4, value)

    def write_chunk(self, addr: int, data: bytes) -> None:
        self.log.append(("C", addr, len(data), int.from_bytes(bytes(data), "little")))
        self.chunks.append((addr, bytes(data)))

    def writes(self, addr: int) -> list[int]:
        return [v for (k, a, _w, v) in self.log if k == "W" and a == addr]

    def addrs(self, kind: str) -> list[int]:
        return [a for (k, a, _w, _v) in self.log if k == kind]


SYS_CFG_88C_TSMC = 0x04400735
NORMAL_SIE_EP_2OUT = {C.REG_NORMAL_SIE_EP + 1: 0x32, C.REG_NORMAL_SIE_EP + 2: 0x00}


class FakeChip(FakeRegisters):
    """FakeRegisters plus the hardware handshakes rtl92cu_hw_init polls on."""

    def __init__(self, regs: dict[int, int] | None = None, efuse: bytearray | None = None) -> None:
        super().__init__({C.REG_APS_FSMCO: C.PFM_ALDN, **(regs or {})}, efuse)

    def _value(self, addr: int, width: int) -> int:
        value = super()._value(addr, width)
        if addr == C.REG_APS_FSMCO and width == 2:
            value &= ~C.APFM_ONMAC
        elif addr == C.REG_LLT_INIT:
            value &= 0x3FFFFFFF
        elif addr == C.REG_MCUFWDL and width == 4:
            value |= C.FWDL_CHKSUM_RPT | C.WINTINI_RDY
        return value


def probed_efuse(pa_setting: int = 0xFF, **logical) -> bytearray:
    """A physical EFUSE image for logical_map(**logical) with ``pa_setting`` at byte 0x1FA."""
    phys = encode_physical(logical_map(**logical))
    phys[0x1FA] = pa_setting
    return phys
