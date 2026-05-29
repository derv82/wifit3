"""mt76x02_mac_wcid_setup + 256-slot init clear.

Kernel reference: mt76x02_mac.c:148-167 (`mt76x02_mac_wcid_setup`),
mt76x2/usb_init.c:165-167 (the for-loop that clears all 256 slots at
cold boot).
"""
import pytest

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import wcid


class FakeTransport:
    """Records every write32 call as (addr, value)."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []

    def write32(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFFFFFFFF))


# ---------------------------------------------------------------------------
# mt76x02_mac_wcid_setup — single-slot semantics
# ---------------------------------------------------------------------------

def test_wcid_setup_idx0_no_mac_writes_attr_and_addr():
    """Slot 0, vif=0, mac=None → ATTR=0 + ADDR low=0 + ADDR high=0."""
    t = FakeTransport()
    wcid.mt76x02_mac_wcid_setup(t, idx=0, vif_idx=0, mac=None)
    assert t.writes == [
        (C.MT_WCID_ATTR_BASE + 0, 0),         # ATTR(0) = 0xa800
        (C.MT_WCID_ADDR_BASE + 0, 0),         # ADDR(0) lo = 0x1800
        (C.MT_WCID_ADDR_BASE + 4, 0),         # ADDR(0) hi = 0x1804
    ]


def test_wcid_setup_idx128_writes_attr_only():
    """Kernel early-returns at `idx >= 128` — no ADDR write."""
    t = FakeTransport()
    wcid.mt76x02_mac_wcid_setup(t, idx=128, vif_idx=0, mac=None)
    assert t.writes == [
        (C.MT_WCID_ATTR_BASE + 128 * 4, 0),   # ATTR(128) = 0xaa00
    ]


def test_wcid_setup_idx255_writes_attr_only():
    """Same: highest slot, ATTR only."""
    t = FakeTransport()
    wcid.mt76x02_mac_wcid_setup(t, idx=255, vif_idx=0, mac=None)
    assert t.writes == [
        (C.MT_WCID_ATTR_BASE + 255 * 4, 0),   # 0xa800 + 0x3fc = 0xabfc
    ]


def test_wcid_setup_vif_idx_packs_into_attr_bss_idx():
    """vif_idx 0..7 → BSS_IDX field (bits 6:4)."""
    t = FakeTransport()
    wcid.mt76x02_mac_wcid_setup(t, idx=10, vif_idx=5, mac=None)
    expected_attr = (5 & 0x7) << 4   # 0x50
    assert t.writes[0] == (C.MT_WCID_ATTR_BASE + 10 * 4, expected_attr)


def test_wcid_setup_vif_idx_8_sets_ext_bit():
    """vif_idx bit 3 (=8) sets BSS_IDX_EXT (bit 11)."""
    t = FakeTransport()
    wcid.mt76x02_mac_wcid_setup(t, idx=10, vif_idx=8, mac=None)
    # vif_idx=8: BSS_IDX = 0, EXT = 1 → attr = BIT(11) = 0x800
    assert t.writes[0] == (C.MT_WCID_ATTR_BASE + 10 * 4, 1 << 11)


def test_wcid_setup_vif_idx_combined_low_and_ext():
    """vif_idx=12 = 0b1100: BSS_IDX = 4, EXT = 1 → attr = (4<<4)|(1<<11)."""
    t = FakeTransport()
    wcid.mt76x02_mac_wcid_setup(t, idx=10, vif_idx=12, mac=None)
    expected = (4 << 4) | (1 << 11)
    assert t.writes[0] == (C.MT_WCID_ATTR_BASE + 10 * 4, expected)


def test_wcid_setup_mac_writes_le_into_addr():
    """6-byte MAC → low4 = u32_le(mac[0..3]), high4 = u16_le(mac[4..5])."""
    t = FakeTransport()
    mac = bytes.fromhex("AABBCCDDEEFF")
    wcid.mt76x02_mac_wcid_setup(t, idx=3, vif_idx=0, mac=mac)
    # ATTR write first
    assert t.writes[0] == (C.MT_WCID_ATTR_BASE + 12, 0)
    # ADDR low = "DD CC BB AA" → 0xDDCCBBAA
    assert t.writes[1] == (C.MT_WCID_ADDR_BASE + 24, 0xDDCCBBAA)
    # ADDR high = "FF EE" → 0x0000FFEE
    assert t.writes[2] == (C.MT_WCID_ADDR_BASE + 28, 0xFFEE)


def test_wcid_setup_rejects_wrong_length_mac():
    t = FakeTransport()
    with pytest.raises(ValueError):
        wcid.mt76x02_mac_wcid_setup(t, idx=0, vif_idx=0, mac=b"\x00" * 5)


# ---------------------------------------------------------------------------
# wcid_table_clear — full init loop
# ---------------------------------------------------------------------------

def test_wcid_table_clear_writes_correct_total_count():
    """256 slots × 1 ATTR + 128 slots × 2 ADDR (low + high) = 512 writes."""
    t = FakeTransport()
    wcid.wcid_table_clear(t)
    assert len(t.writes) == 256 + 128 * 2


def test_wcid_table_clear_all_writes_zero():
    """Init clear writes all zeros (vif=0, mac=None)."""
    t = FakeTransport()
    wcid.wcid_table_clear(t)
    for _, val in t.writes:
        assert val == 0, f"non-zero write during clear: {val:#x}"


def test_wcid_table_clear_attr_addresses_are_dense():
    """ATTR writes hit every slot 0..255 in order, stride 4."""
    t = FakeTransport()
    wcid.wcid_table_clear(t)
    attr_addrs = [
        addr for addr, _ in t.writes
        if C.MT_WCID_ATTR_BASE <= addr < C.MT_WCID_ATTR_BASE + 256 * 4
    ]
    assert len(attr_addrs) == 256
    assert attr_addrs == [C.MT_WCID_ATTR_BASE + i * 4 for i in range(256)]


def test_wcid_table_clear_addr_only_first_128():
    """ADDR writes only for slots 0-127 (kernel early-returns at >= 128)."""
    t = FakeTransport()
    wcid.wcid_table_clear(t)
    addr_addrs = sorted({
        addr for addr, _ in t.writes
        if C.MT_WCID_ADDR_BASE <= addr < C.MT_WCID_ADDR_BASE + 128 * 8
    })
    # 128 slots × 2 words (low + high) = 256 unique addresses, stride 4.
    assert len(addr_addrs) == 128 * 2
    expected = sorted(
        [C.MT_WCID_ADDR_BASE + i * 8 for i in range(128)] +
        [C.MT_WCID_ADDR_BASE + i * 8 + 4 for i in range(128)]
    )
    assert addr_addrs == expected
    # Nothing above slot 127 in ADDR range.
    too_high = [
        addr for addr, _ in t.writes
        if addr >= C.MT_WCID_ADDR_BASE + 128 * 8
        and addr < C.MT_WCID_ATTR_BASE
    ]
    assert too_high == []
