"""mt76x02_mac_shared_key_setup + 64-slot init clear.

Kernel reference: mt76x02_mac.c:58-79 (`mt76x02_mac_shared_key_setup`),
mt76x2/usb_init.c:169-173 (16×4 clear loop at cold boot).
"""
import pytest

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import skey


class FakeTransport:
    """Records read32 + write32 + write_copy; serves reads from a backing dict."""

    def __init__(self, reads: dict[int, int] | None = None):
        self.reads = dict(reads or {})
        self.writes: list[tuple[int, int]] = []
        self.copies: list[tuple[int, bytes]] = []

    def read32(self, addr: int) -> int:
        return self.reads.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFFFFFFFF))

    def write_copy(self, addr: int, data: bytes) -> None:
        self.copies.append((addr, bytes(data)))


# ---------------------------------------------------------------------------
# Address macros
# ---------------------------------------------------------------------------

def test_skey_addr_vif0_key0_is_base_0():
    assert skey._skey_addr(0, 0) == C.MT_SKEY_BASE_0


def test_skey_addr_vif0_key3_offsets_by_3x32():
    assert skey._skey_addr(0, 3) == C.MT_SKEY_BASE_0 + 3 * 32


def test_skey_addr_vif1_key0_is_at_4x32_offset():
    assert skey._skey_addr(1, 0) == C.MT_SKEY_BASE_0 + 4 * 32


def test_skey_addr_vif7_key3_is_last_slot_of_base_0():
    """Last slot of SKEY_0: vif 7 key 3 = (4*7+3)*32 = 992 = 0x3E0."""
    assert skey._skey_addr(7, 3) == C.MT_SKEY_BASE_0 + 0x3E0


def test_skey_addr_vif8_key0_is_base_1():
    """vif & 8 routes to SKEY_1."""
    assert skey._skey_addr(8, 0) == C.MT_SKEY_BASE_1


def test_skey_addr_vif15_key3_is_last_slot_of_base_1():
    assert skey._skey_addr(15, 3) == C.MT_SKEY_BASE_1 + 0x3E0


# ---------------------------------------------------------------------------
# Mode-register addressing
# ---------------------------------------------------------------------------

def test_skey_mode_addr_vif0_and_vif1_share_register():
    """Vifs 0 and 1 → MT_SKEY_MODE_BASE_0 (no offset)."""
    assert skey._skey_mode_addr(0) == C.MT_SKEY_MODE_BASE_0
    assert skey._skey_mode_addr(1) == C.MT_SKEY_MODE_BASE_0


def test_skey_mode_addr_paired_vifs_share_registers():
    for pair_idx in range(4):
        even_vif = pair_idx * 2
        odd_vif = pair_idx * 2 + 1
        expected = C.MT_SKEY_MODE_BASE_0 + pair_idx * 4
        assert skey._skey_mode_addr(even_vif) == expected
        assert skey._skey_mode_addr(odd_vif) == expected


def test_skey_mode_addr_vif8_routes_to_base_1():
    assert skey._skey_mode_addr(8) == C.MT_SKEY_MODE_BASE_1
    assert skey._skey_mode_addr(9) == C.MT_SKEY_MODE_BASE_1
    assert skey._skey_mode_addr(15) == C.MT_SKEY_MODE_BASE_1 + 12


# ---------------------------------------------------------------------------
# Mode-shift field positions
# ---------------------------------------------------------------------------

def test_skey_mode_shift_even_vif_low_half():
    """Even vif occupies bits 0-15 (4 keys × 4 bits)."""
    assert skey._skey_mode_shift(0, 0) == 0
    assert skey._skey_mode_shift(0, 1) == 4
    assert skey._skey_mode_shift(0, 2) == 8
    assert skey._skey_mode_shift(0, 3) == 12


def test_skey_mode_shift_odd_vif_high_half():
    """Odd vif occupies bits 16-31."""
    assert skey._skey_mode_shift(1, 0) == 16
    assert skey._skey_mode_shift(1, 1) == 20
    assert skey._skey_mode_shift(1, 2) == 24
    assert skey._skey_mode_shift(1, 3) == 28


# ---------------------------------------------------------------------------
# mt76x02_mac_shared_key_setup with key=None — the init clear path
# ---------------------------------------------------------------------------

def test_shared_key_setup_vif0_key0_writes_mode_and_skey_copy():
    t = FakeTransport()
    skey.mt76x02_mac_shared_key_setup(t, vif_idx=0, key_idx=0, key=None)
    # Mode RMW write32 (cleared cipher field = 0 → register stays 0).
    assert t.writes == [(C.MT_SKEY_MODE_BASE_0, 0)]
    # Then ONE 32-byte copy of zeros to MT_SKEY(0, 0).
    assert t.copies == [(C.MT_SKEY_BASE_0, b"\x00" * C.MT76_SKEY_ENTRY_BYTES)]


def test_shared_key_setup_preserves_other_vif_cipher_bits():
    """Mode register holds 8 cipher fields. Clearing vif 0 key 0 must NOT
    touch the other 7 fields' bits."""
    # Pre-stuff mode register with all-ones EXCEPT we'll write 0 to the
    # vif 0 key 0 slot (bits 0-3).
    starting_val = 0xFFFFFFFF
    t = FakeTransport(reads={C.MT_SKEY_MODE_BASE_0: starting_val})
    skey.mt76x02_mac_shared_key_setup(t, vif_idx=0, key_idx=0, key=None)
    written_val = t.writes[0][1]
    # Bits 4-31 must be preserved; bits 0-3 must be cleared.
    assert (written_val & 0xF) == 0   # vif 0 key 0 cleared
    assert (written_val & ~0xF) == (starting_val & ~0xF)


def test_shared_key_setup_clears_correct_bits_for_vif1_key2():
    """Vif 1 key 2 → bits 24-27. Other bits preserved."""
    starting_val = 0xFFFFFFFF
    t = FakeTransport(reads={C.MT_SKEY_MODE_BASE_0: starting_val})
    skey.mt76x02_mac_shared_key_setup(t, vif_idx=1, key_idx=2, key=None)
    written_val = t.writes[0][1]
    target_mask = 0xF << 24
    assert (written_val & target_mask) == 0
    assert (written_val & ~target_mask) == (starting_val & ~target_mask)


def test_shared_key_setup_with_key_raises_not_implemented():
    """Wifit3 only does NULL clears (software crypto for the WEP suite)."""
    t = FakeTransport()
    with pytest.raises(NotImplementedError):
        skey.mt76x02_mac_shared_key_setup(
            t, vif_idx=0, key_idx=0, key=b"\x00" * 16
        )


# ---------------------------------------------------------------------------
# shared_key_table_clear — full init loop
# ---------------------------------------------------------------------------

def test_shared_key_table_clear_write_count():
    """16 vifs × 4 keys × (1 mode write32 + 1 skey copy) = 64 writes + 64 copies."""
    t = FakeTransport()
    skey.shared_key_table_clear(t)
    assert len(t.writes) == 16 * 4
    assert len(t.copies) == 16 * 4


def test_shared_key_table_clear_all_writes_zero():
    """Init clear leaves all regs at 0 (default backing dict is all-zero)."""
    t = FakeTransport()
    skey.shared_key_table_clear(t)
    for _, val in t.writes:
        assert val == 0
    for _, buf in t.copies:
        assert buf == b"\x00" * C.MT76_SKEY_ENTRY_BYTES


def test_shared_key_table_clear_hits_all_8_mode_registers():
    """The 4 MODE_0 + 4 MODE_1 = 8 unique mode registers should each be
    written 8 times (2 paired vifs × 4 keys)."""
    t = FakeTransport()
    skey.shared_key_table_clear(t)
    mode_addrs_written = [
        addr for addr, _ in t.writes
        if addr in {
            C.MT_SKEY_MODE_BASE_0 + i * 4 for i in range(4)
        } | {
            C.MT_SKEY_MODE_BASE_1 + i * 4 for i in range(4)
        }
    ]
    # Each mode register hit 8 times = 64 mode writes total.
    assert len(mode_addrs_written) == 64
    unique = set(mode_addrs_written)
    assert len(unique) == 8


def test_shared_key_table_clear_covers_all_64_key_regions():
    """64 distinct MT_SKEY base addresses, each cleared by one 32-byte copy."""
    t = FakeTransport()
    skey.shared_key_table_clear(t)
    skey_bases = {addr for addr, _ in t.copies}
    assert len(skey_bases) == 64   # 16 vifs × 4 keys
