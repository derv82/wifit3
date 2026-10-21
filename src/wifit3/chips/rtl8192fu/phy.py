import time

from . import constants as C
from .tables import _AGC_TABLE, _MAC_INIT_TABLE, PHY_INIT_TABLE, RADIOA_INIT_TABLE, RADIOB_INIT_TABLE
from .transport import RTL8192FUTransport


_RF_LSSI = (0x0840, 0x0844)
_RF_SW_CTRL = (C.REG_FPGA0_XA_RF_SW_CTRL, C.REG_FPGA0_XB_RF_SW_CTRL)
_RF_INT_OE = (C.REG_FPGA0_XA_RF_INT_OE, C.REG_FPGA0_XB_RF_INT_OE)
_RF_HSSI_PARM1 = (0x0820, 0x0828)
_RF_HSSI_PARM2 = (C.REG_FPGA0_XA_HSSI_PARM2, C.REG_FPGA0_XB_HSSI_PARM2)
_RF_HSPI_READBACK = (0x08B8, 0x08BC)
_RF_LSSI_READBACK = (0x08A0, 0x08A4)


def read_rfreg(transport: RTL8192FUTransport, path: int, register: int) -> int:
    hssia = transport.read32(C.REG_FPGA0_XA_HSSI_PARM2)
    value = transport.read32(_RF_HSSI_PARM2[path]) if path else hssia
    value = (value & ~0x7F800000) | (register << 23) | (1 << 31)
    transport.write32(C.REG_FPGA0_XA_HSSI_PARM2, hssia & ~(1 << 31))
    time.sleep(0.00001)
    transport.write32(_RF_HSSI_PARM2[path], value)
    time.sleep(0.0001)
    transport.write32(C.REG_FPGA0_XA_HSSI_PARM2, hssia | (1 << 31))
    time.sleep(0.00001)
    hssi1 = transport.read32(_RF_HSSI_PARM1[path])
    readback = _RF_HSPI_READBACK[path] if hssi1 & (1 << 8) else _RF_LSSI_READBACK[path]
    return transport.read32(readback) & 0xFFFFF


def write_rfreg(transport: RTL8192FUTransport, path: int, register: int, value: int) -> None:
    transport.write32(_RF_LSSI[path], (register << 20) | (value & 0xFFFFF))
    time.sleep(0.000001)


def init_mac(transport: RTL8192FUTransport) -> None:
    for register, value in _MAC_INIT_TABLE:
        transport.write8(register, value)


def _apply_phy_table(transport: RTL8192FUTransport, table) -> None:
    for register, value in table:
        transport.write32(register, value)
        time.sleep(0.000001)


def init_phy_bb(transport: RTL8192FUTransport) -> None:
    value = transport.read16(C.REG_SYS_FUNC)
    transport.write16(C.REG_SYS_FUNC, value | C.SYS_FUNC_BBRSTB | C.SYS_FUNC_BB_GLB_RSTN)
    transport.write8(C.REG_RF_CTRL, 7)
    transport.write8(C.REG_LDOHCI12_CTRL, 0x0F)
    transport.write8(C.REG_SYS_SWR_CTRL2 + 1, 0xE9)
    _apply_phy_table(transport, PHY_INIT_TABLE)
    _apply_phy_table(transport, _AGC_TABLE)


def _init_rf_path(transport: RTL8192FUTransport, path: int, table) -> None:
    rfenv = transport.read16(_RF_SW_CTRL[path]) & (1 << 4)
    value = transport.read32(_RF_INT_OE[path])
    transport.write32(_RF_INT_OE[path], value | (1 << 20))
    time.sleep(0.000001)
    value = transport.read32(_RF_INT_OE[path])
    transport.write32(_RF_INT_OE[path], value | (1 << 4))
    time.sleep(0.000001)
    value = transport.read32(_RF_HSSI_PARM2[path])
    transport.write32(_RF_HSSI_PARM2[path], value & ~0x400)
    time.sleep(0.000001)
    value = transport.read32(_RF_HSSI_PARM2[path])
    transport.write32(_RF_HSSI_PARM2[path], value & ~0x800)
    time.sleep(0.000001)

    delays = {0xFE: 0.05, 0xFD: 0.005, 0xFC: 0.001, 0xFB: 0.00005, 0xFA: 0.000005, 0xF9: 0.000001}
    for register, value in table:
        if register in delays:
            time.sleep(delays[register])
            continue
        write_rfreg(transport, path, register, value)
        time.sleep(0.000001)

    value = transport.read16(_RF_SW_CTRL[path])
    transport.write16(_RF_SW_CTRL[path], (value & ~(1 << 4)) | rfenv)


def set_crystal_cap(transport: RTL8192FUTransport, crystal_cap: int) -> None:
    pll = transport.read32(0x0028)
    xtal = transport.read32(C.REG_AFE_XTAL_CTRL)
    pll = (pll & ~0x7E) | ((crystal_cap << 1) & 0x7E)
    xtal = (xtal & ~0x7E000000) | ((crystal_cap << 25) & 0x7E000000)
    transport.write32(0x0028, pll)
    transport.write32(C.REG_AFE_XTAL_CTRL, xtal)


def init_phy_rf(transport: RTL8192FUTransport) -> None:
    _init_rf_path(transport, 0, RADIOA_INIT_TABLE)
    _init_rf_path(transport, 1, RADIOB_INIT_TABLE)


def lc_calibrate(transport: RTL8192FUTransport) -> None:
    noise = transport.read32(0x0CE4)
    backup = noise & 0xC0000000
    transport.write32(0x0CE4, noise & ~0xC0000000)

    lstf = transport.read32(0x0D00)
    if lstf & 0x70000000:
        transport.write32(0x0D00, lstf & ~0x70000000)
    else:
        transport.write8(0x0522, 0xFF)
    rf_amode = read_rfreg(transport, 0, 0x18)
    write_rfreg(transport, 0, 0x18, rf_amode | 0x08000)
    for _ in range(100):
        if not read_rfreg(transport, 0, 0x18) & 0x08000:
            break
        time.sleep(0.01)
    write_rfreg(transport, 0, 0x18, rf_amode)
    if lstf & 0x70000000:
        transport.write32(0x0D00, lstf)
    else:
        transport.write8(0x0522, 0)

    noise = transport.read32(0x0CE4)
    transport.write32(0x0CE4, (noise & ~0xC0000000) | backup)
    mode = transport.read32(C.REG_FPGA0_RF_MODE)
    transport.write32(C.REG_FPGA0_RF_MODE, mode & ~(1 << 25))
    mode = transport.read32(C.REG_FPGA0_RF_MODE)
    transport.write32(C.REG_FPGA0_RF_MODE, mode | (1 << 25))
