from . import constants as C
from .phy import write_rfreg
from .transport import RTL8192FUTransport


def enable_hardware_led(transport: RTL8192FUTransport) -> None:
    transport.write32(0x1038, 0x00020080)
    transport.write32(0x103C, 0x001B0000)
    value = transport.read32(0x004C)
    value |= 1 << 21
    value &= ~(1 << 7 | 1 << 15)
    value = (value & ~0x00000707) | 0x00000303
    value &= ~((1 << 3) | (1 << 11))
    transport.write32(0x004C, value)


def set_tx_led(transport: RTL8192FUTransport, on: bool) -> None:
    """Set both source-defined LED pins through software control."""
    transport.write32(0x1038, 0x00020080)
    transport.write32(0x103C, 0x001B0000)
    value = transport.read32(0x004C)
    value |= 1 << 21
    value &= ~(1 << 7 | 1 << 15)
    value &= ~0x00000707
    if on:
        value |= (1 << 3) | (1 << 11)
    else:
        value &= ~((1 << 3) | (1 << 11))
    transport.write32(0x004C, value)


def enable_rf(transport: RTL8192FUTransport) -> None:
    transport.write8(C.REG_RF_CTRL, 7)
    value = transport.read32(0x0C04)
    value &= ~0x000000FF
    value |= 0x33
    transport.write32(0x0C04, value)
    transport.write8(0x0522, 0)


def open_rx_filters(transport: RTL8192FUTransport) -> None:
    transport.write16(C.REG_RXFLTMAP2, 0xFFFF)
    transport.write16(C.REG_RXFLTMAP0, 0xFFFF)
    value = transport.read32(0x0C50)
    transport.write32(0x0C50, (value & ~0x7F) | 0x1E)


def configure_monitor_filter(transport: RTL8192FUTransport) -> None:
    rcr = 0x7000600E | C.RCR_ACCEPT_AP | C.RCR_ACCEPT_PM | C.RCR_ACCEPT_CTRL_FRAME
    transport.write32(C.REG_RCR, rcr)


def disable_rf(transport: RTL8192FUTransport) -> None:
    value = transport.read32(0x0C04)
    transport.write32(0x0C04, value & ~0x0F)
    write_rfreg(transport, 0, 0x00, 0)
