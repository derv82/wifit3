"""RTL8922AU USB register-access transport, ported from rtw89-7.2 usb.c.

Every register op is a vendor control transfer on endpoint 0 (rtw89_usb_vendorreq). A read of
a CMAC-window register can come back R32_DEAD until its clock is on, so read_cmac re-enables
the clock and re-reads. This is the layer every later op sits on.
"""
import struct

import usb.core

from .constants import (
    RTW89_USB_VENQT, RTW89_USB_VENQT_READ, RTW89_USB_VENQT_WRITE,
    RTW89_USB_VENDORREQ_ATTEMPTS, RTW89_USB_VENDORREQ_TIMEOUT_MS,
    R_AX_CMAC_REG_START, R_AX_CMAC_REG_END, RTW89_R32_DEAD, MAC_REG_POOL_COUNT,
    R_AX_CK_EN, B_AX_CMAC_ALLCKEN,
)


def _access_cmac(addr: int) -> bool:
    """True for the CMAC register window. [SRC] mac.h:587 ACCESS_CMAC."""
    return R_AX_CMAC_REG_START <= addr <= R_AX_CMAC_REG_END


class RTL8922AUTransport:
    """Vendor-control register access for the rtw89 8922A over USB."""

    def __init__(self, dev: usb.core.Device):
        self.dev = dev

    def _vendorreq(self, addr: int, data: bytes, length: int, reqtype: int) -> bytes:
        """rtw89_usb_vendorreq: one endpoint-0 vendor control transfer, retried up to 10
        times. [SRC] usb.c:20-73. wValue = addr & 0xFFFF, wIndex = (addr >> 16) & 0xFF."""
        value = addr & 0xFFFF
        index = (addr >> 16) & 0xFF
        for _ in range(RTW89_USB_VENDORREQ_ATTEMPTS):
            try:
                if reqtype == RTW89_USB_VENQT_READ:
                    res = self.dev.ctrl_transfer(reqtype, RTW89_USB_VENQT, value, index,
                                                 length, RTW89_USB_VENDORREQ_TIMEOUT_MS)
                    if len(res) == length:
                        return bytes(res)
                else:
                    n = self.dev.ctrl_transfer(reqtype, RTW89_USB_VENQT, value, index,
                                               data, RTW89_USB_VENDORREQ_TIMEOUT_MS)
                    if n == length:
                        return b""
            except usb.core.USBError:
                pass
        # TODO: verify, untested here. The kernel flags RTW89_FLAG_UNPLUGGED after 4
        # continual I/O errors ([SRC] usb.c:59-72); wire it once the unplug path is ported.
        return b""

    def read8(self, addr: int) -> int:
        """rtw89_usb_ops_read8. [SRC] usb.c:113-123."""
        if _access_cmac(addr):
            return self._read_cmac(addr) & 0xFF
        d = self._vendorreq(addr, b"", 1, RTW89_USB_VENQT_READ)
        return d[0] if len(d) >= 1 else 0

    def read16(self, addr: int) -> int:
        """rtw89_usb_ops_read16. [SRC] usb.c:125-135."""
        if _access_cmac(addr):
            return self._read_cmac(addr) & 0xFFFF
        d = self._vendorreq(addr, b"", 2, RTW89_USB_VENQT_READ)
        return struct.unpack("<H", d)[0] if len(d) >= 2 else 0

    def read32(self, addr: int) -> int:
        """rtw89_usb_ops_read32. [SRC] usb.c:137-148."""
        if _access_cmac(addr):
            return self._read_cmac(addr)
        d = self._vendorreq(addr, b"", 4, RTW89_USB_VENQT_READ)
        return struct.unpack("<I", d)[0] if len(d) >= 4 else 0

    def write8(self, addr: int, val: int) -> None:
        """rtw89_usb_ops_write8. [SRC] usb.c:150-155."""
        self._vendorreq(addr, struct.pack("<B", val & 0xFF), 1, RTW89_USB_VENQT_WRITE)

    def write16(self, addr: int, val: int) -> None:
        """rtw89_usb_ops_write16. [SRC] usb.c:157-162."""
        self._vendorreq(addr, struct.pack("<H", val & 0xFFFF), 2, RTW89_USB_VENQT_WRITE)

    def write32(self, addr: int, val: int) -> None:
        """rtw89_usb_ops_write32. [SRC] usb.c:164-169."""
        self._vendorreq(addr, struct.pack("<I", val & 0xFFFFFFFF), 4, RTW89_USB_VENQT_WRITE)

    def write32_quiet(self, addr: int, val: int) -> None:
        """write32 with the kernel's warn suppressed; identical wire op. [SRC] usb.c:171-177."""
        self.write32(addr, val)

    def write32_set(self, addr: int, bits: int) -> None:
        """rtw89_write32_set: read-modify-write, bits OR'd in. [SRC] core.h:7201."""
        self.write32(addr, self.read32(addr) | bits)

    def write32_clr(self, addr: int, bits: int) -> None:
        """rtw89_write32_clr: read-modify-write, bits masked out. [SRC] core.h:7228."""
        self.write32(addr, self.read32(addr) & ~bits & 0xFFFFFFFF)

    def write8_set(self, addr: int, bits: int) -> None:
        """rtw89_write8_set: byte read-modify-write, bits OR'd in. [SRC] core.h:7183."""
        self.write8(addr, self.read8(addr) | bits)

    def write8_clr(self, addr: int, bits: int) -> None:
        """rtw89_write8_clr: byte read-modify-write, bits masked out. [SRC] core.h:7210."""
        self.write8(addr, self.read8(addr) & ~bits & 0xFF)

    def write16_set(self, addr: int, bits: int) -> None:
        """rtw89_write16_set: 16-bit read-modify-write, bits OR'd in. [SRC] core.h."""
        self.write16(addr, self.read16(addr) | bits)

    def write16_clr(self, addr: int, bits: int) -> None:
        """rtw89_write16_clr: 16-bit read-modify-write, bits masked out. [SRC] core.h."""
        self.write16(addr, self.read16(addr) & ~bits & 0xFFFF)

    def _read_cmac(self, addr: int) -> int:
        """rtw89_usb_read_cmac: read a CMAC-window register, re-enabling its clock and
        re-reading while it returns R32_DEAD. [SRC] usb.c:83-108."""
        addr32 = addr & ~0x3
        shift = (addr & 0x3) * 8
        count = 0
        while True:
            d = self._vendorreq(addr32, b"", 4, RTW89_USB_VENQT_READ)
            val32 = struct.unpack("<I", d)[0] if len(d) >= 4 else RTW89_R32_DEAD
            if val32 != RTW89_R32_DEAD:
                break
            if count >= MAC_REG_POOL_COUNT:
                val32 = RTW89_R32_DEAD
                break
            self.write32(R_AX_CK_EN, B_AX_CMAC_ALLCKEN)
            count += 1
        return val32 >> shift
