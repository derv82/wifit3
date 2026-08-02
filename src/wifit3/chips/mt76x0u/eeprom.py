"""MT76x0U EFUSE (on-die "EEPROM") reads (M2).

Ports `mt76x02_efuse_read` + `mt76x02_get_efuse_data` + helpers from
`driver_sources/mt76-source-v6.18/mt76x02_eeprom.c` and `mt76x0/eeprom.c`.

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
    MT76X0_EEPROM_SIZE,
    MT76X0U_EE_MAX_VER,
    MT_EE_2G_TARGET_POWER,
    MT_EE_CHIP_ID,
    MT_EE_FREQ_OFFSET,
    MT_EE_LNA_GAIN,
    MT_EE_RSSI_OFFSET_2G_1,
    MT_EE_RSSI_OFFSET_5G_1,
    MT_EE_MAC_ADDR,
    MT_EE_NIC_CONF_0,
    MT_EE_NIC_CONF_0_BOARD_TYPE_MASK,
    MT_EE_NIC_CONF_0_BOARD_TYPE_SHIFT,
    MT_EE_NIC_CONF_0_RX_PATH_MASK,
    MT_EE_NIC_CONF_0_TX_PATH_MASK,
    MT_EE_NIC_CONF_0_TX_PATH_SHIFT,
    MT_EE_NIC_CONF_1,
    MT_EE_PCI_ID,
    MT_EE_PHYSICAL_READ,
    MT_EE_READ,
    MT_EE_TSSI_BOUND4,
    MT_EE_VERSION,
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


class EEPROMCache:
    """Full 512-byte EFUSE buffer + accessor mirroring `mt76x02_eeprom_get`.

    [SRC] mt76x0/eeprom.h:16 — MT76X0_EEPROM_SIZE = 512.
    [SRC] mt76x02_eeprom.h `mt76x02_eeprom_get` returns u16 at offset.
    """

    def __init__(self, data: bytes):
        if len(data) != MT76X0_EEPROM_SIZE:
            raise EEPROMError(
                f"EEPROMCache: expected {MT76X0_EEPROM_SIZE} bytes, got {len(data)}"
            )
        self.data = bytes(data)

    def get_u8(self, offset: int) -> int:
        return self.data[offset]

    def get_u16(self, offset: int) -> int:
        """Equivalent of `mt76x02_eeprom_get(dev, field)`. Returns LE u16."""
        return int.from_bytes(self.data[offset: offset + 2], "little")

    def get_bytes(self, offset: int, length: int) -> bytes:
        return bytes(self.data[offset: offset + length])


def load_full_eeprom(transport: MT76x0UTransport) -> EEPROMCache:
    """`mt76x0_load_eeprom` — load 512 bytes from EFUSE into a cache.

    [SRC] mt76x0/eeprom.c:293-310. The kernel first tries `mt76_eeprom_init`
    (loads from a host-side stored file or DT EEPROM partition); on USB we
    don't have either of those, so we always fall through to
    `mt76x0_efuse_physical_size_check` + `mt76x02_get_efuse_data`. The size-
    check uses MT_EE_PHYSICAL_READ; the actual cache load uses MT_EE_READ.

    Returns an EEPROMCache wrapping the 512-byte buffer.
    """
    # Kernel does a physical-size check first (mt76x0_efuse_physical_size_check),
    # which scans the usage-map region for free slots. For our purposes we
    # accept any EFUSE that read returns and let check_eeprom() validate the
    # chip_id field instead — that's the meaningful sanity check.
    raw = efuse_get_data(transport, 0, MT76X0_EEPROM_SIZE, mode=MT_EE_READ)
    return EEPROMCache(raw)


def check_eeprom(cache: EEPROMCache) -> int:
    """`mt76x0_check_eeprom` — verify the EEPROM chip-id field.

    [SRC] mt76x0/eeprom.c:273-291. Reads u16 at offset 0 (CHIP_ID); if zero,
    falls back to offset MT_EE_PCI_ID. Must be 0x7610 or 0x7650 for mt76x0u.

    Returns the validated chip_id. Raises EEPROMError on mismatch.
    """
    chip_id = cache.get_u16(MT_EE_CHIP_ID)
    if chip_id == 0:
        chip_id = cache.get_u16(MT_EE_PCI_ID)
    if chip_id not in (0x7610, 0x7650):
        raise EEPROMError(
            f"EEPROM chip_id 0x{chip_id:04x} not in (0x7610, 0x7650)"
        )
    return chip_id


def _field_valid_u8(val: int) -> bool:
    """`mt76x02_field_valid` — kernel macro `val != 0xff`. [SRC] mt76x02.h."""
    return val != 0xFF


def _sign_extend(val: int, width_bits: int) -> int:
    """`mt76x02_sign_extend(val, width)` — sign-extend an N-bit value."""
    sign_bit = 1 << (width_bits - 1)
    mask = (1 << width_bits) - 1
    val &= mask
    if val & sign_bit:
        val -= (1 << width_bits)
    return val


def decode_chip_cap(
    cache: EEPROMCache, is_mt7630: bool = False, no_2ghz: bool = False,
) -> dict:
    """Port of `mt76x0_set_chip_cap` + `mt76x02_eeprom_parse_hw_cap`.

    [SRC] mt76x0/eeprom.c:48-80 + mt76x02_eeprom.c:72-89.

    Returns a dict with has_2ghz, has_5ghz, tx_path, rx_path,
    nic_conf_0/1, plus a list of warnings the kernel would log.

    ``is_mt7630`` / ``no_2ghz`` are the two runtime discriminators that mask a
    band off the board-type default; both default to the captured reference
    (0x7650, no quirk) so its decode is unchanged:
      - ``no_2ghz`` (Archer T1U USB `driver_info=1`) masks 2 GHz. [SRC] eeprom.c:57-60
      - ``is_mt7630`` (ASIC ver >> 16 == 0x7630, the WiFi-2.4G+BT combo strap)
        masks 5 GHz. [SRC] eeprom.c:62-65
    """
    nic0 = cache.get_u16(MT_EE_NIC_CONF_0)
    nic1 = cache.get_u16(MT_EE_NIC_CONF_1)

    board = (nic0 & 0x3000) >> 12       # MT_EE_NIC_CONF_0_BOARD_TYPE
    if board == BOARD_TYPE_5GHZ:
        has_2g, has_5g = False, True
    elif board == BOARD_TYPE_2GHZ:
        has_2g, has_5g = True, False
    else:
        has_2g, has_5g = True, True     # default (kernel fall-through)

    if no_2ghz:
        has_2g = False
    if is_mt7630:
        has_5g = False

    rx_path = nic0 & 0x000F
    tx_path = (nic0 & 0x00F0) >> 4

    warnings = []
    # Kernel: `if (!mt76x02_field_valid(nic_conf1 & 0xff)) nic_conf1 &= 0xff00;`
    if (nic1 & 0xFF) == 0xFF:
        nic1 = nic1 & 0xFF00
    # Kernel: `if (nic_conf1 & MT_EE_NIC_CONF_1_HW_RF_CTRL) warn`
    if nic1 & (1 << 0):
        warnings.append("HW_RF_CTRL set in NIC_CONF_1 — kernel doesn't support that")
    # Kernel: validate tx/rx path each <= 1.
    if rx_path > 1 or tx_path > 1:
        warnings.append(f"invalid tx-rx stream (tx={tx_path}, rx={rx_path})")

    return {
        "has_2ghz": has_2g, "has_5ghz": has_5g,
        "tx_path": tx_path, "rx_path": rx_path,
        "nic_conf_0": nic0, "nic_conf_1": nic1,
        "board_type": board, "warnings": warnings,
    }


def decode_temp_offset(cache: EEPROMCache) -> int:
    """`mt76x0_set_temp_offset` — [SRC] mt76x0/eeprom.c:82-91.

    Reads the high byte of MT_EE_2G_TARGET_POWER as a signed 8-bit value.
    Default to -10 if the byte is 0xFF (unburned).
    """
    val = cache.get_u16(MT_EE_2G_TARGET_POWER) >> 8
    if _field_valid_u8(val):
        return _sign_extend(val, 8)
    return -10


def decode_freq_offset(cache: EEPROMCache) -> int:
    """`mt76x0_set_freq_offset` — [SRC] mt76x0/eeprom.c:93-108.

    Returns the unsigned u8 at MT_EE_FREQ_OFFSET (0 if unburned), minus a
    sign-extended compensation from MT_EE_TSSI_BOUND4 high byte.
    """
    val = cache.get_u8(MT_EE_FREQ_OFFSET)
    if not _field_valid_u8(val):
        val = 0
    freq_offset = val

    comp = cache.get_u16(MT_EE_TSSI_BOUND4) >> 8
    if not _field_valid_u8(comp):
        comp = 0
    freq_offset -= _sign_extend(comp, 8)
    return freq_offset


def lna_gain_for_channel(cache: EEPROMCache, channel: int) -> int:
    """Per-channel RX LNA gain (signed dB) — the value the AGC gain register is
    corrected by (`AGC,8 gain -= lna_gain*2`) for sensitivity on this channel.

    Ports `mt76x02_get_rx_gain` (LNA extraction) + `mt76x02_get_lna_gain` (band /
    5 GHz-subband select) [SRC] mt76x02_eeprom.c:102-147, as driven per-tune by
    `mt76x0_read_rx_gain` [SRC] mt76x0/eeprom.c:110. Only the LNA-gain half is
    ported; the `rssi_offset[]` half is RSSI-display-only and unused here.

    Subband map by channel number (`chan->hw_value`): 2.4 GHz → lna_2g; 5 GHz
    ch≤64 → lna_5g[0], ch≤128 → lna_5g[1], else → lna_5g[2]. An invalid 5 GHz
    subband entry (`mt76x02_field_valid` = ``!= 0 and != 0xff``) falls back to
    lna_5g[0]; a 0xff selection yields 0. The result is sign-extended because the
    kernel stores it in an ``s8`` (`mt76x02_rx_freq_cal.lna_gain`).
    """
    lna_word = cache.get_u16(MT_EE_LNA_GAIN)
    lna_2g = lna_word & 0xFF
    lna_5g = [
        lna_word >> 8,
        cache.get_u16(MT_EE_RSSI_OFFSET_2G_1) >> 8,
        cache.get_u16(MT_EE_RSSI_OFFSET_5G_1) >> 8,
    ]
    # mt76x02_field_valid(u8) = (val != 0 && val != 0xff) — NB stricter than the
    # local _field_valid_u8 (which only excludes 0xff); 0 must also fall back.
    for i in (1, 2):
        if lna_5g[i] in (0, 0xFF):
            lna_5g[i] = lna_5g[0]

    if channel <= 14:
        lna = lna_2g
    elif channel <= 64:
        lna = lna_5g[0]
    elif channel <= 128:
        lna = lna_5g[1]
    else:
        lna = lna_5g[2]

    return 0 if lna == 0xFF else _sign_extend(lna, 8)


@dataclass
class EFUSEFullInfo:
    """Decoded full EFUSE — what wifit3 actually consumes."""

    chip_id: int                # 0x7610 or 0x7650
    version: int                # MT_EE_VERSION high byte
    fae: int                    # MT_EE_VERSION low byte
    mac_address: str            # "xx:xx:xx:xx:xx:xx"
    mac_bytes: bytes
    has_2ghz: bool
    has_5ghz: bool
    tx_path: int
    rx_path: int
    nic_conf_0: int
    nic_conf_1: int
    temp_offset: int
    freq_offset: int
    cap_warnings: list
    cache: EEPROMCache          # raw cache for future per-channel TX power lookups


def read_efuse_full(
    transport: MT76x0UTransport, is_mt7630: bool = False, no_2ghz: bool = False,
) -> EFUSEFullInfo:
    """Port of `mt76x0_eeprom_init` (mt76x0/eeprom.c:312-353).

    Loads the full 512-byte EFUSE into a cache, validates chip_id, reads
    version/fae, extracts MAC, decodes chip_cap + temp_offset + freq_offset.
    Returns an EFUSEFullInfo populated with all decoded fields.

    ``is_mt7630`` / ``no_2ghz`` gate the band-capability masks in
    ``decode_chip_cap`` and default to the captured reference (no mask), so its
    decode — and therefore every downstream wire write keyed on has_2ghz/
    has_5ghz (phy_ant_select) — is byte-identical.

    Does NOT write MAC to chip registers — caller is responsible for calling
    `mt76x02_mac_setaddr` separately (the kernel does this in eeprom_init but
    we keep MAC-write side-effects in mac.py).
    """
    cache = load_full_eeprom(transport)
    chip_id = check_eeprom(cache)

    ver_word = cache.get_u16(MT_EE_VERSION)
    version = (ver_word >> 8) & 0xFF
    fae = ver_word & 0xFF
    if version > MT76X0U_EE_MAX_VER:
        logger.warning("EEPROM version 0x%02x > MT76X0U_EE_MAX_VER=0x%02x — "
                       "may be unsupported", version, MT76X0U_EE_MAX_VER)
    logger.info("EEPROM ver=0x%02x fae=0x%02x chip_id=0x%04x",
                version, fae, chip_id)

    mac_bytes = cache.get_bytes(MT_EE_MAC_ADDR, 6)
    mac_str = ":".join(f"{b:02x}" for b in mac_bytes)

    cap = decode_chip_cap(cache, is_mt7630=is_mt7630, no_2ghz=no_2ghz)
    for w in cap["warnings"]:
        logger.warning("EEPROM cap: %s", w)

    return EFUSEFullInfo(
        chip_id=chip_id,
        version=version,
        fae=fae,
        mac_address=mac_str,
        mac_bytes=mac_bytes,
        has_2ghz=cap["has_2ghz"],
        has_5ghz=cap["has_5ghz"],
        tx_path=cap["tx_path"],
        rx_path=cap["rx_path"],
        nic_conf_0=cap["nic_conf_0"],
        nic_conf_1=cap["nic_conf_1"],
        temp_offset=decode_temp_offset(cache),
        freq_offset=decode_freq_offset(cache),
        cap_warnings=cap["warnings"],
        cache=cache,
    )


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
