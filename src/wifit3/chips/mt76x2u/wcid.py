"""MT76x2U WCID (Wireless Client ID) table maintenance.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

The chip exposes 256 per-station WCID slots, each with:
  - ATTR (4 bytes at MT_WCID_ATTR(idx) = 0xa800 + idx*4) — BSS index + flags
  - ADDR (8 bytes at MT_WCID_ADDR(idx) = 0x1800 + idx*8) — 6-byte MAC + 2-byte
    BA mask. Only slots 0-127 have ADDR storage; slots 128-255 have ATTR only.

The chip's TX engine looks up the WCID from `TXWI.wcid` to fetch per-station
rate / key index / cipher mode / ACK matching. **For inject TX (wcid=0xFF in
TXWI) the chip still consults entry 255** — if stale data sits there from a
previous boot or warm reattach, TX silently misbehaves even though MGMT
frames work fine.

Kernel clears all entries at init (`mt76x2/usb_init.c:165-167`), so wifit3
must do the same.

[SRC] mt76x02_mac.c:148-167 (mt76x02_mac_wcid_setup)
[SRC] mt76x02_regs.h:643-688 (register/struct definitions)
"""
from __future__ import annotations

import logging
from typing import Optional

from .constants import (
    MT76_N_WCIDS,
    MT76_WCID_ADDR_SLOTS,
    MT_WCID_ADDR_BASE,
    MT_WCID_ATTR_BASE,
    MT_WCID_ATTR_BSS_IDX_EXT,
    MT_WCID_ATTR_BSS_IDX_MASK,
    MT_WCID_ATTR_BSS_IDX_SHIFT,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)


def _wcid_attr_addr(idx: int) -> int:
    return MT_WCID_ATTR_BASE + idx * 4


def _wcid_addr_addr(idx: int) -> int:
    return MT_WCID_ADDR_BASE + idx * 8


def mt76x02_mac_wcid_setup(
    transport: MT76x2UTransport,
    idx: int,
    vif_idx: int = 0,
    mac: Optional[bytes] = None,
) -> None:
    """`mt76x02_mac_wcid_setup` — [SRC] mt76x02_mac.c:148-167.

    Programs one WCID slot. Always writes ATTR (256 slots available);
    only writes ADDR for idx < 128 (kernel early-returns at idx >= 128).

    ``vif_idx`` packs into ATTR as:
      - BSS_IDX  = vif_idx & 7         → bits 6:4
      - BSS_IDX_EXT = !!(vif_idx & 8)  → bit 11

    ``mac`` is the 6-byte MAC address; ``None`` writes a zeroed
    ``struct mt76_wcid_addr`` (6 bytes MAC + 2 bytes ba_mask = 8 bytes).
    """
    attr = (
        ((vif_idx & 0x7) << MT_WCID_ATTR_BSS_IDX_SHIFT)
        & MT_WCID_ATTR_BSS_IDX_MASK
    )
    if vif_idx & 0x8:
        attr |= MT_WCID_ATTR_BSS_IDX_EXT
    transport.write32(_wcid_attr_addr(idx), attr)

    if idx >= MT76_WCID_ADDR_SLOTS:
        return

    # `struct mt76_wcid_addr` = { u8 macaddr[6]; __le16 ba_mask; } = 8 bytes,
    # written as ONE mt76_wr_copy (a single 8-byte MULTI_WRITE), not two write32.
    # [SRC] mt76x02_mac.c mt76x02_mac_wcid_setup + usb.c mt76u_copy.
    if mac is None:
        addr_buf = b"\x00" * 8
    elif len(mac) != 6:
        raise ValueError(f"wcid_setup: mac must be 6 bytes, got {len(mac)}")
    else:
        addr_buf = bytes(mac[:6]) + b"\x00\x00"    # MAC + ba_mask=0
    transport.write_copy(_wcid_addr_addr(idx), addr_buf)


def wcid_table_clear(transport: MT76x2UTransport) -> None:
    """Clear all 256 WCID slots — kernel `usb_init.c:165-167`.

    For each slot: ATTR=0, plus ADDR=zeroed for slots 0-127. Total writes:
    256 (ATTR) + 128*2 (ADDR low + high) = 512 register transfers. Ran once
    per cold boot; takes ~200-300 ms on USB.
    """
    for i in range(MT76_N_WCIDS):
        mt76x02_mac_wcid_setup(transport, i, vif_idx=0, mac=None)
    logger.info("MT7612U: WCID table cleared (%d slots)", MT76_N_WCIDS)
