"""Byte-addressed recording mock for the RTL8192EUTransport register protocol.

Every helper in ``chips/rtl8192eu`` talks to the transport's read8/16/32 and
write8/16/32 methods, so a plain dict-backed register file exercises the real
code paths without USB. Writes are recorded as ``(op, addr, value)`` tuples so
tests can assert the exact wire values (including read-modify-write results).
"""
from __future__ import annotations


class MockTransport:
    """Register file + access log; mimics a little-endian MAC register space."""

    def __init__(self) -> None:
        self.regs: dict[int, int] = {}
        self.writes: list[tuple[str, int, int]] = []
        self.reads: list[tuple[str, int]] = []

    # ---- seeding / inspection ------------------------------------------
    def seed(self, addr: int, value: int, size: int = 4) -> None:
        """Preload a register (little-endian, ``size`` bytes)."""
        self._store(addr, [(value >> (8 * i)) & 0xFF for i in range(size)])

    def _store(self, addr: int, data: list[int]) -> None:
        for i, byte in enumerate(data):
            self.regs[addr + i] = byte & 0xFF

    def _load(self, addr: int, size: int) -> int:
        out = 0
        for i in range(size):
            out |= self.regs.get(addr + i, 0) << (8 * i)
        return out

    def writes_to(self, addr: int) -> list[tuple[str, int, int]]:
        return [w for w in self.writes if w[1] == addr]

    def last_write_to(self, addr: int) -> int:
        return self.writes_to(addr)[-1][2]

    # ---- register access ------------------------------------------------
    def read8(self, addr: int) -> int:
        self.reads.append(("r8", addr))
        return self._load(addr, 1)

    def read16(self, addr: int) -> int:
        self.reads.append(("r16", addr))
        return self._load(addr, 2)

    def read32(self, addr: int) -> int:
        self.reads.append(("r32", addr))
        return self._load(addr, 4)

    def write8(self, addr: int, val: int) -> None:
        self.writes.append(("w8", addr, val & 0xFF))
        self._store(addr, [val & 0xFF])

    def write16(self, addr: int, val: int) -> None:
        self.writes.append(("w16", addr, val & 0xFFFF))
        self._store(addr, [val & 0xFF, (val >> 8) & 0xFF])

    def write32(self, addr: int, val: int) -> None:
        self.writes.append(("w32", addr, val & 0xFFFFFFFF))
        self._store(addr, [(val >> (8 * i)) & 0xFF for i in range(4)])

    def write_block(self, addr: int, data: bytes) -> None:
        self.writes.append(("block", addr, bytes(data)))
        self._store(addr, list(data))


class FailingReadTransport(MockTransport):
    """Raises IOError on every read (the USB-unplug / wedged-pipe case)."""

    def read8(self, addr: int) -> int:
        raise IOError("usb read failed")

    def read16(self, addr: int) -> int:
        raise IOError("usb read failed")

    def read32(self, addr: int) -> int:
        raise IOError("usb read failed")
