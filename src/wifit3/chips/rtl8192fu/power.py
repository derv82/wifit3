import time

from . import constants as C
from .transport import RTL8192FUTransport


def _update8(transport: RTL8192FUTransport, register: int, clear: int = 0, set_: int = 0) -> None:
    transport.write8(register, (transport.read8(register) & ~clear) | set_)


def _update16(transport: RTL8192FUTransport, register: int, clear: int = 0, set_: int = 0) -> None:
    transport.write16(register, (transport.read16(register) & ~clear) | set_)


def _update32(transport: RTL8192FUTransport, register: int, clear: int = 0, set_: int = 0) -> None:
    transport.write32(register, (transport.read32(register) & ~clear) | set_)


def _poll32_clear(transport: RTL8192FUTransport, register: int, mask: int) -> None:
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if not transport.read32(register) & mask:
            return
        time.sleep(0.00001)
    raise IOError(f"register {register:#06x} did not clear {mask:#x}")


def _poll32_set(transport: RTL8192FUTransport, register: int, mask: int) -> None:
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if transport.read32(register) & mask:
            return
        time.sleep(0.00001)
    raise IOError(f"register {register:#06x} did not set {mask:#x}")


def power_on(transport: RTL8192FUTransport) -> None:
    transport.write8(C.REG_USB_ACCESS_TIMEOUT, 0x80)
    _update16(transport, C.REG_APS_FSMCO, clear=(1 << 15) | (1 << 11))
    _update32(transport, C.REG_GPIO_INTM, clear=1 << 16)
    _update16(transport, C.REG_APS_FSMCO, clear=(1 << 12) | (1 << 11))

    _update8(transport, C.REG_LDOA15_CTRL, set_=1)
    _update32(transport, C.REG_PAD_CTRL1, clear=1 << 28)
    time.sleep(0.001)
    _update8(transport, C.REG_SYS_ISO_CTRL, clear=1 << 5)
    _update16(transport, C.REG_APS_FSMCO, clear=(1 << 12) | (1 << 11) | (1 << 10))
    _poll32_set(transport, C.REG_APS_FSMCO, 1 << 17)
    _update32(transport, C.REG_APS_FSMCO, set_=1 << 16)
    _poll32_clear(transport, C.REG_APS_FSMCO, (1 << 8) | (1 << 9))
    _update32(transport, C.REG_AFE_MISC, set_=1 << 18)
    _update16(transport, C.REG_APS_FSMCO, clear=1 << 15)
    _update16(transport, C.REG_APS_FSMCO, clear=(1 << 12) | (1 << 11))
    _update32(transport, C.REG_LDO_SW_CTRL, set_=1 << 31)
    _update16(transport, C.REG_APS_FSMCO, set_=1 << 8)
    _poll32_clear(transport, C.REG_APS_FSMCO, 1 << 8)
    _update8(transport, C.REG_AFE_MISC, set_=1 << 6)
    _update16(transport, C.REG_GPIO_INTM, set_=1 << 9)
    _update16(transport, C.REG_GPIO_IO_SEL_2, clear=1 << 9)
    _update16(transport, C.REG_GPIO_IO_SEL_2, clear=1 << 1)
    _update8(transport, C.REG_HSIMR, set_=1)
    _update8(transport, C.REG_MULTI_FUNC_CTRL, clear=1 << 3)
    _update8(transport, C.REG_RSV_CTRL, set_=1 << 7)
    _update16(transport, C.REG_MULTI_FUNC_CTRL, set_=1 << 14)
    transport.write8(C.REG_RF_CTRL, 0)
    transport.write8(C.REG_AFE_CTRL4 + 3, 0)
    transport.write8(C.REG_RF_CTRL, 7)
    transport.write8(C.REG_AFE_CTRL4 + 3, 7)
    _update8(transport, C.REG_RSVD_1, set_=1 << 5)
    transport.write8(C.REG_RSVD_4, 0xCC)
    _update8(transport, C.REG_AFE_XTAL_CTRL, clear=(1 << 4) | (1 << 3))
    transport.write32(C.REG_GPIO_A0, 0xFFFFFFFF)
    transport.write8(C.REG_GPIO_B0, 0xFF)
    _update8(transport, C.REG_RSV_CTRL, clear=1 << 7)

    transport.write16(C.REG_CR, 0)
    _update16(transport, C.REG_CR, set_=0x063F)
