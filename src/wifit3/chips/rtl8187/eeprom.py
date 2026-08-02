"""93cx6 EEPROM bit-bang reader for the RTL8187L.

Port of the 93cx6 SPI lib (``drivers/misc/eeprom/eeprom_93cx6.c``) driven through the
RTL8187's EEPROM_CMD register callbacks (``rtl8187_eeprom_register_{read,write}``,
``dev.c``). The chip's permanent MAC, per-channel TX power and ``txpwr_base`` live in this
93cx6 SPI EEPROM; the kernel probe bit-bangs them out before ``init_hw``, and the
per-channel TX power it reads here feeds every ``set_tx_power`` (RF init + channel tune).

Every bit clocked drives EEPROM_CMD in PROGRAM mode with the four SPI lines (CS/CK/DI/DO),
and every value read back samples DO on the same register — so the read is fully on the
wire and the acceptance gate reproduces it byte-for-byte.

[SRC] ``driver_sources/rtl818x-source-v6.18/eeprom_93cx6.c`` (the lib) +
``rtl8187/dev.c`` (the EEPROM_CMD callbacks).
"""
from __future__ import annotations

from .constants import (
    EEPROM_CMD_CK,
    EEPROM_CMD_CS,
    EEPROM_CMD_PROGRAM,
    EEPROM_CMD_READ,
    EEPROM_CMD_WRITE,
    REG_EEPROM_CMD,
    REG_RX_CONF,
)
from .transport import RTL8187Transport

# [SRC] include/linux/eeprom_93cx6.h
PCI_EEPROM_WIDTH_93C46 = 6
PCI_EEPROM_WIDTH_93C66 = 8
PCI_EEPROM_WIDTH_OPCODE = 3
PCI_EEPROM_READ_OPCODE = 0x06


def eeprom_width(t: RTL8187Transport) -> int:
    """93c66 (8-bit addr) iff RX_CONF bit 6 is set, else 93c46 (6-bit). [SRC] dev.c:1496."""
    if t.read32(REG_RX_CONF) & (1 << 6):
        return PCI_EEPROM_WIDTH_93C66
    return PCI_EEPROM_WIDTH_93C46


class Eeprom93cx6:
    """Bit-bang state machine over EEPROM_CMD. The four SPI lines mirror the kernel's
    ``struct eeprom_93cx6`` flags; ``register_{read,write}`` are the rtl8187 callbacks."""

    def __init__(self, t: RTL8187Transport, width: int):
        self.t = t
        self.width = width
        self.data_in = 0      # DI we drive to the chip   (EEPROM_CMD_WRITE bit)
        self.data_out = 0     # DO the chip drives back    (EEPROM_CMD_READ bit)
        self.clock = 0
        self.cs = 0

    # rtl8187_eeprom_register_read (dev.c) — sample EEPROM_CMD back; DO rides bit 0.
    def _reg_read(self) -> None:
        reg = self.t.read8(REG_EEPROM_CMD)
        self.data_in = bool(reg & EEPROM_CMD_WRITE)
        self.data_out = bool(reg & EEPROM_CMD_READ)
        self.clock = bool(reg & EEPROM_CMD_CK)
        self.cs = bool(reg & EEPROM_CMD_CS)

    # rtl8187_eeprom_register_write (dev.c) — PROGRAM mode + the four SPI lines.
    def _reg_write(self) -> None:
        reg = EEPROM_CMD_PROGRAM
        if self.data_in:
            reg |= EEPROM_CMD_WRITE
        if self.data_out:
            reg |= EEPROM_CMD_READ
        if self.clock:
            reg |= EEPROM_CMD_CK
        if self.cs:
            reg |= EEPROM_CMD_CS
        self.t.write8(REG_EEPROM_CMD, reg)
        # kernel udelay(10) — not needed under replay; harmless to omit on HW.

    def _pulse_high(self) -> None:
        self.clock = 1
        self._reg_write()

    def _pulse_low(self) -> None:
        self.clock = 0
        self._reg_write()

    def _startup(self) -> None:
        self._reg_read()
        self.data_in = 0
        self.data_out = 0
        self.clock = 0
        self.cs = 1
        self._reg_write()
        self._pulse_high()
        self._pulse_low()

    def _cleanup(self) -> None:
        self._reg_read()
        self.data_in = 0
        self.cs = 0
        self._reg_write()
        self._pulse_high()
        self._pulse_low()

    def _write_bits(self, data: int, count: int) -> None:
        self._reg_read()
        self.data_in = 0
        self.data_out = 0
        for i in range(count, 0, -1):
            self.data_in = 1 if (data & (1 << (i - 1))) else 0
            self._reg_write()
            self._pulse_high()
            self._pulse_low()
        self.data_in = 0
        self._reg_write()

    def _read_bits(self, count: int) -> int:
        self._reg_read()
        self.data_in = 0
        self.data_out = 0
        buf = 0
        for i in range(count, 0, -1):
            self._pulse_high()
            self._reg_read()
            self.data_in = 0
            if self.data_out:
                buf |= 1 << (i - 1)
            self._pulse_low()
        return buf

    def read(self, word: int) -> int:
        """Read one 16-bit word at ``word`` (host order). [SRC] eeprom_93cx6_read."""
        self._startup()
        command = (PCI_EEPROM_READ_OPCODE << self.width) | word
        self._write_bits(command, PCI_EEPROM_WIDTH_OPCODE + self.width)
        # rtl8187 sets no quirks → no has_quirk_extra_read_cycle pulse.
        data = self._read_bits(16)
        self._cleanup()
        return data

    def multiread(self, word: int, words: int) -> list[int]:
        """Read ``words`` consecutive 16-bit words. [SRC] eeprom_93cx6_multiread."""
        return [self.read(word + i) for i in range(words)]
