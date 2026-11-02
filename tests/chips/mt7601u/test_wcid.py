"""WCID allocation, ported from main.c:187-204 (mt76_wcid_alloc) and mac.c:355 (wcid_setup).

No hardware: the transport is a recorder. What matters here is that the allocator hands
out the same slots the kernel would, and that a slot's registers are written in the
kernel's order -- a TX descriptor carries a WCID that must resolve to a valid slot.
"""
from __future__ import annotations

import pytest

from wifit3.chips.mt7601u.constants import (
    MT_WCID_ADDR,
    MT_WCID_ATTR,
    MT_WCID_ATTR_BSS_IDX,
    MT_WCID_ATTR_BSS_IDX_SHIFT,
    MT_WCID_ATTR_BSS_IDX_EXT,
)
from wifit3.chips.mt7601u.wcid import (
    WCID_ALLOC_LIMIT,
    WCID_COUNT,
    WCID_MONITOR,
    WcidAllocator,
    wcid_setup,
)


class TestAllocator:
    def test_wcid_zero_is_reserved_for_the_broadcast_station(self) -> None:
        """init.c:583 -- 'Reserve WCID 0 for mcast'. Handing it out would make the card
        answer for the broadcast STA."""
        a = WcidAllocator()
        assert a.is_used(0)
        assert a.alloc() == 1

    def test_allocation_returns_the_lowest_free_slot(self) -> None:
        """main.c:192 takes the first ffs(~mask), which is the lowest clear bit."""
        a = WcidAllocator()
        assert [a.alloc() for _ in range(4)] == [1, 2, 3, 4]

    def test_a_freed_slot_is_reused_before_a_higher_one(self) -> None:
        a = WcidAllocator()
        for _ in range(5):
            a.alloc()
        a.free(2)
        assert a.alloc() == 2

    def test_freeing_an_unallocated_slot_is_harmless(self) -> None:
        """main.c:250 clears the bit unconditionally; a double free must not corrupt."""
        a = WcidAllocator()
        a.free(7)
        a.free(7)
        assert a.alloc() == 1        # lowest free slot, so slot 7 is not reached yet

    def test_freeing_wcid_zero_does_not_release_the_broadcast_slot(self) -> None:
        """The broadcast reservation is not a real station and nothing frees it."""
        a = WcidAllocator()
        a.free(0)
        assert a.is_used(0)
        assert a.alloc() == 1

    def test_out_of_range_indices_read_as_unused_and_free_is_ignored(self) -> None:
        a = WcidAllocator()
        assert not a.is_used(-1)
        assert not a.is_used(WCID_COUNT)
        a.free(-1)
        a.free(WCID_COUNT + 5)          # must not raise

    def test_allocation_stops_at_the_kernel_limit(self) -> None:
        """main.c:203 -- idx > 119 is refused. 128 slots exist but 120-127 are unusable,
        so exhausting them must report failure rather than hand out a slot the kernel
        would never have used."""
        a = WcidAllocator()
        for _ in range(WCID_ALLOC_LIMIT - 1):     # WCID 0 was already taken
            assert a.alloc() is not None
        assert a.alloc() is None
        assert WCID_ALLOC_LIMIT == 120

    def test_the_monitor_wcid_is_not_a_real_slot(self) -> None:
        """init.c:590 mon_wcid->idx = 0xff, outside N_WCIDS. The allocator must never
        return it, or a monitor frame would name an unmapped slot."""
        a = WcidAllocator()
        for _ in range(WCID_ALLOC_LIMIT - 1):
            assert a.alloc() != WCID_MONITOR
        assert WCID_MONITOR == 0xFF
        assert WCID_MONITOR >= WCID_COUNT


class TestWcidSetup:
    def _recorder(self):
        writes: list[tuple[int, int]] = []

        class FakeTransport:
            def wr(self, offset: int, val: int) -> None:
                writes.append((offset, val))

            def addr_wr(self, offset: int, addr: bytes) -> None:
                writes.append((offset, int.from_bytes(addr, "little")))

        return FakeTransport(), writes

    def test_a_slot_is_written_attribute_first_then_address(self) -> None:
        """mac.c:355 writes MT_WCID_ATTR(idx) then MT_WCID_ADDR(idx); the register order
        is not arbitrary -- the address must not be visible before the slot is enabled."""
        tp, writes = self._recorder()
        wcid_setup(tp, 3, 0, bytes.fromhex("aabbccddeeff"))

        assert [off for off, _ in writes] == [MT_WCID_ATTR(3), MT_WCID_ADDR(3)]
        assert writes[0][1] == 0
        assert writes[1][1] == 0xFFEEDDCCBBAA

    def test_the_bss_index_is_split_across_both_attr_fields(self) -> None:
        """mac.c:359-360 -- vif_idx & 7 in BSS_IDX, bit 3 in BSS_IDX_EXT."""
        tp, writes = self._recorder()
        wcid_setup(tp, 1, 9, b"\x01\x02\x03\x04\x05\x06")
        attr = writes[0][1]
        assert (attr & MT_WCID_ATTR_BSS_IDX) >> MT_WCID_ATTR_BSS_IDX_SHIFT == 9 & 7
        assert bool(attr & MT_WCID_ATTR_BSS_IDX_EXT) is True

    def test_a_low_vif_index_leaves_the_ext_bit_clear(self) -> None:
        tp, writes = self._recorder()
        wcid_setup(tp, 1, 3, b"\x01\x02\x03\x04\x05\x06")
        assert not writes[0][1] & MT_WCID_ATTR_BSS_IDX_EXT

    def test_releasing_a_slot_writes_the_zero_address(self) -> None:
        """main.c:251 calls wcid_setup(idx, 0, NULL) on sta_remove. That is the all-zero
        address, NOT the 0xFFFFFFFF marker init.c:433 used -- matching the kernel exactly
        is the point."""
        tp, writes = self._recorder()
        wcid_setup(tp, 4, 0, None)
        assert writes[1][1] == 0

    @pytest.mark.parametrize("mac", [bytes(6), bytes.fromhex("001122334455")])
    def test_a_short_or_normal_address_is_written_whole(self, mac: bytes) -> None:
        tp, writes = self._recorder()
        wcid_setup(tp, 2, 0, mac)
        assert writes[1][1] == int.from_bytes(mac, "little")

    def test_the_bss_index_lands_in_the_field_not_onto_the_mask(self) -> None:
        """Regression: masking the index onto MT_WCID_ATTR_BSS_IDX instead of shifting it
        into the field writes 0 for every vif_idx, so the silicon attributes the station to
        BSS 0 regardless of which interface it belongs to."""
        tp, writes = self._recorder()
        for vif_idx in range(8):
            wcid_setup(tp, 1, vif_idx, b"\x01\x02\x03\x04\x05\x06")
            attr = writes[-2][1]
            assert (attr & MT_WCID_ATTR_BSS_IDX) >> MT_WCID_ATTR_BSS_IDX_SHIFT == vif_idx
