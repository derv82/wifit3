"""RTL8188EUS EFUSE read + per-chip parse.

Cleanroom port of:

* `rtl8xxxu_read_efuse8`   — `core.c:1746-1778` (single-byte EFUSE read)
* `rtl8xxxu_read_efuse`    — `core.c:1780-1890` (full EFUSE map walker)
* `rtl8188eu_parse_efuse`  — `8188e.c:537-557` (8188e-specific struct decode)

EFUSE is the chip's on-die one-time-programmable memory — burned at the
factory with the dongle's MAC address + per-channel TX power calibration +
RF / regulatory params. Reading it is a polled register protocol on
REG_EFUSE_CTRL (0x0030):

    write address bytes  →  trigger read (clear bit 7 of CTRL+3)  →
    poll bit 31 of CTRL  →  read 8-bit result from CTRL[7:0]

The map itself is variable-length-encoded: 8-bit headers carry an offset
+ a 4-bit "word valid" mask; data follows in the next bytes. The walker
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
)
from .transport import RTL8188EUSTransport

logger = logging.getLogger(__name__)


# ---- low-level byte read --------------------------------------------


def read_efuse_byte(t: RTL8188EUSTransport, offset: int) -> int:
    """Port of `rtl8xxxu_read_efuse8` (core.c:1746-1778).

    Reads one byte from EFUSE at the given offset. Raises IOError on
    poll timeout.
    """
    # Write address: low 8 bits go to CTRL+1, high 2 bits OR into CTRL+2[1:0].
    t.write8(REG_EFUSE_CTRL + 1, offset & 0xFF)
    val8 = t.read8(REG_EFUSE_CTRL + 2)
    val8 = (val8 & 0xFC) | ((offset >> 8) & 0x03)
    t.write8(REG_EFUSE_CTRL + 2, val8)

    # Trigger read: clear bit 7 of CTRL+3.
    val8 = t.read8(REG_EFUSE_CTRL + 3)
    t.write8(REG_EFUSE_CTRL + 3, val8 & 0x7F)

    # Poll for data-ready (bit 31 of CTRL).
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


def read_efuse_map(t: RTL8188EUSTransport) -> bytes:
    """Port of `rtl8xxxu_read_efuse` (core.c:1780-1890).

    Returns the 512-byte unpacked EFUSE map. Bytes that weren't written
    by the variable-length encoding remain `0xFF` (the kernel pre-fills
    with that and unpacks valid words on top).

    Performs the prep sequence (1.2V power, ELDR reset, clock-loader
    enable) and the access-window dance (ACCESS_ENABLE → walk →
    ACCESS_DISABLE) per the kernel.
    """
    # Probe boot-mode bits — informational only.
    val16 = t.read16(REG_9346CR)
    if val16 & EEPROM_ENABLE:
        logger.debug("EFUSE: has_eeprom bit set")
    if val16 & EEPROM_BOOT:
        logger.debug("EFUSE: boot_eeprom bit set (booted from EEPROM)")

    # Open EFUSE access window.
    t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_ENABLE)

    # Power + reset + clock prep (kernel core.c:1805-1825).
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
                # End of valid data — rest of the map is unwritten 0xFF.
                break

            if (header & 0x1F) == 0x0F:
                # Extended header: 2nd byte carries more offset bits + word_mask.
                offset = (header & 0xE0) >> 5
                extheader = read_efuse_byte(t, efuse_addr)
                efuse_addr += 1
                if (extheader & 0x0F) == 0x0F:
                    # All words disabled in this group.
                    continue
                offset |= (extheader & 0xF0) >> 1
                word_mask = extheader & 0x0F
            else:
                offset = (header >> 4) & 0x0F
                word_mask = header & 0x0F

            map_addr = offset * 8
            for i in range(EFUSE_MAX_WORD_UNIT):
                if word_mask & (1 << i):
                    # Bit set = word disabled; skip 2 bytes in dest only.
                    map_addr += 2
                    continue
                # Read 2 bytes of valid word into the map at map_addr.
                if map_addr >= EFUSE_MAP_LEN - 1:
                    raise IOError(
                        f"EFUSE map_addr out of range (0x{map_addr:04x}) — corrupt header"
                    )
                raw[map_addr] = read_efuse_byte(t, efuse_addr)
                efuse_addr += 1
                map_addr += 1
                raw[map_addr] = read_efuse_byte(t, efuse_addr)
                efuse_addr += 1
                map_addr += 1
    finally:
        # Close EFUSE access window regardless of success.
        t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_DISABLE)

    return bytes(raw)


# ---- 8188eu per-chip parse ------------------------------------------


# Offsets within the 512-byte EFUSE map for the 8188eu-specific fields.
# Derived from `struct rtl8188eu_efuse` layout (rtl8xxxu.h around line
# 1800). We avoid porting the full struct — only the fields M8 needs.

_EFUSE_OFFSET_CCK_PWR_INDEX_A = 0x10        # cck_tx_power_index_A[6]  (6 channel groups)
_EFUSE_OFFSET_HT40_1S_PWR_INDEX_A = 0x16    # ht40_1s_tx_power_index_A[6]
_EFUSE_OFFSET_HT20_PWR_DIFF = 0x1C          # ht20_tx_power_diff[3] (3 path×diff packed)
_EFUSE_OFFSET_OFDM_PWR_DIFF = 0x1B          # ofdm_tx_power_diff[3]
_EFUSE_OFFSET_HT40_PWR_DIFF = 0x1D          # ht40_tx_power_diff[3]
_EFUSE_OFFSET_MAC = 0xD7                    # mac_addr[6]


# Sane fallback values when the EFUSE map shows 0xFF (unprogrammed) for
# the power fields. ~17 dBm equivalent — matches what most retail
# 8188eu dongles ship with after burn-in.
_FALLBACK_POWER_INDEX = 0x22
_FALLBACK_DIFF = 0x00


@dataclass
class EfuseDefaults:
    """Parsed-from-EFUSE bring-up params for 8188eu.

    All fields fall back to sane lab defaults if the EFUSE shows 0xFF
    (unprogrammed or unreadable) — same `[[rfe-defaults-first]]` pattern
    we use for rtw88 chips. Real EFUSE values are preferred when
    available because they reflect the dongle's actual factory cal.
    """
    cck_tx_power_index_A: tuple[int, ...] = field(
        default_factory=lambda: (_FALLBACK_POWER_INDEX,) * 6
    )
    ht40_1s_tx_power_index_A: tuple[int, ...] = field(
        default_factory=lambda: (_FALLBACK_POWER_INDEX,) * 6
    )
    ofdm_tx_power_diff_a: int = _FALLBACK_DIFF
    ht20_tx_power_diff_a: int = _FALLBACK_DIFF
    ht40_tx_power_diff_a: int = _FALLBACK_DIFF
    mac_address: Optional[bytes] = None
    raw: Optional[bytes] = None


def _sanitize_power_index(b: int) -> int:
    """Clamp an EFUSE byte to a sensible 6-bit power index.

    Unprogrammed bytes read 0xFF; the chip's TX AGC field is 6 bits so
    0xFF would overflow + write garbage into adjacent fields. If the
    EFUSE byte is 0xFF we substitute the fallback default.
    """
    if b == 0xFF or b == 0x00:
        return _FALLBACK_POWER_INDEX
    return b & 0x3F


def parse_efuse_8188eu(raw: bytes) -> EfuseDefaults:
    """Port of `rtl8188eu_parse_efuse` (8188e.c:537-557).

    Picks the M8-relevant fields out of the raw EFUSE map and returns
    them in an :class:`EfuseDefaults`. Any 0xFF byte (unprogrammed)
    falls back to a sane default.
    """
    if len(raw) < EFUSE_MAP_LEN:
        raise ValueError(f"EFUSE map too short: {len(raw)} bytes")

    cck = tuple(
        _sanitize_power_index(raw[_EFUSE_OFFSET_CCK_PWR_INDEX_A + i]) for i in range(6)
    )
    ht40_1s = tuple(
        _sanitize_power_index(raw[_EFUSE_OFFSET_HT40_1S_PWR_INDEX_A + i])
        for i in range(6)
    )

    def _read_diff(offset: int) -> int:
        b = raw[offset]
        if b == 0xFF:
            return _FALLBACK_DIFF
        # Diff bytes are signed 4-bit (low nibble = path A).
        v = b & 0x0F
        return v - 16 if v & 0x08 else v

    ofdm_diff_a = _read_diff(_EFUSE_OFFSET_OFDM_PWR_DIFF)
    ht20_diff_a = _read_diff(_EFUSE_OFFSET_HT20_PWR_DIFF)
    ht40_diff_a = _read_diff(_EFUSE_OFFSET_HT40_PWR_DIFF)

    mac = raw[_EFUSE_OFFSET_MAC : _EFUSE_OFFSET_MAC + 6]
    if mac == b"\xFF" * 6 or mac == b"\x00" * 6:
        mac = None  # caller falls back to "no MAC override"

    return EfuseDefaults(
        cck_tx_power_index_A=cck,
        ht40_1s_tx_power_index_A=ht40_1s,
        ofdm_tx_power_diff_a=ofdm_diff_a,
        ht20_tx_power_diff_a=ht20_diff_a,
        ht40_tx_power_diff_a=ht40_diff_a,
        mac_address=bytes(mac) if mac else None,
        raw=raw,
    )


def read_and_parse(t: RTL8188EUSTransport) -> EfuseDefaults:
    """Convenience: read full EFUSE map, parse into EfuseDefaults."""
    raw = read_efuse_map(t)
    parsed = parse_efuse_8188eu(raw)
    logger.info(
        "EFUSE: MAC=%s cck_pwr=%s ht40_1s_pwr=%s ofdm_diff=%+d ht20_diff=%+d ht40_diff=%+d",
        parsed.mac_address.hex(":") if parsed.mac_address else "(unset)",
        [f"0x{x:02x}" for x in parsed.cck_tx_power_index_A],
        [f"0x{x:02x}" for x in parsed.ht40_1s_tx_power_index_A],
        parsed.ofdm_tx_power_diff_a,
        parsed.ht20_tx_power_diff_a,
        parsed.ht40_tx_power_diff_a,
    )
    return parsed
