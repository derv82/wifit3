"""RTL8192EU EFUSE read + per-chip parse.

Cleanroom port of:

* `rtl8xxxu_read_efuse8`   — `core.c:1745-1778` (single-byte EFUSE read)
* `rtl8xxxu_read_efuse`    — `core.c:1780-1890` (full EFUSE map walker)
* `rtl8192eu_parse_efuse`  — `8192e.c:588-637` (8192eu-specific struct decode)

EFUSE is the chip's on-die one-time-programmable memory — burned at the
factory with the dongle's MAC address + per-channel TX power calibration +
RF / regulatory params. Reading it is a polled register protocol on
REG_EFUSE_CTRL (0x0030):

    write address bytes  →  trigger read (clear bit 7 of CTRL+3)  →
    poll bit 31 of CTRL  →  read 8-bit result from CTRL[7:0]

The map itself is variable-length-encoded: 8-bit headers carry an offset
+ a 4-bit "word valid" mask; data follows in the next bytes. The walker
unpacks all valid words into a 512-byte raw buffer; per-chip parsers then
index fixed offsets out of that.

The 8192eu EFUSE layout lives in `struct rtl8192eu_efuse`
(`rtl8xxxu.h:1130-1164`); the TX-power calibration block it embeds is
`struct rtl8192eu_efuse_tx_power` (`rtl8xxxu.h:1122-1128`).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

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
from .transport import RTL8192EUTransport

logger = logging.getLogger(__name__)


# ---- low-level byte read --------------------------------------------


def read_efuse_byte(t: RTL8192EUTransport, offset: int) -> int:
    """Port of `rtl8xxxu_read_efuse8` (core.c:1745-1778): one EFUSE byte.

    Raises IOError on poll timeout (the kernel's `-EIO`).
    """
    # Write address: low 8 bits to CTRL+1, high 2 bits into CTRL+2[1:0].
    t.write8(REG_EFUSE_CTRL + 1, offset & 0xFF)
    val8 = t.read8(REG_EFUSE_CTRL + 2)
    val8 = (val8 & 0xFC) | ((offset >> 8) & 0x03)
    t.write8(REG_EFUSE_CTRL + 2, val8)

    # Trigger read: clear bit 7 of CTRL+3 (core.c:1759-1760).
    val8 = t.read8(REG_EFUSE_CTRL + 3)
    t.write8(REG_EFUSE_CTRL + 3, val8 & 0x7F)

    # core.c:1763 — a first read whose result the kernel discards.
    t.read32(REG_EFUSE_CTRL)
    for _ in range(RTL8XXXU_MAX_REG_POLL):
        val32 = t.read32(REG_EFUSE_CTRL)
        if val32 & (1 << 31):
            break
    else:
        raise IOError(f"EFUSE read timed out at offset 0x{offset:04x}")

    time.sleep(0.000050)  # udelay(50), core.c:1773
    val32 = t.read32(REG_EFUSE_CTRL)
    return val32 & 0xFF


# ---- raw-map walker -------------------------------------------------


def read_efuse_map(t: RTL8192EUTransport) -> bytes:
    """Port of `rtl8xxxu_read_efuse` (core.c:1780-1890).

    Returns the 512-byte unpacked EFUSE map. Bytes the variable-length
    encoding didn't touch stay `0xFF` (the kernel pre-fills with that).
    Runs the prep sequence (1.2V power, ELDR reset, clock-loader enable)
    then the access-window dance (ACCESS_ENABLE → walk → ACCESS_DISABLE).

    The kernel's `is_multi_func` REG_EFUSE_TEST branch (core.c:1794-1798)
    is skipped: 8192eu never sets it (`rtl8192eu_identify_chip`,
    8192e.c:465-510).
    """
    val16 = t.read16(REG_9346CR)
    if val16 & EEPROM_ENABLE:
        logger.debug("EFUSE: has_eeprom bit set")
    if val16 & EEPROM_BOOT:
        logger.debug("EFUSE: boot_eeprom bit set (booted from EEPROM)")

    t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_ENABLE)

    # 1.2V power / ELDR reset / loader+8M clock (core.c:1805-1825).
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
                # Extended header: a second byte carries more offset bits.
                offset = (header & 0xE0) >> 5
                extheader = read_efuse_byte(t, efuse_addr)
                efuse_addr += 1
                if (extheader & 0x0F) == 0x0F:
                    continue  # all words disabled in this group
                offset |= (extheader & 0xF0) >> 1
                word_mask = extheader & 0x0F
            else:
                offset = (header >> 4) & 0x0F
                word_mask = header & 0x0F

            map_addr = offset * 8
            for i in range(EFUSE_MAX_WORD_UNIT):
                if word_mask & (1 << i):
                    map_addr += 2  # bit set = word disabled
                    continue
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
        t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_DISABLE)

    return bytes(raw)


# ---- 8192eu per-chip parse ------------------------------------------

# `struct rtl8192eu_efuse` (rtl8xxxu.h:1130-1164).
_EFUSE_OFFSET_RTL_ID = 0x00
_EFUSE_RTL_ID_8192EU = 0x8129              # 8192e.c:593
_EFUSE_OFFSET_TX_POWER_A = 0x10            # tx_power_index_A  (rtl8xxxu.h:1133)
_EFUSE_OFFSET_TX_POWER_B = 0x3A            # tx_power_index_B  (rtl8xxxu.h:1134)
_EFUSE_OFFSET_XTAL_K = 0xB9                # xtal_k            (rtl8xxxu.h:1137)
_EFUSE_OFFSET_MAC = 0xD7                   # mac_addr[6]       (rtl8xxxu.h:1159)

# `struct rtl8192eu_efuse_tx_power` (rtl8xxxu.h:1122-1128): `cck_base[6]`
# + `ht40_base[5]` + a 1-byte `ht20_ofdm_1s_diff` + three 2-byte `pwr_diff`;
# size 0x2a, so tx_power_index_B lands at 0x3a.
_TX_POWER_OFFSET_CCK_BASE = 0x00           # u8 cck_base[6]
_TX_POWER_OFFSET_HT40_BASE = 0x06          # u8 ht40_base[5]
_TX_POWER_OFFSET_HT20_OFDM_1S_DIFF = 0x0B  # struct rtl8723au_idx
_TX_POWER_OFFSET_PWR_DIFF = 0x0C           # struct rtl8723bu_pwr_idx[3]

_CCK_BASE_LEN = 6                          # RTL8XXXU_MAX_CHANNEL_GROUPS (rtl8xxxu.h:84)
_HT40_BASE_LEN = 5                         # sizeof(ht40_base) as memcpy'd in 8192e.c:605
_TX_COUNT = 4                              # RTL8723B_TX_COUNT (rtl8xxxu.h:82)

# wifit3 fallback used only when the EFUSE read fails outright (the kernel
# aborts probe instead); not a source-derived value.
_FALLBACK_POWER_INDEX = 0x22


class PathPowerDiff(NamedTuple):
    """`struct rtl8723au_idx` — 4-bit signed per-path power delta (rtl8xxxu.h:965-973)."""

    a: int
    b: int


@dataclass
class EfuseDefaults:
    """EFUSE-derived bring-up params for 8192eu.

    Field names mirror the `struct rtl8xxxu_priv` members the kernel parse
    fills (rtl8xxxu.h:1811-1854). Default-constructed values stand in when
    the EFUSE read itself fails.
    """

    cck_tx_power_index_A: tuple[int, ...] = field(
        default_factory=lambda: (_FALLBACK_POWER_INDEX,) * _CCK_BASE_LEN
    )
    cck_tx_power_index_B: tuple[int, ...] = field(
        default_factory=lambda: (_FALLBACK_POWER_INDEX,) * _CCK_BASE_LEN
    )
    ht40_1s_tx_power_index_A: tuple[int, ...] = field(
        default_factory=lambda: (_FALLBACK_POWER_INDEX,) * _CCK_BASE_LEN
    )
    ht40_1s_tx_power_index_B: tuple[int, ...] = field(
        default_factory=lambda: (_FALLBACK_POWER_INDEX,) * _CCK_BASE_LEN
    )
    ofdm_tx_power_diff: tuple[PathPowerDiff, ...] = field(
        default_factory=lambda: (PathPowerDiff(0, 0),) * _TX_COUNT
    )
    ht20_tx_power_diff: tuple[PathPowerDiff, ...] = field(
        default_factory=lambda: (PathPowerDiff(0, 0),) * _TX_COUNT
    )
    ht40_tx_power_diff: tuple[PathPowerDiff, ...] = field(
        default_factory=lambda: (PathPowerDiff(0, 0),) * _TX_COUNT
    )
    mac_address: Optional[bytes] = None
    default_crystal_cap: int = 0       # EFUSE xtal_k & 0x3f; 0 = leave HW default
    raw: Optional[bytes] = None


def _signed4(nibble: int) -> int:
    """Sign-extend the 4-bit two's-complement power deltas (`rtl8723au_idx`)."""
    return nibble - 16 if nibble & 0x8 else nibble


def _idx_path(nibble_byte: int) -> PathPowerDiff:
    """`struct rtl8723au_idx`: `a` = bits[3:0], `b` = bits[7:4] (rtl8xxxu.h:965-973)."""
    return PathPowerDiff(a=_signed4(nibble_byte & 0x0F), b=_signed4(nibble_byte >> 4))


def _pwr_diff(raw: bytes, block: int, index: int) -> tuple[int, int, int, int]:
    """`struct rtl8723bu_pwr_idx` bytes → (ht20, ht40, ofdm, cck), little-endian.

    Byte 0 packs ht20 in bits[3:0] and ht40 in bits[7:4]; byte 1 packs ofdm
    in bits[3:0] and cck in bits[7:4] (rtl8xxxu.h:1058-1070).
    """
    lo = raw[block + _TX_POWER_OFFSET_PWR_DIFF + 2 * index]
    hi = raw[block + _TX_POWER_OFFSET_PWR_DIFF + 2 * index + 1]
    return (
        _signed4(lo & 0x0F),
        _signed4(lo >> 4),
        _signed4(hi & 0x0F),
        _signed4(hi >> 4),
    )


def parse_efuse_8192eu(raw: bytes) -> EfuseDefaults:
    """Port of `rtl8192eu_parse_efuse` (8192e.c:588-637).

    Decodes MAC, the per-path CCK/HT40-1S power bases, and the per-TX-count
    OFDM/HT20/HT40 power deltas out of the raw EFUSE map.
    """
    if len(raw) < EFUSE_MAP_LEN:
        raise ValueError(f"EFUSE map too short: {len(raw)} bytes")

    rtl_id = raw[_EFUSE_OFFSET_RTL_ID] | (raw[_EFUSE_OFFSET_RTL_ID + 1] << 8)
    if rtl_id != _EFUSE_RTL_ID_8192EU:  # 8192e.c:593
        raise ValueError(f"EFUSE rtl_id 0x{rtl_id:04x} is not 8192eu 0x8129")

    a = _EFUSE_OFFSET_TX_POWER_A
    b = _EFUSE_OFFSET_TX_POWER_B

    cck_a = tuple(raw[a + _TX_POWER_OFFSET_CCK_BASE + i] for i in range(_CCK_BASE_LEN))
    cck_b = tuple(raw[b + _TX_POWER_OFFSET_CCK_BASE + i] for i in range(_CCK_BASE_LEN))

    # 8192e.c:603-608 memcpy's only sizeof(ht40_base) == 5 bytes into the
    # 6-wide priv array; priv is zeroed, so the last slot stays 0.
    ht40_a = tuple(raw[a + _TX_POWER_OFFSET_HT40_BASE + i] for i in range(_HT40_BASE_LEN))
    ht40_b = tuple(raw[b + _TX_POWER_OFFSET_HT40_BASE + i] for i in range(_HT40_BASE_LEN))
    ht40_a += (0,) * (_CCK_BASE_LEN - _HT40_BASE_LEN)
    ht40_b += (0,) * (_CCK_BASE_LEN - _HT40_BASE_LEN)

    # index 0 is the single HT20/OFDM offset burned per path (8192e.c:610-616);
    # ofdm_tx_power_diff[0] is left at its zeroed default.
    ht20_0 = PathPowerDiff(
        a=_idx_path(raw[a + _TX_POWER_OFFSET_HT20_OFDM_1S_DIFF]).b,
        b=_idx_path(raw[b + _TX_POWER_OFFSET_HT20_OFDM_1S_DIFF]).b,
    )

    ofdm_diff = [PathPowerDiff(0, 0)]
    ht20_diff = [ht20_0]
    ht40_diff = [PathPowerDiff(0, 0)]
    for i in range(1, _TX_COUNT):
        pa = _pwr_diff(raw, a, i - 1)
        pb = _pwr_diff(raw, b, i - 1)
        ofdm_diff.append(PathPowerDiff(a=pa[2], b=pb[2]))
        ht20_diff.append(PathPowerDiff(a=pa[0], b=pb[0]))
        ht40_diff.append(PathPowerDiff(a=pa[1], b=pb[1]))

    return EfuseDefaults(
        cck_tx_power_index_A=cck_a,
        cck_tx_power_index_B=cck_b,
        ht40_1s_tx_power_index_A=ht40_a,
        ht40_1s_tx_power_index_B=ht40_b,
        ofdm_tx_power_diff=tuple(ofdm_diff),
        ht20_tx_power_diff=tuple(ht20_diff),
        ht40_tx_power_diff=tuple(ht40_diff),
        mac_address=bytes(raw[_EFUSE_OFFSET_MAC : _EFUSE_OFFSET_MAC + 6]),
        default_crystal_cap=raw[_EFUSE_OFFSET_XTAL_K] & 0x3F,  # 8192e.c:635
        raw=raw,
    )


def read_and_parse(t: RTL8192EUTransport) -> EfuseDefaults:
    """Convenience: read full EFUSE map, parse into EfuseDefaults."""
    raw = read_efuse_map(t)
    parsed = parse_efuse_8192eu(raw)
    logger.info(
        "EFUSE: MAC=%s cck_A=%s ht40_1s_A=%s ht20_diff=%s",
        parsed.mac_address.hex(":") if parsed.mac_address else "(unset)",
        [f"0x{x:02x}" for x in parsed.cck_tx_power_index_A],
        [f"0x{x:02x}" for x in parsed.ht40_1s_tx_power_index_A],
        [(d.a, d.b) for d in parsed.ht20_tx_power_diff],
    )
    return parsed
