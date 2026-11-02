"""Station memory and WCID allocation for MT7601U.

Two related ports:

Station memory clears are from driver_sources/mt7601u-source-v7.2/mt7601u/init.c --
mt7601u_init_wcid_mem, mt7601u_init_key_mem and mt7601u_init_wcid_attr_mem. The kernel
runs all three during init, before the MAC register tables; monitor-mode receive tolerates
skipping them, but a TX descriptor carries a wcid that must resolve to a valid slot. The
values are the kernel's literals, verified op-for-op against the recorded bursts in
capture-1 and capture-2 by verify_mt7601u.py stage `wcid`.

Allocation is from main.c (mt76_wcid_alloc, sta_remove) and mac.c:355 (wcid_setup), which
the clears alone do not provide: without a way to claim a slot, PMKID, WPS and AP mode have
nothing to name a station with.

**Not yet wired into association.** Nothing calls `wcid_setup` at runtime yet, because the
Driver ABC has no association surface to call it from and adding one would force every other
port to implement a method it does not need. What is here is the kernel's allocation and
register sequencing, ready for that contract to exist.
"""
from __future__ import annotations

from typing import Optional

from typing import Iterable

from .constants import (
    MT_MAX_LEN_CFG,
    MT_MAX_LEN_CFG_AMPDU,
    MT_SKEY_MODE_BASE_0,
    MT_WCID_ADDR,
    MT_WCID_ADDR_BASE,
    MT_WCID_ATTR,
    MT_WCID_ATTR_BASE,
    MT_WCID_ATTR_BSS_IDX_EXT,
    MT_WCID_ATTR_BSS_IDX_SHIFT,
    MT_WCID_DROP,
    MT_WCID_DROP_MASK,
    _field_prep,
)
from .mcu import MT7601UMcu
from .transport import MT7601UTransport

WCID_COUNT = 128
"""mt7601u.h:105 N_WCIDS. Each station slot occupies two words in the WCID table."""

WCID_ADDR_INVALID = 0xFFFFFFFF
"""init.c:182 -- the "no address" marker for an unused WCID slot."""
WCID_ADDR_MASK = 0x00FFFFFF
"""init.c:183 -- the address bytes inside that word."""


def init_wcid_mem(mcu: MT7601UMcu) -> None:
    """init.c mt7601u_init_wcid_mem -- mark every station slot as unassigned."""
    pair = [WCID_ADDR_INVALID, WCID_ADDR_MASK]
    mcu.burst_write_regs(MT_WCID_ADDR_BASE, pair * WCID_COUNT)


def init_key_mem(mcu: MT7601UMcu) -> None:
    """init.c mt7601u_init_key_mem -- four zero words, which is also the broadcast STA."""
    mcu.burst_write_regs(MT_SKEY_MODE_BASE_0, [0, 0, 0, 0])


def init_wcid_attr_mem(mcu: MT7601UMcu) -> None:
    """init.c mt7601u_init_wcid_attr_mem -- every slot enabled, pairwise off."""
    mcu.burst_write_regs(MT_WCID_ATTR_BASE, [1] * (WCID_COUNT * 2))


def init_station_memory(mcu: MT7601UMcu) -> None:
    """All three clears, in the order init.c runs them."""
    init_wcid_mem(mcu)
    init_key_mem(mcu)
    init_wcid_attr_mem(mcu)


WCID_MONITOR = 0xFF
"""init.c:590 dev->mon_wcid->idx. A monitor-mode TX descriptor carries this so the frame
addresses no station; the DMA ring keeps WCID 0 for the real broadcast STA."""

WCID_ALLOC_LIMIT = 120
"""main.c:203 -- an idx above 119 is refused, so slots 120-127 stay unallocatable even
though N_WCIDS is 128. WCID 0 is already taken by the broadcast STA (init.c:583)."""


class WcidAllocator:
    """The station-slot allocator, ported from main.c:187-204 mt76_wcid_alloc.

    Bitmask by design, matching the kernel's wcid_mask, so allocation and release are the
    same operation the driver does rather than a divergent scheme that could hand the
    silicon a slot the kernel would not.
    """

    def __init__(self, count: int = WCID_COUNT) -> None:
        self._used = [0] * count
        self._used[0] = 1                      # init.c:583 reserves WCID 0 for multicast

    def alloc(self) -> Optional[int]:
        """The lowest free slot, or None once WCID_ALLOC_LIMIT is reached (main.c:203).

        main.c:192 searches every word and takes the first ffs(~mask), which is the lowest
        clear bit; this walks slots in order and stops at the same limit.
        """
        for idx in range(1, WCID_ALLOC_LIMIT):
            if not self._used[idx]:
                self._used[idx] = 1
                return idx
        return None

    def free(self, idx: int) -> None:
        """main.c:250 -- clear the slot's bit. The registers are cleared by wcid_setup(None)."""
        if 1 <= idx < len(self._used):
            self._used[idx] = 0

    def is_used(self, idx: int) -> bool:
        """main.c:62 -- whether the slot is currently allocated."""
        return bool(self._used[idx]) if 0 <= idx < len(self._used) else False


def wcid_setup(transport: MT7601UTransport, idx: int, vif_idx: int,
               mac: Optional[bytes]) -> None:
    """mac.c:355 mt7601u_mac_wcid_setup -- write the slot's BSS index, then its address.

    ``mac=None`` writes the all-zero address, which is how sta_remove releases a slot
    (main.c:251) -- it does not restore the 0xFFFFFFFF marker from init.c:182.
    """
    # mac.c:359-360 uses FIELD_PREP, so the index is shifted into the field, not masked
    # against it -- MT_WCID_ATTR_BSS_IDX is GENMASK(6, 4), and ANDing the index straight
    # onto it drops the value entirely.
    attr = ((vif_idx & 7) << MT_WCID_ATTR_BSS_IDX_SHIFT) | (
        MT_WCID_ATTR_BSS_IDX_EXT if vif_idx & 8 else 0)
    transport.wr(MT_WCID_ATTR(idx), attr)
    transport.addr_wr(MT_WCID_ADDR(idx), bytes(mac) if mac else b"\x00" * 6)


AMPDU_FACTOR_NO_STATIONS = 3
"""mac.c:375 -- `u8 min_factor = 3` is the value when no station has a smaller one."""

MAX_LEN_CFG_STATION_BASE = 0xA0FFF
"""mac.c:392 -- the literal the C ORs the AMPDU factor into, and not what the MAC
initvals table leaves in MT_MAX_LEN_CFG."""


def set_ampdu_factor(transport: MT7601UTransport,
                     station_factors: Iterable[int] = ()) -> None:
    """mac.c:371 mt7601u_mac_set_ampdu_factor -- the smallest factor across live stations.

    The C reads each station's ht_cap.ampdu_factor; nothing here has an association
    surface to carry one, so an empty iterable gives the kernel's no-station value.
    """
    min_factor = min([AMPDU_FACTOR_NO_STATIONS, *station_factors])
    transport.wr(MT_MAX_LEN_CFG,
                 MAX_LEN_CFG_STATION_BASE | _field_prep(MT_MAX_LEN_CFG_AMPDU, min_factor))


def sta_add(transport: MT7601UTransport, allocator: WcidAllocator, vif_idx: int,
            mac: bytes, station_factors: Iterable[int] = ()) -> Optional[int]:
    """main.c:220-231 mt7601u_sta_add -- claim a slot and let its traffic through.

    MT_WCID_DROP is the gate that matters: main.c:229 is the only place in the whole
    driver that clears it, so a slot whose bit the chip powered up set would have its
    traffic discarded by the MAC with nothing to show why.
    """
    idx = allocator.alloc()
    if idx is None:
        return None
    wcid_setup(transport, idx, vif_idx, mac)
    transport.rmw(MT_WCID_DROP(idx), MT_WCID_DROP_MASK(idx), 0)         # main.c:229
    set_ampdu_factor(transport, station_factors)
    return idx


def sta_remove(transport: MT7601UTransport, allocator: WcidAllocator, idx: int,
               station_factors: Iterable[int] = ()) -> None:
    """main.c:240-252 mt7601u_sta_remove -- drop the slot's traffic, then release it.

    main.c:251 passes vif_idx 0, so the attr word is zeroed outright rather than left
    naming the BSS the station used to belong to.
    """
    transport.rmw(MT_WCID_DROP(idx),
                  MT_WCID_DROP_MASK(idx), MT_WCID_DROP_MASK(idx))       # main.c:249
    allocator.free(idx)
    wcid_setup(transport, idx, 0, None)                                 # main.c:251
    set_ampdu_factor(transport, station_factors)
