"""rtlwifi efuse.c: the EFUSE shadow-map read and rtl_get_hwinfo, as rtl8192cu reaches them."""
from __future__ import annotations

import time

from . import constants as C
from .constants import HalCfgMaps as maps
from .priv import RtlPriv


def efuse_power_switch(rtlpriv: RtlPriv, write: bool, pwrstate: bool) -> None:
    """efuse.c:1099."""
    io = rtlpriv.io
    hw_type = rtlpriv.rtlhal.hw_type
    if pwrstate and hw_type != C.HARDWARE_TYPE_RTL8192SE:
        if hw_type != C.HARDWARE_TYPE_RTL8192CE and hw_type != C.HARDWARE_TYPE_RTL8192DE:
            io.write8(maps.EFUSE_ACCESS, 0x69)
        else:
            tmpv16 = io.read16(maps.SYS_ISO_CTRL)
            if not (tmpv16 & maps.EFUSE_PWC_EV12V):
                tmpv16 |= maps.EFUSE_PWC_EV12V
                io.write16(maps.SYS_ISO_CTRL, tmpv16)
        tmpv16 = io.read16(maps.SYS_FUNC_EN)
        if not (tmpv16 & maps.EFUSE_FEN_ELDR):
            tmpv16 |= maps.EFUSE_FEN_ELDR
            io.write16(maps.SYS_FUNC_EN, tmpv16)

        tmpv16 = io.read16(maps.SYS_CLK)
        if not (tmpv16 & maps.EFUSE_LOADER_CLK_EN) or not (tmpv16 & maps.EFUSE_ANA8M):
            tmpv16 |= maps.EFUSE_LOADER_CLK_EN | maps.EFUSE_ANA8M
            io.write16(maps.SYS_CLK, tmpv16)

    if pwrstate:
        if write:
            tempval = io.read8(maps.EFUSE_TEST + 3)
            if hw_type == C.HARDWARE_TYPE_RTL8812AE:
                tempval &= ~(C.BIT(3) | C.BIT(4) | C.BIT(5) | C.BIT(6)) & 0xFF
                tempval |= C.VOLTAGE_V25 << 3
            elif hw_type != C.HARDWARE_TYPE_RTL8192SE:
                tempval &= 0x0F
                tempval |= C.VOLTAGE_V25 << 4
            io.write8(maps.EFUSE_TEST + 3, tempval | 0x80)
        if hw_type == C.HARDWARE_TYPE_RTL8192SE:
            io.write8(maps.EFUSE_CLK, 0x03)
    else:
        if hw_type != C.HARDWARE_TYPE_RTL8192CE and hw_type != C.HARDWARE_TYPE_RTL8192DE:
            io.write8(maps.EFUSE_ACCESS, 0)
        if write:
            tempval = io.read8(maps.EFUSE_TEST + 3)
            io.write8(maps.EFUSE_TEST + 3, tempval & 0x7F)
        if hw_type == C.HARDWARE_TYPE_RTL8192SE:
            io.write8(maps.EFUSE_CLK, 0x02)


def read_efuse_byte(rtlpriv: RtlPriv, _offset: int) -> int:
    """efuse.c:162; max_attempts is 10 on USB (efuse.c:176)."""
    io = rtlpriv.io
    max_attempts = 10
    io.write8(maps.EFUSE_CTRL + 1, _offset & 0xFF)
    readbyte = io.read8(maps.EFUSE_CTRL + 2)
    io.write8(maps.EFUSE_CTRL + 2, ((_offset >> 8) & 0x03) | (readbyte & 0xFC))

    readbyte = io.read8(maps.EFUSE_CTRL + 3)
    io.write8(maps.EFUSE_CTRL + 3, readbyte & 0x7F)

    retry = 0
    value32 = io.read32(maps.EFUSE_CTRL)
    while not ((value32 >> 24) & 0xFF & 0x80) and retry < max_attempts:
        value32 = io.read32(maps.EFUSE_CTRL)
        retry += 1

    time.sleep(50e-6)
    value32 = io.read32(maps.EFUSE_CTRL)
    return value32 & 0xFF


def read_efuse(rtlpriv: RtlPriv, _offset: int, _size_byte: int, pbuf: bytearray) -> None:
    """efuse.c:203."""
    rtlefuse = rtlpriv.efuse
    efuse_max_section = maps.EFUSE_MAX_SECTION_MAP
    efuse_len = maps.EFUSE_REAL_CONTENT_SIZE
    if _offset + _size_byte > maps.EFUSE_HWSET_MAX_SIZE:
        return

    efuse_tbl = bytearray(maps.EFUSE_HWSET_MAX_SIZE)
    efuse_word = [[0xFFFF] * efuse_max_section for _ in range(C.EFUSE_MAX_WORD_UNIT)]
    efuse_addr = 0
    efuse_utilized = 0
    u1temp = 0

    rtemp8 = read_efuse_byte(rtlpriv, efuse_addr)
    if rtemp8 != 0xFF:
        efuse_utilized += 1
        efuse_addr += 1

    while rtemp8 != 0xFF and efuse_addr < efuse_len:
        if (rtemp8 & 0x1F) == 0x0F:
            u1temp = (rtemp8 & 0xE0) >> 5
            rtemp8 = read_efuse_byte(rtlpriv, efuse_addr)
            if (rtemp8 & 0x0F) == 0x0F:
                efuse_addr += 1
                rtemp8 = read_efuse_byte(rtlpriv, efuse_addr)
                if rtemp8 != 0xFF and efuse_addr < efuse_len:
                    efuse_addr += 1
                continue
            offset = ((rtemp8 & 0xF0) >> 1) | u1temp
            wren = rtemp8 & 0x0F
            efuse_addr += 1
        else:
            offset = (rtemp8 >> 4) & 0x0F
            wren = rtemp8 & 0x0F

        if offset < efuse_max_section:
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if not (wren & 0x01):
                    rtemp8 = read_efuse_byte(rtlpriv, efuse_addr)
                    efuse_addr += 1
                    efuse_utilized += 1
                    efuse_word[i][offset] = rtemp8 & 0xFF
                    if efuse_addr >= efuse_len:
                        break
                    rtemp8 = read_efuse_byte(rtlpriv, efuse_addr)
                    efuse_addr += 1
                    efuse_utilized += 1
                    efuse_word[i][offset] |= (rtemp8 << 8) & 0xFF00
                    if efuse_addr >= efuse_len:
                        break
                wren >>= 1

        rtemp8 = read_efuse_byte(rtlpriv, efuse_addr)
        if rtemp8 != 0xFF and efuse_addr < efuse_len:
            efuse_utilized += 1
            efuse_addr += 1

    for i in range(efuse_max_section):
        for j in range(C.EFUSE_MAX_WORD_UNIT):
            efuse_tbl[i * 8 + j * 2] = efuse_word[j][i] & 0xFF
            efuse_tbl[i * 8 + j * 2 + 1] = (efuse_word[j][i] >> 8) & 0xFF

    for i in range(_size_byte):
        pbuf[i] = efuse_tbl[_offset + i]

    rtlefuse.efuse_usedbytes = efuse_utilized
    rtlefuse.efuse_usedpercentage = (efuse_utilized * 100) // efuse_len & 0xFF


def efuse_read_all_map(rtlpriv: RtlPriv, efuse: bytearray) -> None:
    """efuse.c:652."""
    efuse_power_switch(rtlpriv, False, True)
    read_efuse(rtlpriv, 0, maps.EFUSE_HWSET_MAX_SIZE, efuse)
    efuse_power_switch(rtlpriv, False, False)


def rtl_efuse_shadow_map_update(rtlpriv: RtlPriv) -> None:
    """efuse.c:494."""
    rtlefuse = rtlpriv.efuse
    init_map = rtlefuse.efuse_map[C.EFUSE_INIT_MAP]
    if rtlefuse.autoload_failflag:
        init_map[:maps.EFUSE_HWSET_MAX_SIZE] = b"\xff" * maps.EFUSE_HWSET_MAX_SIZE
    else:
        efuse_read_all_map(rtlpriv, init_map)
    rtlefuse.efuse_map[C.EFUSE_MODIFY_MAP][:maps.EFUSE_HWSET_MAX_SIZE] = init_map[:maps.EFUSE_HWSET_MAX_SIZE]


def _u16(buf: bytes | bytearray, offset: int) -> int:
    return buf[offset] | (buf[offset + 1] << 8)


def rtl_get_hwinfo(rtlpriv: RtlPriv, max_size: int, hwinfo: bytearray, params: list[int]) -> int:
    """efuse.c:1222. Returns 1 when the map is unusable (no EFUSE boot, or bad EEPROM ID)."""
    rtlefuse = rtlpriv.efuse
    if rtlefuse.epromtype == C.EEPROM_BOOT_EFUSE:
        rtl_efuse_shadow_map_update(rtlpriv)
    else:
        return 1

    hwinfo[:max_size] = rtlefuse.efuse_map[C.EFUSE_INIT_MAP][:max_size]

    eeprom_id = _u16(hwinfo, 0)
    rtlefuse.autoload_failflag = eeprom_id != params[0]
    if rtlefuse.autoload_failflag:
        return 1

    rtlefuse.eeprom_vid = _u16(hwinfo, params[1])
    rtlefuse.eeprom_did = _u16(hwinfo, params[2])
    rtlefuse.eeprom_svid = _u16(hwinfo, params[3])
    rtlefuse.eeprom_smid = _u16(hwinfo, params[4])
    rtlefuse.dev_addr = bytes(hwinfo[params[5]:params[5] + 6])
    rtlefuse.eeprom_channelplan = hwinfo[params[6]]
    rtlefuse.eeprom_version = _u16(hwinfo, params[7]) & 0xFF
    rtlefuse.txpwr_fromeprom = True
    rtlefuse.eeprom_oemid = hwinfo[params[8]]
    rtlefuse.channel_plan = params[9]
    return 0


def efuse_read_1byte(rtlpriv: RtlPriv, address: int) -> int:
    """efuse.c:70."""
    io = rtlpriv.io
    efuse_len = maps.EFUSE_REAL_CONTENT_SIZE
    if address >= efuse_len:
        return 0xFF
    io.write8(maps.EFUSE_CTRL + 1, address & 0xFF)
    bytetemp = io.read8(maps.EFUSE_CTRL + 2)
    io.write8(maps.EFUSE_CTRL + 2, ((address >> 8) & 0x03) | (bytetemp & 0xFC))

    bytetemp = io.read8(maps.EFUSE_CTRL + 3)
    io.write8(maps.EFUSE_CTRL + 3, bytetemp & 0x7F)

    bytetemp = io.read8(maps.EFUSE_CTRL + 3)
    k = 0
    while not (bytetemp & 0x80):
        bytetemp = io.read8(maps.EFUSE_CTRL + 3)
        k += 1
        if k == 1000:
            break
    return io.read8(maps.EFUSE_CTRL)


def _rtl_fw_block_write_usb(rtlpriv: RtlPriv, buffer: bytes) -> None:
    """efuse.c:1299."""
    start = C.START_ADDRESS
    offset = 0
    size = len(buffer)
    while size > 0:
        if size >= 64:
            n = 64
        elif size >= 8:
            n = 8
        else:
            n = 1
        rtlpriv.io.write_chunk(start, buffer[offset:offset + n])
        start += n
        offset += n
        size -= n


def rtl_fw_block_write(rtlpriv: RtlPriv, buffer: bytes) -> None:
    """efuse.c:1321."""
    if rtlpriv.rtlhal.interface == C.INTF_PCI:
        for i, byte in enumerate(buffer):
            rtlpriv.io.write8(C.START_ADDRESS + i, byte)
    elif rtlpriv.rtlhal.interface == C.INTF_USB:
        _rtl_fw_block_write_usb(rtlpriv, buffer)


def rtl_fw_page_write(rtlpriv: RtlPriv, page: int, buffer: bytes) -> None:
    """efuse.c:1336."""
    u8page = page & 0x07
    value8 = (rtlpriv.io.read8(C.REG_MCUFWDL + 2) & 0xF8) | u8page
    rtlpriv.io.write8(C.REG_MCUFWDL + 2, value8)
    rtl_fw_block_write(rtlpriv, buffer)


def rtl_fill_dummy(pfwbuf: bytes) -> bytes:
    """efuse.c:1350: zero-pad to a 4-byte multiple."""
    remain = len(pfwbuf) % 4
    remain = 0 if remain == 0 else 4 - remain
    return bytes(pfwbuf) + bytes(remain)
