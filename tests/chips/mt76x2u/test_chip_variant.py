"""is_mt7612 chip-strap discriminator (0x7612 WiFi-only vs 0x7662/0x7632 combo).

Two runtime-gated branches key on `is_mt7612`:
  - mac_reset: COEXCFG0 BT-coexistence clear runs only for 0x7612. [SRC] usb_mac.c:84
  - load_rom_patch: MT_MCU_SEMAPHORE_03 acquire/release runs only for the combo
    (rom_protect = !is_mt7612). [SRC] usb_mcu.c:59,65-70,138-139

The reference 0x7612 (`is_mt7612=True`, the default) path must be unchanged:
COEXCFG0 still cleared, semaphore never touched.
"""
from unittest.mock import MagicMock

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import firmware, mac


class FakeTransport:
    """Records writes + rmws; default reads return 0. Stubs the bits the
    ROM-patch vendor helpers poke that a register dict can't model."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []
        self.rmws: list[tuple[int, int, int]] = []
        self.regs: dict[int, int] = {}
        self.dev = MagicMock()
        self.timeout_ms = 1000

    def read32(self, addr: int) -> int:
        return self.regs.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        value &= 0xFFFFFFFF
        self.regs[addr] = value
        self.writes.append((addr, value))

    def rmw32(self, addr: int, mask: int, value: int) -> None:
        cur = self.regs.get(addr, 0)
        self.regs[addr] = ((cur & ~mask) | (value & mask)) & 0xFFFFFFFF
        self.rmws.append((addr, mask, value))

    def single_wr_fce(self, addr: int, val: int) -> None:
        self.writes.append((addr, val))

    def vendor_dev_mode(self, wvalue: int) -> None:
        pass


_COEX_RMW = (C.MT_COEXCFG0, C.MT_COEXCFG0_COEX_EN, 0)


# ---------------------------------------------------------------------------
# mac_reset — COEXCFG0 clear gated on is_mt7612
# ---------------------------------------------------------------------------
async def test_mac_reset_clears_coex_for_mt7612_default():
    """Reference 0x7612 (default): COEXCFG0 COEX_EN is cleared."""
    t = FakeTransport()
    assert await mac.mac_reset(t) is True
    assert _COEX_RMW in t.rmws


async def test_mac_reset_skips_coex_for_combo():
    """WiFi+BT combo (is_mt7612=False): COEXCFG0 is left enabled."""
    t = FakeTransport()
    assert await mac.mac_reset(t, is_mt7612=False) is True
    assert _COEX_RMW not in t.rmws


# ---------------------------------------------------------------------------
# load_rom_patch — MT_MCU_SEMAPHORE_03 acquire/release gated on !is_mt7612
# ---------------------------------------------------------------------------
def _stub_upload(monkeypatch) -> list[int]:
    """Neutralise the heavy upload so only the semaphore bracket is exercised.
    Returns a list that captures every addr passed to _poll_reg32_msec."""
    polled: list[int] = []

    async def _fake_poll(transport, addr, mask, expected, timeout_ms):
        polled.append(addr)
        return True

    async def _fake_send(*a, **k):
        return True

    monkeypatch.setattr(firmware, "_poll_reg32_msec", _fake_poll)
    monkeypatch.setattr(firmware, "_send_fw_chunks", _fake_send)
    monkeypatch.setattr(firmware, "_load_asset", lambda name, size: b"\x00" * size)
    return polled


async def test_rom_patch_skips_semaphore_for_mt7612_default(monkeypatch):
    """Reference 0x7612 (default): MT_MCU_SEMAPHORE_03 is never read or written."""
    _stub_upload(monkeypatch)
    t = FakeTransport()
    assert await firmware.load_rom_patch(t, asic_rev=C.MT76XX_REV_E3) is True
    sem_writes = [w for w in t.writes if w[0] == C.MT_MCU_SEMAPHORE_03]
    assert sem_writes == []


async def test_rom_patch_acquires_and_releases_semaphore_for_combo(monkeypatch):
    """Combo strap (is_mt7612=False): acquire (poll SEMAPHORE_03) then release
    (write 1)."""
    polled = _stub_upload(monkeypatch)
    t = FakeTransport()
    assert await firmware.load_rom_patch(
        t, asic_rev=C.MT76XX_REV_E3, is_mt7612=False
    ) is True
    # Acquire: the first poll targets the semaphore register.
    assert polled[0] == C.MT_MCU_SEMAPHORE_03
    # Release: write 1 to the semaphore at the end.
    assert (C.MT_MCU_SEMAPHORE_03, 1) in t.writes


async def test_rom_patch_release_on_upload_failure_for_combo(monkeypatch):
    """A mid-upload failure still releases the semaphore (kernel's `out:`)."""
    _stub_upload(monkeypatch)

    async def _fail_send(*a, **k):
        return False

    monkeypatch.setattr(firmware, "_send_fw_chunks", _fail_send)
    t = FakeTransport()
    assert await firmware.load_rom_patch(
        t, asic_rev=C.MT76XX_REV_E3, is_mt7612=False
    ) is False
    assert (C.MT_MCU_SEMAPHORE_03, 1) in t.writes


async def test_rom_patch_combo_semaphore_acquire_failure_aborts(monkeypatch):
    """If the semaphore can't be acquired, the load aborts before any upload."""
    async def _fake_poll_fail(transport, addr, mask, expected, timeout_ms):
        return False

    async def _fake_send(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("upload must not start without the semaphore")

    monkeypatch.setattr(firmware, "_poll_reg32_msec", _fake_poll_fail)
    monkeypatch.setattr(firmware, "_send_fw_chunks", _fake_send)
    monkeypatch.setattr(firmware, "_load_asset", lambda name, size: b"\x00" * size)
    t = FakeTransport()
    assert await firmware.load_rom_patch(
        t, asic_rev=C.MT76XX_REV_E3, is_mt7612=False
    ) is False
    # No release either — nothing was acquired.
    assert (C.MT_MCU_SEMAPHORE_03, 1) not in t.writes
