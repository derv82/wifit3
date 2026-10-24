"""Register-file fake for the rtl8192fu transport: reads return the stored value, writes are logged."""
from __future__ import annotations

from typing import Callable

from wifit3.chips.rtl8192fu.core import Rtl8xxxuPriv
from wifit3.chips.rtl8192fu.rtl8192f import rtl8192fu_fops


class FakeTransport:
    def __init__(self, regs: dict[int, int] | None = None) -> None:
        self.regs = dict(regs or {})
        self.read_hooks: dict[int, Callable[[], int]] = {}
        self.ops: list[tuple[str, int, int, int]] = []
        self.bulk: list[tuple[int, bytes]] = []

    def _read(self, address: int, width: int) -> int:
        hook = self.read_hooks.get(address)
        value = hook() if hook else self.regs.get(address, 0)
        value &= (1 << (8 * width)) - 1
        self.ops.append(("R", address, width, value))
        return value

    def _write(self, address: int, width: int, value: int) -> None:
        self.regs[address] = value
        self.ops.append(("W", address, width, value))

    def read8(self, address: int) -> int:
        return self._read(address, 1)

    def read16(self, address: int) -> int:
        return self._read(address, 2)

    def read32(self, address: int) -> int:
        return self._read(address, 4)

    def write8(self, address: int, value: int) -> None:
        self._write(address, 1, value)

    def write16(self, address: int, value: int) -> None:
        self._write(address, 2, value)

    def write32(self, address: int, value: int) -> None:
        self._write(address, 4, value)

    def write_block(self, address: int, data: bytes) -> None:
        self._write(address, len(data), int.from_bytes(data, "little"))

    def bulk_write(self, endpoint: int, data: bytes) -> None:
        self.bulk.append((endpoint, bytes(data)))

    def writes(self) -> list[tuple[int, int, int]]:
        return [(address, width, value) for kind, address, width, value in self.ops if kind == "W"]


def make_priv(regs: dict[int, int] | None = None) -> tuple[Rtl8xxxuPriv, FakeTransport]:
    transport = FakeTransport(regs)
    return Rtl8xxxuPriv(transport, rtl8192fu_fops), transport
