from . import constants as C
from .efuse import _read_efuse8
from .phy import read_rfreg, write_rfreg
from .transport import RTL8192FUTransport


def _write32_mask(transport: RTL8192FUTransport, register: int, mask: int, value: int) -> None:
    shift = (mask & -mask).bit_length() - 1
    current = transport.read32(register)
    transport.write32(register, (current & ~mask) | ((value << shift) & mask))


def _write_rfreg_mask(
    transport: RTL8192FUTransport,
    path: int,
    register: int,
    mask: int,
    value: int,
) -> None:
    shift = (mask & -mask).bit_length() - 1
    current = read_rfreg(transport, path, register)
    write_rfreg(transport, path, register, (current & ~mask) | ((value << shift) & mask))


def _revise_cck_tx_psf(transport: RTL8192FUTransport, channel: int) -> None:
    if channel == 13:
        values = (0xF8FE0001, 0x64B80C1C, 0x8810, 0x01235667)
    elif channel == 14:
        values = (0xE82C0001, 0x0000B81C, 0x0000, 0x00003667)
    else:
        values = (0xE82C0001, 0x64B80C1C, 0x8810, 0x01235667)
    transport.write32(0x0A20, values[0])
    transport.write32(0x0A24, values[1])
    transport.write16(0x0A28, values[2])
    transport.write32(0x0AAC, values[3])


def _config_kfree(transport: RTL8192FUTransport, channel: int) -> None:
    gains = [0xFF, 0xFF, 0xFF]
    gains[1] = _read_efuse8(transport, 0x1EE)
    gains[0] = _read_efuse8(transport, 0x1EC)
    gains[2] = _read_efuse8(transport, 0x1EA)
    if gains[1] == 0xFF:
        return
    if gains[0] == 0xFF:
        gains[0] = gains[1]
    if gains[2] == 0xFF:
        gains[2] = gains[1]
    channel_index = 0 if channel <= 3 else 1 if channel <= 9 else 2

    for path, path_mask in enumerate((0x0F, 0xF0)):
        _write_rfreg_mask(transport, path, 0x55, 1 << 5, 1)
        _write_rfreg_mask(transport, path, 0xF5, 1 << 18, 1)
        _write_rfreg_mask(transport, path, 0xDF, 1 << 7, 1)
        _write_rfreg_mask(transport, path, 0xEF, 1 << 7, 1)
        gain = (gains[channel_index] & path_mask) >> (0 if path == 0 else 4)
        _write_rfreg_mask(transport, path, 0x33, 0x70000, channel_index * 2)
        _write_rfreg_mask(transport, path, 0x33, 0x3F, gain)
        _write_rfreg_mask(transport, path, 0x33, 0x70000, channel_index * 2 + 1)
        _write_rfreg_mask(transport, path, 0x33, 0x3F, gain)
        _write_rfreg_mask(transport, path, 0xDF, 1 << 7, 0)
        _write_rfreg_mask(transport, path, 0xEF, 1 << 7, 0)


def config_channel(transport: RTL8192FUTransport, channel: int) -> None:
    value = read_rfreg(transport, 0, 0x18)
    _revise_cck_tx_psf(transport, channel)
    value = (value & ~((1 << 18) | (1 << 17) | 0xFF)) | channel
    write_rfreg(transport, 0, 0x18, value)
    write_rfreg(transport, 1, 0x18, value)
    _config_kfree(transport, channel)
    transport.write8(0x0483, 0)

    value = transport.read32(0x0CE4)
    transport.write32(0x0CE4, value & ~0xC0000000)
    _write32_mask(transport, C.REG_FPGA0_RF_MODE, 1 << 0, 0)
    _write32_mask(transport, 0x0900, 1 << 0, 0)
    _write32_mask(transport, C.REG_FPGA0_RF_MODE, 0x700, 4)
    _write32_mask(transport, C.REG_FPGA0_RF_MODE, 0x3000, 2)
    _write32_mask(transport, 0x0CA4, 0x0C000000, 2)

    value = transport.read32(C.REG_FPGA0_RF_MODE)
    transport.write32(C.REG_FPGA0_RF_MODE, value | (1 << 24))
    value = read_rfreg(transport, 0, 0x18)
    value = (value & ~0xC00) | 0xC00
    write_rfreg(transport, 0, 0x18, value)
    write_rfreg(transport, 1, 0x18, value)
    _write32_mask(transport, 0x0E24, 0x00300000, 2)
    _write32_mask(transport, 0x0C10, 0x30000000, 2)
    _write32_mask(transport, 0x0948, 0x1FF, 0x1A3)
