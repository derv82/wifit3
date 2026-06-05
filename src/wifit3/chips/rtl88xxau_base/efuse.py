"""RTL88xxAU EFUSE read mechanics — physical byte read + PG logical-map walk.

The probe phase reads the burned-in efuse to recover per-card parameters. The *read*
mechanics are family-shared (JAGUAR PG-block format, EFUSE_CTRL byte protocol); the
*parse* (which logical offsets hold the MAC / crystal / per-path TX-power) is per-chip
and lives in each chip package's ``efuse.py``.

[SRC] ReadEFuseByte (rtw_efuse.c:2209) + efuse_ReadEFuse (PG-block format). Gated by
REG_EFUSE_ACCESS (0x69 on, 0x00 off) by the caller around these reads.
"""
from __future__ import annotations

from . import registers as R


def efuse_one_byte_read(t, addr: int) -> int:
    """[SRC] ReadEFuseByte (rtw_efuse.c:2209) — one physical efuse byte.

    Write the 10-bit address (EFUSE_CTRL+1 low, +2 high preserving the top 6 bits),
    clear EFUSE_CTRL+3 bit7 to trigger, poll EFUSE_CTRL bit31 (ready), take the low byte.
    """
    t.write8(R.REG_EFUSE_CTRL + 1, addr & 0xFF)
    v = t.read8(R.REG_EFUSE_CTRL + 2)
    t.write8(R.REG_EFUSE_CTRL + 2, ((addr >> 8) & 0x03) | (v & 0xFC))
    v = t.read8(R.REG_EFUSE_CTRL + 3)
    t.write8(R.REG_EFUSE_CTRL + 3, v & 0x7F)
    value32 = t.read32(R.REG_EFUSE_CTRL)
    retry = 0
    while not ((value32 >> 24) & 0x80) and retry < 10000:
        value32 = t.read32(R.REG_EFUSE_CTRL)
        retry += 1
    value32 = t.read32(R.REG_EFUSE_CTRL)   # re-read after the HW settle delay
    return value32 & 0xFF


def efuse_one_byte_read_poll33(t, addr: int) -> int:
    """[SRC] efuse_OneByteRead (rtw_efuse.c:2343) — the *legacy* one-byte read.

    Distinct from ``efuse_one_byte_read`` (= the vendor's ``ReadEFuseByte``, which polls
    EFUSE_CTRL[31] via a 32-bit read): this variant polls EFUSE_CTRL+3 bit7 with 8-bit
    reads and takes the data as a single EFUSE_CTRL byte. The 8812a calls it directly for
    the FW-offload probe (``hal_InitPGData_8812A``) and the USB-type antenna/wmode reads
    (``hal_ReadUsbType_8812AU``); the PG-block map walk uses ``ReadEFuseByte`` instead.
    """
    t.write8(R.REG_EFUSE_CTRL + 1, addr & 0xFF)
    v = t.read8(R.REG_EFUSE_CTRL + 2)
    t.write8(R.REG_EFUSE_CTRL + 2, ((addr >> 8) & 0x03) | (v & 0xFC))
    v = t.read8(R.REG_EFUSE_CTRL + 3)
    t.write8(R.REG_EFUSE_CTRL + 3, v & 0x7F)        # clear bit7 -> trigger read
    tmpidx = 0
    while not (0x80 & t.read8(R.REG_EFUSE_CTRL + 3)) and tmpidx < 1000:
        tmpidx += 1
    return t.read8(R.REG_EFUSE_CTRL)


def read_logical_map(t) -> bytes:
    """[SRC] efuse_ReadEFuse — physical efuse PG stream -> 512 B logical map.

    Each PG block has a header (section offset + 4-bit word-enable, or an EXT_HEADER
    form) and contributes two bytes per enabled word to eFuseWord[section][word]; the
    64x4 words flatten into the logical map at ``section*8 + word*2``. JAGUAR-common
    across 8821au/8812au.
    """
    word = [[0xFFFF] * R.EFUSE_MAX_WORD_UNIT for _ in range(R.EFUSE_MAX_SECTION_JAGUAR)]
    addr = 0

    header = efuse_one_byte_read(t, addr)
    addr += 1
    if header == 0xFF:
        return b"\xFF" * R.EFUSE_MAP_LEN_JAGUAR          # empty efuse

    while header != 0xFF and addr < R.EFUSE_REAL_CONTENT_LEN_JAGUAR:
        if (header & 0x1F) == 0x0F:               # EXT_HEADER
            offset_2_0 = (header & 0xE0) >> 5
            ext = efuse_one_byte_read(t, addr)
            addr += 1
            if ext == 0xFF:
                break
            if (ext & 0x0F) == 0x0F:              # ALL_WORDS_DISABLED
                header = efuse_one_byte_read(t, addr)
                addr += 1
                break
            offset = ((ext & 0xF0) >> 1) | offset_2_0
            wden = ext & 0x0F
        else:
            offset = (header >> 4) & 0x0F
            wden = header & 0x0F

        if offset < R.EFUSE_MAX_SECTION_JAGUAR:
            for i in range(R.EFUSE_MAX_WORD_UNIT):
                if wden & (1 << i):               # word disabled
                    continue
                data = efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] = data & 0xFF
                if addr >= R.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
                data = efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] |= (data << 8) & 0xFF00
                if addr >= R.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
        else:                                     # invalid offset — skip its words
            for i in range(R.EFUSE_MAX_WORD_UNIT):
                if wden & 0x01:
                    continue
                addr += 1
                if addr >= R.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
                addr += 1
                if addr >= R.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break

        header = efuse_one_byte_read(t, addr)
        if header != 0xFF:
            addr += 1

    tbl = bytearray(b"\xFF" * R.EFUSE_MAP_LEN_JAGUAR)
    for i in range(R.EFUSE_MAX_SECTION_JAGUAR):
        for j in range(R.EFUSE_MAX_WORD_UNIT):
            tbl[i * 8 + j * 2] = word[i][j] & 0xFF
            tbl[i * 8 + j * 2 + 1] = (word[i][j] >> 8) & 0xFF
    return bytes(tbl)


def s4(n: int) -> int:
    """Signed 4-bit nibble -> int (PG_TXPWR_*_DIFF_TO_S8BIT)."""
    return n - 16 if (n & 0x8) else n
