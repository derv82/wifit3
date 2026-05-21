"""MT76x0U EFUSE (on-die "EEPROM") reads (M2).

Ports `mt76x02_efuse_read` + `mt76x02_get_efuse_data` + helpers from
`data_dumps/mt76-source-v6.18/mt76x02_eeprom.c` and `mt76x0/eeprom.c`.

EFUSE is the chip's on-die one-time-programmable memory holding per-card
calibration data: MAC address, RF frequency offset, TX path / RX path
config, regulatory region, etc. Reads happen via the dedicated EFUSE_CTRL
register protocol — NOT MT_VEND_READ_EEPROM (that bRequest is for an
external EEPROM, which our chips don't have).

EFUSE read protocol (per `mt76x02_efuse_read`):
  1. Read MT_EFUSE_CTRL (current value, so we preserve unrelated bits).
  2. Clear AIN + MODE fields; set AIN = addr & ~0xf (16-byte aligned),
     MODE = (MT_EE_READ | MT_EE_PHYSICAL_READ), KICK = 1.
  3. Write MT_EFUSE_CTRL.
  4. Poll MT_EFUSE_CTRL until KICK clears (success), up to 1000 ms.
  5. udelay(2).
  6. Read MT_EFUSE_DATA(0..3) — 16 bytes of EFUSE content at that block.
  7. If AOUT field of MT_EFUSE_CTRL is all 1s after the read, the block
     is unburned — return 16 bytes of 0xff.

Per [[feedback_chipset_methodology]] the kernel "logical" EFUSE field
offsets (MT_EE_MAC_ADDR=0x004, etc.) map directly into the read buffer
because each block returns 16 contiguous bytes — no separate cache layer
needed for the small set of fields M2 touches.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from .constants import (
    BOARD_TYPE_2GHZ,
    BOARD_TYPE_5GHZ,
    MT_EE_FREQ_OFFSET,
    MT_EE_MAC_ADDR,
    MT_EE_NIC_CONF_0,
    MT_EE_NIC_CONF_0_BOARD_TYPE_MASK,
    MT_EE_NIC_CONF_0_BOARD_TYPE_SHIFT,
    MT_EE_NIC_CONF_0_RX_PATH_MASK,
    MT_EE_NIC_CONF_0_TX_PATH_MASK,
    MT_EE_NIC_CONF_0_TX_PATH_SHIFT,
    MT_EE_NIC_CONF_1,
    MT_EE_PHYSICAL_READ,
    MT_EE_READ,
    MT_EFUSE_CTRL,
    MT_EFUSE_CTRL_AIN_MASK,
    MT_EFUSE_CTRL_AIN_SHIFT,
    MT_EFUSE_CTRL_AOUT_MASK,
    MT_EFUSE_CTRL_KICK,
    MT_EFUSE_CTRL_MODE_MASK,
    MT_EFUSE_CTRL_MODE_SHIFT,
    MT_EFUSE_DATA_BASE,
)
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)

EFUSE_BLOCK_SIZE = 16
EFUSE_KICK_POLL_TIMEOUT_MS = 1000


class EEPROMError(RuntimeError):
    """EFUSE read failed (KICK didn't clear, length issues, etc.)."""


def efuse_read_block(
    transport: MT76x0UTransport, addr: int, mode: int = MT_EE_PHYSICAL_READ,
) -> bytes:
    """Read a single 16-byte EFUSE block starting at `addr & ~0xf`.

    Mirrors `mt76x02_efuse_read`. Returns exactly 16 bytes. If the block
    is unburned (AOUT all-set after read), returns b"\\xff" * 16.
    """
    block_addr = addr & ~0xF
    val = transport.read32(MT_EFUSE_CTRL)
    val &= ~(MT_EFUSE_CTRL_AIN_MASK | MT_EFUSE_CTRL_MODE_MASK)
    val |= (block_addr << MT_EFUSE_CTRL_AIN_SHIFT) & MT_EFUSE_CTRL_AIN_MASK
    val |= (mode << MT_EFUSE_CTRL_MODE_SHIFT) & MT_EFUSE_CTRL_MODE_MASK
    val |= MT_EFUSE_CTRL_KICK
    transport.write32(MT_EFUSE_CTRL, val)

    # Poll for KICK to clear.
    deadline = time.monotonic() + EFUSE_KICK_POLL_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        cur = transport.read32(MT_EFUSE_CTRL)
        if not (cur & MT_EFUSE_CTRL_KICK):
            break
        time.sleep(0.001)
    else:
        raise EEPROMError(
            f"EFUSE KICK didn't clear within {EFUSE_KICK_POLL_TIMEOUT_MS}ms "
            f"(addr=0x{block_addr:04x})"
        )

    # udelay(2) per kernel.
    time.sleep(0.000002)

    final_ctrl = transport.read32(MT_EFUSE_CTRL)
    if (final_ctrl & MT_EFUSE_CTRL_AOUT_MASK) == MT_EFUSE_CTRL_AOUT_MASK:
        logger.debug("EFUSE block 0x%03x: unburned (returning 0xff×16)", block_addr)
        return b"\xff" * EFUSE_BLOCK_SIZE

    block = bytearray(EFUSE_BLOCK_SIZE)
    for i in range(4):
        word = transport.read32(MT_EFUSE_DATA_BASE + 4 * i)
        block[4 * i: 4 * i + 4] = word.to_bytes(4, "little")
    return bytes(block)


def efuse_get_data(
    transport: MT76x0UTransport, base: int, length: int,
    mode: int = MT_EE_PHYSICAL_READ,
) -> bytes:
    """Read a range of EFUSE bytes starting at `base`. Length must be a
    multiple of 16 (kernel rejects non-aligned reads with `i + 16 <= len`).

    Mirrors `mt76x02_get_efuse_data`.
    """
    if length <= 0 or length % EFUSE_BLOCK_SIZE != 0:
        raise EEPROMError(
            f"efuse_get_data: length {length} must be positive multiple of "
            f"{EFUSE_BLOCK_SIZE}"
        )
    buf = bytearray(length)
    for i in range(0, length, EFUSE_BLOCK_SIZE):
        block = efuse_read_block(transport, base + i, mode=mode)
        buf[i: i + EFUSE_BLOCK_SIZE] = block
    return bytes(buf)


@dataclass
class EFUSEInfo:
    """Decoded EFUSE summary — what wifit3 actually consumes from EFUSE."""

    mac_address: str            # "xx:xx:xx:xx:xx:xx"
    mac_bytes: bytes            # 6 raw bytes
    nic_conf_0: int             # raw u16
    nic_conf_1: int             # raw u16
    freq_offset: Optional[int]  # signed offset, or None if unburned
    rx_path: int                # 1 (single-stream is the only MT7610U value)
    tx_path: int                # 1
    has_2ghz: bool
    has_5ghz: bool
    raw_first_64: bytes         # for diagnostic dump


def read_efuse_summary(transport: MT76x0UTransport) -> EFUSEInfo:
    """Read the small block of EFUSE wifit3 actually needs and decode it.

    Reads 0x000..0x040 (64 bytes = 4 blocks) which covers everything from
    CHIP_ID through NIC_CONF_2.
    """
    raw = efuse_get_data(transport, 0x000, 64, mode=MT_EE_READ)
    mac = bytes(raw[MT_EE_MAC_ADDR: MT_EE_MAC_ADDR + 6])
    mac_str = ":".join(f"{b:02x}" for b in mac)

    nic_conf_0 = int.from_bytes(raw[MT_EE_NIC_CONF_0: MT_EE_NIC_CONF_0 + 2], "little")
    nic_conf_1 = int.from_bytes(raw[MT_EE_NIC_CONF_1: MT_EE_NIC_CONF_1 + 2], "little")
    freq_offset_byte = raw[MT_EE_FREQ_OFFSET]
    # Kernel: `if (!mt76x02_field_valid(val)) val = 0;` where field_valid = (val != 0xff).
    if freq_offset_byte == 0xFF:
        freq_offset: Optional[int] = None
    else:
        # Kernel sign-extends 8-bit to int.
        freq_offset = freq_offset_byte if freq_offset_byte < 0x80 else freq_offset_byte - 0x100

    rx_path = nic_conf_0 & MT_EE_NIC_CONF_0_RX_PATH_MASK
    tx_path = (nic_conf_0 & MT_EE_NIC_CONF_0_TX_PATH_MASK) >> MT_EE_NIC_CONF_0_TX_PATH_SHIFT
    board_type = (nic_conf_0 & MT_EE_NIC_CONF_0_BOARD_TYPE_MASK) >> MT_EE_NIC_CONF_0_BOARD_TYPE_SHIFT

    # Per `mt76x02_eeprom_parse_hw_cap` — 5G-only vs 2G-only vs dual.
    if board_type == BOARD_TYPE_5GHZ:
        has_2ghz, has_5ghz = False, True
    elif board_type == BOARD_TYPE_2GHZ:
        has_2ghz, has_5ghz = True, False
    else:
        # 0 (BBP) / 3 (unburned) → default dual-band per kernel fall-through.
        has_2ghz, has_5ghz = True, True

    return EFUSEInfo(
        mac_address=mac_str,
        mac_bytes=mac,
        nic_conf_0=nic_conf_0,
        nic_conf_1=nic_conf_1,
        freq_offset=freq_offset,
        rx_path=rx_path,
        tx_path=tx_path,
        has_2ghz=has_2ghz,
        has_5ghz=has_5ghz,
        raw_first_64=raw,
    )
