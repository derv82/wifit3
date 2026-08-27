"""MT76x2U shared-key table maintenance.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

The chip exposes 16 vifs × 4 keys = 64 shared-key slots, each 32 bytes
(key + TX MIC + RX MIC). Each vif has 4 cipher-type fields packed into a
shared MT_SKEY_MODE register (4 bits per cipher × 4 keys × 2 vifs sharing
one register = 32 bits).

The chip's TX encryption engine looks up the cipher mode + key when
encrypting a frame; for inject TX (wcid=0xFF), the chip *may* still
consult these tables. Kernel clears all entries at init
(`mt76x2/usb_init.c:169-173`).

[SRC] mt76x02_mac.c:58-79 (mt76x02_mac_shared_key_setup)
[SRC] mt76x02_regs.h:666-679 (register/macro definitions)
"""
from __future__ import annotations

import logging
import struct

from .constants import (
    MT76_N_KEYS_PER_VIF,
    MT76_N_VIFS,
    MT76_SKEY_ENTRY_BYTES,
    MT76X02_CIPHER_NONE,
    MT_SKEY_BASE_0,
    MT_SKEY_BASE_1,
    MT_SKEY_MODE_BASE_0,
    MT_SKEY_MODE_BASE_1,
    MT_SKEY_MODE_MASK,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)


def _skey_addr(vif_idx: int, key_idx: int) -> int:
    """`MT_SKEY(_bss, _idx)` — [SRC] mt76x02_regs.h:670."""
    bss = vif_idx & 0xF
    idx = key_idx & 0x3
    if bss & 8:
        return MT_SKEY_BASE_1 + (4 * (bss & 7) + idx) * MT76_SKEY_ENTRY_BYTES
    return MT_SKEY_BASE_0 + (4 * bss + idx) * MT76_SKEY_ENTRY_BYTES


def _skey_mode_addr(vif_idx: int) -> int:
    """`MT_SKEY_MODE(_bss)` — [SRC] mt76x02_regs.h:676. Pairs of vifs share
    one mode register (vif 0+1 share, 2+3 share, etc.)."""
    bss = vif_idx & 0xF
    if bss & 8:
        return MT_SKEY_MODE_BASE_1 + (((bss & 7) // 2) << 2)
    return MT_SKEY_MODE_BASE_0 + ((bss // 2) << 2)


def _skey_mode_shift(vif_idx: int, key_idx: int) -> int:
    """`MT_SKEY_MODE_SHIFT(_bss, _idx)` — [SRC] mt76x02_regs.h:678. Even
    vif occupies bits 0-15, odd vif occupies bits 16-31; within each half,
    each key occupies 4 bits (key 0 lowest)."""
    return 4 * (key_idx + 4 * (vif_idx & 1))


def mt76x02_mac_shared_key_setup(
    transport: MT76x2UTransport,
    vif_idx: int,
    key_idx: int,
    key: bytes | None = None,
) -> None:
    """`mt76x02_mac_shared_key_setup` — [SRC] mt76x02_mac.c:58-79.

    Programs one shared key slot for ``(vif_idx, key_idx)``. ``key=None``
    sets ``cipher=MT76X02_CIPHER_NONE`` and writes 32 bytes of zero (the
    cleared state used by wifit3's cold-boot init loop). Real key
    installation (TKIP / CCMP) isn't ported — wifit3 doesn't push keys to
    the chip; we do crypto in software for the WEP suite.
    """
    if key is not None:
        raise NotImplementedError(
            "shared_key_setup: keyed install not ported; wifit3 only uses "
            "NULL clears at init (it does WEP crypto in software)."
        )

    cipher = MT76X02_CIPHER_NONE
    # Read-modify-write the cipher-mode bits for this (vif, key_idx).
    mode_addr = _skey_mode_addr(vif_idx)
    shift = _skey_mode_shift(vif_idx, key_idx)
    mask = MT_SKEY_MODE_MASK << shift
    val = transport.read32(mode_addr)
    val = (val & ~mask) | ((cipher << shift) & mask)
    transport.write32(mode_addr, val)

    # 32 bytes (= 8 u32 words) of zeroed key+MICs.
    key_data = bytes(MT76_SKEY_ENTRY_BYTES)
    skey_addr = _skey_addr(vif_idx, key_idx)
    for word_idx in range(MT76_SKEY_ENTRY_BYTES // 4):
        word = struct.unpack("<I", key_data[word_idx * 4:word_idx * 4 + 4])[0]
        transport.write32(skey_addr + word_idx * 4, word)


def shared_key_table_clear(transport: MT76x2UTransport) -> None:
    """Clear all 16×4 = 64 shared key slots — kernel ``usb_init.c:169-173``.

    Per slot: 1 RMW on the mode register + 8 zero writes for the 32-byte
    key region. Total: 64 reads + 64 + 64*8 = 64+576 = 640 register
    transfers. Ran once per cold boot; takes ~300 ms on USB.
    """
    for vif_idx in range(MT76_N_VIFS):
        for key_idx in range(MT76_N_KEYS_PER_VIF):
            mt76x02_mac_shared_key_setup(transport, vif_idx, key_idx, key=None)
    logger.debug(
        "MT7612U: Shared key table cleared (%d vifs x %d keys)",
        MT76_N_VIFS, MT76_N_KEYS_PER_VIF,
    )
