"""RTL8188FTV EFUSE read + per-chip parse.

Cleanroom port of:

* `rtl8xxxu_read_efuse8`   -- `core.c:1746-1778` (single-byte EFUSE read)
* `rtl8xxxu_read_efuse`    -- `core.c:1780-1890` (full EFUSE map walker)
* `rtl8188fu_parse_efuse`  -- `8188f.c:704-737` (8188f-specific struct decode)

EFUSE is the chip's one-time-programmable factory memory -- it holds the
dongle's MAC, per-group TX power calibration and RF / regulatory params.
Reading it is a polled register protocol on REG_EFUSE_CTRL (0x0030):

    write address bytes  ->  trigger read (clear bit 7 of CTRL+3)  ->
    poll bit 31 of CTRL  ->  read 8-bit result from CTRL[7:0]

The map is variable-length-encoded: 8-bit headers carry an offset + a
4-bit "word valid" mask; data follows in the next bytes.  The walker
unpacks all valid words into a 512-byte raw buffer; per-chip parsers
then pick fixed offsets out of that.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .constants import (
    EEPROM_BOOT,
    EEPROM_ENABLE,
    EFUSE_ACCESS_DISABLE,
    EFUSE_ACCESS_ENABLE,
    EFUSE_MAP_LEN,
    EFUSE_MAX_WORD_UNIT,
    EFUSE_REAL_CONTENT_LEN_8723A,
    EFUSE_RTL_ID,
    REG_9346CR,
    REG_EFUSE_ACCESS,
    REG_EFUSE_CTRL,
    REG_SYS_CLKR,
    REG_SYS_FUNC,
    REG_SYS_ISO_CTRL,
    RTL8XXXU_MAX_REG_POLL,
    SYS_CLK_ANA8M,
    SYS_CLK_LOADER_ENABLE,
    SYS_FUNC_ELDR,
    SYS_ISO_PWC_EV12V,
    TX_POWER_INDEX_DEFAULT_CCK,
    TX_POWER_INDEX_DEFAULT_HT40,
    TX_POWER_INDEX_MAX,
)
from .transport import RTL8188FTVTransport

logger = logging.getLogger(__name__)


# ---- low-level byte read --------------------------------------------


def read_efuse_byte(t: RTL8188FTVTransport, offset: int) -> int:
    """Port of `rtl8xxxu_read_efuse8` (core.c:1746-1778).

    Reads one byte from EFUSE at the given offset.  Raises IOError on
    poll timeout.  Mirrors the kernel's three CTRL reads per byte: one
    probe read, the poll loop (which breaks as soon as bit 31 is set)
    and the final post-udelay read.
    """
    t.write8(REG_EFUSE_CTRL + 1, offset & 0xFF)
    val8 = t.read8(REG_EFUSE_CTRL + 2)
    val8 = (val8 & 0xFC) | ((offset >> 8) & 0x03)
    t.write8(REG_EFUSE_CTRL + 2, val8)

    val8 = t.read8(REG_EFUSE_CTRL + 3)
    t.write8(REG_EFUSE_CTRL + 3, val8 & 0x7F)

    t.read32(REG_EFUSE_CTRL)
    for _ in range(RTL8XXXU_MAX_REG_POLL):
        val32 = t.read32(REG_EFUSE_CTRL)
        if val32 & (1 << 31):
            break
    else:
        raise IOError(f"EFUSE read timed out at offset 0x{offset:04x}")

    time.sleep(0.000050)  # udelay(50) per kernel
    val32 = t.read32(REG_EFUSE_CTRL)
    return val32 & 0xFF


# ---- raw-map walker -------------------------------------------------


def read_efuse_map(t: RTL8188FTVTransport) -> bytes:
    """Port of `rtl8xxxu_read_efuse` (core.c:1780-1890).

    Returns the 512-byte unpacked EFUSE map.  Bytes that weren't written
    by the variable-length encoding remain `0xFF` (the kernel pre-fills
    with that and unpacks valid words on top).

    Performs the prep sequence (1.2V power, ELDR reset, clock-loader
    enable) and the access-window dance (ACCESS_ENABLE -> walk ->
    ACCESS_DISABLE) per the kernel.
    """
    val16 = t.read16(REG_9346CR)
    if val16 & EEPROM_ENABLE:
        logger.debug("EFUSE: has_eeprom bit set")
    if val16 & EEPROM_BOOT:
        logger.debug("EFUSE: boot_eeprom bit set (booted from EEPROM)")

    t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_ENABLE)

    # Power + reset + clock prep (kernel core.c:1805-1825).  Each is a
    # conditional write -- skipped when the bit is already set, which is
    # the cold-boot case (so the capture shows a bare read).
    val16 = t.read16(REG_SYS_ISO_CTRL)
    if not (val16 & SYS_ISO_PWC_EV12V):
        t.write16(REG_SYS_ISO_CTRL, val16 | SYS_ISO_PWC_EV12V)

    val16 = t.read16(REG_SYS_FUNC)
    if not (val16 & SYS_FUNC_ELDR):
        t.write16(REG_SYS_FUNC, val16 | SYS_FUNC_ELDR)

    val16 = t.read16(REG_SYS_CLKR)
    if not (val16 & SYS_CLK_LOADER_ENABLE) or not (val16 & SYS_CLK_ANA8M):
        t.write16(REG_SYS_CLKR, val16 | SYS_CLK_LOADER_ENABLE | SYS_CLK_ANA8M)

    raw = bytearray(b"\xFF" * EFUSE_MAP_LEN)

    try:
        efuse_addr = 0
        while efuse_addr < EFUSE_REAL_CONTENT_LEN_8723A:
            header = read_efuse_byte(t, efuse_addr)
            efuse_addr += 1
            if header == 0xFF:
                break

            if (header & 0x1F) == 0x0F:
                offset = (header & 0xE0) >> 5
                extheader = read_efuse_byte(t, efuse_addr)
                efuse_addr += 1
                if (extheader & 0x0F) == 0x0F:
                    continue
                offset |= (extheader & 0xF0) >> 1
                word_mask = extheader & 0x0F
            else:
                offset = (header >> 4) & 0x0F
                word_mask = header & 0x0F

            map_addr = offset * 8
            for i in range(EFUSE_MAX_WORD_UNIT):
                if word_mask & (1 << i):
                    map_addr += 2
                    continue
                if map_addr >= EFUSE_MAP_LEN - 1:
                    raise IOError(
                        f"EFUSE map_addr out of range (0x{map_addr:04x}) -- corrupt header"
                    )
                raw[map_addr] = read_efuse_byte(t, efuse_addr)
                efuse_addr += 1
                map_addr += 1
                raw[map_addr] = read_efuse_byte(t, efuse_addr)
                efuse_addr += 1
                map_addr += 1
    finally:
        t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_DISABLE)

    return bytes(raw)


# ---- 8188fu per-chip parse ------------------------------------------


# Offsets within the 512-byte EFUSE map for the 8188f-specific fields.
# Derived from `struct rtl8188fu_efuse` (rtl8xxxu.h:1166-1208).
_EFUSE_OFFSET_CCK_PWR_INDEX_A = 0x10        # tx_power_index_A.cck_base[6]
_EFUSE_OFFSET_HT40_1S_PWR_INDEX_A = 0x16    # tx_power_index_A.ht40_base[5]
_EFUSE_OFFSET_OFDM_HT20_DIFF = 0x1B         # ht20_ofdm_1s_diff (a:ofdm b:ht20)
_EFUSE_OFFSET_MAC = 0xD7                    # mac_addr[6]
_EFUSE_OFFSET_XTAL_K = 0xB9                 # xtal_k (6-bit crystal-cap trim)
_MAX_CHANNEL_GROUPS = 6                     # RTL8XXXU_MAX_CHANNEL_GROUPS (rtl8xxxu.h:84)


@dataclass
class EfuseDefaults:
    """Parsed-from-EFUSE bring-up params for 8188ftv.

    Power-index fields fall back to the kernel's ``TX_POWER_INDEX_DEFAULT_*``
    when an EFUSE byte reads past ``TX_POWER_INDEX_MAX`` (0x3F) -- exactly
    the `8188f.c:721-731` sanitising.  A blank/unreadable EFUSE (all 0xFF)
    yields the defaults.
    """
    cck_tx_power_index_A: tuple[int, ...] = field(
        default_factory=lambda: (TX_POWER_INDEX_DEFAULT_CCK,) * _MAX_CHANNEL_GROUPS
    )
    ht40_1s_tx_power_index_A: tuple[int, ...] = field(
        default_factory=lambda: (TX_POWER_INDEX_DEFAULT_HT40,) * _MAX_CHANNEL_GROUPS
    )
    ofdm_tx_power_diff_a: int = 0
    ht20_tx_power_diff_a: int = 0
    mac_address: Optional[bytes] = None
    default_crystal_cap: int = 0       # xtal_k & 0x3f; 0 = leave HW default
    raw: Optional[bytes] = None


def _sanitize_power_index(b: int, fallback: int) -> int:
    """Kernel rule: values above 0x3F are out of the 6-bit TX AGC field,
    replace with the per-band default (8188f.c:721-731).  "Less than 0x3F"
    includes the programmer's 0x3F, which the kernel leaves alone."""
    if b > TX_POWER_INDEX_MAX:
        return fallback
    return b


def _read_signed_nibble(b: int, hi: bool) -> int:
    """Half-byte of a `struct rtl8723au_idx` (rtl8xxxu.h:964-971): the two
    4-bit fields are signed.  Little-endian packing puts `a` in the low
    nibble and `b` in the high nibble."""
    v = (b >> 4) & 0x0F if hi else b & 0x0F
    return v - 16 if v & 0x08 else v


def parse_efuse_8188fu(raw: bytes) -> EfuseDefaults:
    """Port of `rtl8188fu_parse_efuse` (8188f.c:704-737).

    Picks the bring-up-relevant fields out of the raw EFUSE map and
    returns them in an :class:`EfuseDefaults`.  The opamp rtl_id check
    (`0x8129`) is enforced: a foreign map raises ValueError.
    """
    if len(raw) < EFUSE_MAP_LEN:
        raise ValueError(f"EFUSE map too short: {len(raw)} bytes")

    rtl_id = raw[0] | (raw[1] << 8)
    # TODO: verify, untested here, needs an 8188f map with a foreign rtl_id
    # (8188f.c:707-710 returns -EINVAL on rtl_id mismatch)
    if rtl_id != EFUSE_RTL_ID:
        raise ValueError(f"EFUSE rtl_id 0x{rtl_id:04x} != 0x{EFUSE_RTL_ID:04x}")

    cck = tuple(
        _sanitize_power_index(raw[_EFUSE_OFFSET_CCK_PWR_INDEX_A + i],
                              TX_POWER_INDEX_DEFAULT_CCK)
        for i in range(_EFUSE_OFFSET_HT40_1S_PWR_INDEX_A - _EFUSE_OFFSET_CCK_PWR_INDEX_A)
    )
    # Kernel memcpy's 5 ht40 bytes into a 6-slot array; slot 5 keeps whatever
    # the private struct held on allocation (0 on our side), then the same
    # sanitise loop runs over all 6.
    ht40_1s = [_sanitize_power_index(raw[_EFUSE_OFFSET_HT40_1S_PWR_INDEX_A + i],
                                     TX_POWER_INDEX_DEFAULT_HT40)
               for i in range(5)]
    ht40_1s.append(_sanitize_power_index(0, TX_POWER_INDEX_DEFAULT_HT40))
    ht40_1s = tuple(ht40_1s)

    diff_byte = raw[_EFUSE_OFFSET_OFDM_HT20_DIFF]
    ofdm_diff_a = _read_signed_nibble(diff_byte, hi=False)
    ht20_diff_a = _read_signed_nibble(diff_byte, hi=True)

    mac = raw[_EFUSE_OFFSET_MAC:_EFUSE_OFFSET_MAC + 6]
    if mac == b"\xFF" * 6 or mac == b"\x00" * 6:
        mac = None

    crystal_cap = raw[_EFUSE_OFFSET_XTAL_K] & 0x3F

    return EfuseDefaults(
        cck_tx_power_index_A=cck,
        ht40_1s_tx_power_index_A=ht40_1s,
        ofdm_tx_power_diff_a=ofdm_diff_a,
        ht20_tx_power_diff_a=ht20_diff_a,
        mac_address=bytes(mac) if mac else None,
        default_crystal_cap=crystal_cap,
        raw=raw,
    )


def read_and_parse(t: RTL8188FTVTransport) -> EfuseDefaults:
    """Convenience: read full EFUSE map, parse into EfuseDefaults."""
    raw = read_efuse_map(t)
    parsed = parse_efuse_8188fu(raw)
    logger.info(
        "EFUSE: MAC=%s cck_pwr=%s ht40_1s_pwr=%s ofdm_diff=%+d ht20_diff=%+d",
        parsed.mac_address.hex(":") if parsed.mac_address else "(unset)",
        [f"0x{x:02x}" for x in parsed.cck_tx_power_index_A],
        [f"0x{x:02x}" for x in parsed.ht40_1s_tx_power_index_A],
        parsed.ofdm_tx_power_diff_a,
        parsed.ht20_tx_power_diff_a,
    )
    return parsed