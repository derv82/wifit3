"""RTL8188FTV DKMS EFUSE wire reads (M2).

Ported from ``GetEEPROMSize8188F`` + ``hal_EfuseCellSel``
(hal/rtl8188f/usb/usb_halinit.c:2300-2312,
hal/rtl8188f/rtl8188f_hal_init.c:3477-3488), ``Hal_EfusePowerSwitch`` +
``hal_EfuseSwitchToBank`` + ``hal_ReadEFuse_WiFi``
(hal/rtl8188f/rtl8188f_hal_init.c:1507-1580,1304-1347,1587-1710),
``efuse_OneByteRead`` (core/efuse/rtw_efuse.c:430-488) and
``Efuse_ReadAllMap``/``EFUSE_ShadowMapUpdate`` (core/efuse/rtw_efuse.c:1221-1245,1390-1418).
``Hal_EfusePgPacketRead`` (rtl8188f_hal_init.c:2219) is not on the read graph
(the walker uses ``efuse_OneByteRead`` directly) and is not ported.
"""
from __future__ import annotations

import time

from . import constants as C


def BIT(n: int) -> int:
    return 1 << n


def get_eeprom_size(t) -> int:
    return 6 if t.read16(C.REG_9346CR) & C.BOOT_FROM_EEPROM else 4


def cell_select(t) -> None:
    value32 = t.read32(C.EFUSE_TEST)
    t.write32(C.EFUSE_TEST, (value32 & ~C.EFUSE_SEL_MASK) | (C.EFUSE_WIFI_SEL_0 << 8))


def power_switch(t, write: bool, on: bool) -> None:
    if on:
        t.write8(C.REG_EFUSE_ACCESS_8188, C.EFUSE_ACCESS_ON_8188)
        tmp16 = t.read16(C.REG_SYS_FUNC_EN)
        if not tmp16 & C.FEN_ELDR:
            t.write16(C.REG_SYS_FUNC_EN, tmp16 | C.FEN_ELDR)
        tmp16 = t.read16(C.REG_SYS_CLKR)
        if not (tmp16 & C.LOADER_CLK_EN) or not (tmp16 & C.ANA8M):
            t.write16(C.REG_SYS_CLKR, tmp16 | C.LOADER_CLK_EN | C.ANA8M)
        if write:
            tmp8 = t.read8(C.EFUSE_TEST + 3)
            t.write8(C.EFUSE_TEST + 3, tmp8 | 0x80)
    else:
        t.write8(C.REG_EFUSE_ACCESS_8188, C.EFUSE_ACCESS_OFF)
        if write:
            tmp8 = t.read8(C.EFUSE_TEST + 3)
            t.write8(C.EFUSE_TEST + 3, tmp8 & 0x7F)


def switch_to_bank(t, bank: int) -> bool:
    value32 = t.read32(C.EFUSE_TEST)
    ok = True
    if bank == 0:
        value32 = (value32 & ~C.EFUSE_SEL_MASK) | (C.EFUSE_WIFI_SEL_0 << 8)
    elif bank == 1:
        value32 = (value32 & ~C.EFUSE_SEL_MASK) | (C.EFUSE_BT_SEL_0 << 8)
    elif bank == 2:
        value32 = (value32 & ~C.EFUSE_SEL_MASK) | (C.EFUSE_BT_SEL_1 << 8)
    elif bank == 3:
        value32 = (value32 & ~C.EFUSE_SEL_MASK) | (C.EFUSE_BT_SEL_2 << 8)
    else:
        value32 = (value32 & ~C.EFUSE_SEL_MASK) | (C.EFUSE_WIFI_SEL_0 << 8)
        ok = False
    t.write32(C.EFUSE_TEST, value32)
    return ok


def one_byte_read(t, addr: int, smic: bool) -> tuple[bool, int]:
    if smic:
        t.write16(0x34, t.read16(0x34) & ~BIT(11))
    t.write8(C.EFUSE_CTRL + 1, addr & 0xFF)
    t.write8(C.EFUSE_CTRL + 2, ((addr >> 8) & 0x03) | (t.read8(C.EFUSE_CTRL + 2) & 0xFC))
    t.write8(C.EFUSE_CTRL + 3, t.read8(C.EFUSE_CTRL + 3) & 0x7F)
    tmpidx = 0
    while not t.read8(C.EFUSE_CTRL + 3) & 0x80 and tmpidx < 1000:
        time.sleep(0.001)
        tmpidx += 1
    if tmpidx < 100:
        return True, t.read8(C.EFUSE_CTRL)
    return False, 0xFF


def word_cnts(word_en: int) -> int:
    return sum(1 for i in range(4) if not word_en & BIT(i))


def read_section_map(t, smic: bool, offset: int = 0, size: int = 512) -> bytes:
    if offset + size > C.EFUSE_MAP_LEN_8188F:
        raise ValueError(f"efuse read out of range: offset={offset} size={size}")
    table = bytearray([0xFF] * C.EFUSE_MAP_LEN_8188F)
    switch_to_bank(t, 0)
    phys = 0
    while phys < C.EFUSE_REAL_CONTENT_LEN_8188F:
        ok, header = one_byte_read(t, phys, smic)
        phys += 1
        if not ok or header == 0xFF:
            break
        if (header & 0x1F) == 0x0F:
            section = (header & 0xE0) >> 5
            ok, ext = one_byte_read(t, phys, smic)
            phys += 1
            if not ok or (ext & 0x0F) == 0x0F:
                continue
            section |= (ext & 0xF0) >> 1
            word_en = ext & 0x0F
        else:
            section = (header >> 4) & 0x0F
            word_en = header & 0x0F
        if section < C.EFUSE_MAX_SECTION_8188F:
            dst = section * C.PGPKT_DATA_SIZE
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if not word_en & BIT(i):
                    ok, lo = one_byte_read(t, phys, smic)
                    phys += 1
                    table[dst] = lo
                    ok, hi = one_byte_read(t, phys, smic)
                    phys += 1
                    table[dst + 1] = hi
                dst += 2
        else:
            phys += word_cnts(word_en) * 2
    return bytes(table[offset:offset + size])
