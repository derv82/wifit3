from __future__ import annotations

import time
from dataclasses import dataclass

from . import constants as C
from .efuse import EfuseData
from .firmware import FirmwareImage, download_firmware, start_firmware
from .phy import init_mac, init_phy_bb, init_phy_rf, lc_calibrate, set_crystal_cap, write_rfreg
from .power import power_on
from .transport import RTL8192FUTransport


@dataclass(frozen=True)
class HardwareFeature:
    max_bandwidth: int


def init_queues(transport: RTL8192FUTransport) -> None:
    endpoints = transport.endpoints
    if endpoints is None:
        raise RuntimeError("USB endpoints have not been parsed")
    count = min(len(endpoints.bulk_out), 3)
    high = 0x08 if count >= 1 else 0
    normal = 0x08 if count >= 2 else 0
    low = 0x08 if count >= 3 else 0
    transport.write32(C.REG_RQPN_NPQ, normal)
    public = 0xF7 - high - normal - low - 1
    transport.write32(C.REG_RQPN, (1 << 31) | high | (low << 8) | (public << 16))

    if count == 3:
        voq, viq, beq, bkq, mgq, hiq = 3, 2, 1, 1, 3, 3
    elif count == 2:
        voq = viq = mgq = hiq = 3
        beq = bkq = 2
    elif count == 1:
        voq = viq = beq = bkq = mgq = hiq = 3
    else:
        raise ValueError("RTL8192FU has no bulk-OUT endpoints")
    value = transport.read32(C.REG_TRXDMA_CTRL) & 0x07
    value |= voq << 4 | viq << 7 | beq << 10 | bkq << 13 | mgq << 16 | hiq << 19
    transport.write32(C.REG_TRXDMA_CTRL, value)
    transport.write16(C.REG_TRXFF_BNDY + 2, 0x3F3F)


def request_hw_feature(transport: RTL8192FUTransport) -> None:
    transport.write8(0x01A0, 0xFD)


def read_hw_feature(transport: RTL8192FUTransport) -> HardwareFeature:
    for _ in range(80):
        if transport.read8(0x01A0) == 0x19:
            break
        time.sleep(0.01)
    else:
        raise IOError("RTL8192FU hardware-feature report timed out")
    feature = bytes(transport.read8(0x01A2 + index) for index in range(13))
    transport.write8(0x01A0, 0)
    bandwidths = (0, 0, 160, 5, 10, 20, 40, 80)
    return HardwareFeature(max_bandwidth=bandwidths[feature[6] & 0x07])


def firmware_bringup(transport: RTL8192FUTransport, firmware: FirmwareImage) -> HardwareFeature:
    transport.read8(C.REG_CR)
    transport.read16(C.REG_SYS_CLKR)
    power_on(transport)
    init_queues(transport)
    request_hw_feature(transport)
    download_firmware(transport, firmware)
    start_firmware(transport)
    return read_hw_feature(transport)


def table_bringup(transport: RTL8192FUTransport, efuse: EfuseData) -> None:
    init_mac(transport)
    init_phy_bb(transport)
    set_crystal_cap(transport, efuse.default_crystal_cap)
    init_phy_rf(transport)
    value = transport.read16(C.REG_AFE_CTRL4)
    transport.write16(C.REG_AFE_CTRL4, value | (1 << 4) | (1 << 15))
    rf_switch = (1 << 5) | (1 << 6) | (1 << 8) | (1 << 9)
    rf_switch |= ((1 << 8) | (1 << 9)) << 16
    rf_switch |= (1 << 10) | (1 << 26)
    transport.write32(C.REG_FPGA0_XAB_RF_SW_CTRL, rf_switch)


def fifo_and_rx_policy(transport: RTL8192FUTransport) -> None:
    boundary = 0xF8
    transport.write8(C.REG_TXPKTBUF_BCNQ_BDNY, boundary)
    transport.write8(C.REG_TXPKTBUF_MGQ_BDNY, boundary)
    transport.write8(C.REG_TXPKTBUF_WMAC_LBK_BF_HD, boundary)
    transport.write8(C.REG_TRXFF_BNDY, boundary)
    transport.write8(C.REG_TDECTRL + 1, boundary)
    transport.write8(C.REG_PBP, 0x22)

    value = transport.read32(C.REG_AUTO_LLT)
    transport.write32(C.REG_AUTO_LLT, value | (1 << 16))
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if not transport.read32(C.REG_AUTO_LLT) & (1 << 16):
            break
        time.sleep(0.000002)
    else:
        raise IOError("RTL8192FU auto-LLT timed out")

    value = transport.read32(C.REG_TXDMA_OFFSET_CHK)
    transport.write32(C.REG_TXDMA_OFFSET_CHK, value | (1 << 9))
    value = transport.read16(C.REG_CR)
    transport.write16(C.REG_CR, value | (1 << 6) | (1 << 7))

    value = transport.read8(C.REG_TX_REPORT_CTRL)
    transport.write8(C.REG_TX_REPORT_CTRL, value | (1 << 1))
    transport.write8(C.REG_TX_REPORT_CTRL + 1, 0x02)
    transport.write16(C.REG_TX_REPORT_TIME, 0xCDF0)
    value = transport.read8(0x00A3)
    transport.write8(0x00A3, value & 0xF8)
    transport.write8(C.REG_EARLY_MODE_CONTROL_8710B, 0)
    transport.write8(C.REG_RX_DRVINFO_SZ, 4)

    transport.write32(C.REG_RCR, 0x7000600E)
    transport.write16(C.REG_RXFLTMAP2, 0xFFFF)
    transport.write16(C.REG_RXFLTMAP1, 0x0400)
    transport.write16(C.REG_RXFLTMAP0, 0xFFFF)


def _write32_mask(transport: RTL8192FUTransport, register: int, mask: int, value: int) -> None:
    shift = (mask & -mask).bit_length() - 1
    current = transport.read32(register)
    transport.write32(register, (current & ~mask) | ((value << shift) & mask))


def set_tx_power(transport: RTL8192FUTransport, efuse: EfuseData, channel: int = 1) -> None:
    def repeated(value: int) -> int:
        value &= 0xFF
        return value | value << 8 | value << 16 | value << 24

    if channel < 3:
        group = 0
    elif channel < 6:
        group = 1
    elif channel < 9:
        group = 2
    elif channel < 12:
        group = 3
    else:
        group = 4
    cck_group = 5 if channel == 14 else group

    cck = efuse.path_a.cck_base[cck_group]
    _write32_mask(transport, 0x0E08, 0x00007F00, cck)
    _write32_mask(transport, 0x086C, 0x7F7F7F00, cck | cck << 8 | cck << 16)
    ofdm = repeated(efuse.path_a.ht40_base[group] + efuse.path_a.ofdm_diff[0])
    _write32_mask(transport, 0x0E00, 0x7F7F7F7F, ofdm)
    _write32_mask(transport, 0x0E04, 0x7F7F7F7F, ofdm)
    mcs = repeated(efuse.path_a.ht40_base[group] + efuse.path_a.ht20_diff[0])
    for register in (0x0E10, 0x0E14, 0x0E18, 0x0E1C):
        _write32_mask(transport, register, 0x7F7F7F7F, mcs)

    cck = efuse.path_b.cck_base[cck_group]
    _write32_mask(transport, 0x0838, 0x7F7F7F00, cck | cck << 8 | cck << 16)
    _write32_mask(transport, 0x086C, 0x0000007F, cck)
    ofdm = repeated(efuse.path_b.ht40_base[group] + efuse.path_b.ofdm_diff[1])
    _write32_mask(transport, 0x0830, 0x7F7F7F7F, ofdm)
    _write32_mask(transport, 0x0834, 0x7F7F7F7F, ofdm)
    mcs = repeated(efuse.path_b.ht40_base[group] + efuse.path_b.ht20_diff[1])
    for register in (0x083C, 0x0848, 0x084C, 0x0868):
        _write32_mask(transport, register, 0x7F7F7F7F, mcs)


def calibrate_lc(transport: RTL8192FUTransport) -> None:
    value = transport.read8(0x004E)
    transport.write8(0x004E, value | (1 << 7))
    transport.write8(0x0423, 0xFF)
    transport.write32(0x04CC, 0x0201FFFF)
    value = transport.read8(0x0040)
    transport.write8(0x0040, value & ~(1 << 5))
    lc_calibrate(transport)


def finish_cold_init(transport: RTL8192FUTransport) -> None:
    write_rfreg(transport, 0, 0x42, 0x37CF8)
    transport.write8(0x0652, (30000 + 127) // 128)
    value = transport.read32(C.REG_FWHW_TXQ_CTRL)
    transport.write32(C.REG_FWHW_TXQ_CTRL, value | (1 << 12))
    transport.read32(0x0A9C)
    transport.read32(0x0D2C)


def mac_policy(transport: RTL8192FUTransport) -> None:
    value = transport.read32(0x0440)
    transport.write32(0x0440, (value & ~0x000FFFFF) | 0x000FFFF1)
    transport.write16(0x0428, 0x1010)
    transport.write16(0x042A, 0x3030)
    transport.write16(0x0428, 0x100A)
    transport.write16(0x063A, 0x100A)
    transport.write16(0x0514, 0x100A)
    transport.write16(0x0516, 0x100A)
    transport.write32(0x0508, 0x005EA42B)
    transport.write32(0x050C, 0x0000A44F)
    transport.write32(0x0504, 0x005EA324)
    transport.write32(0x0500, 0x002FA226)
    transport.write32(0x0430, 0)
    transport.write32(0x0434, 0x10080404)
    transport.write32(0x0438, 0x04030201)
    transport.write32(0x043C, 0x08070605)
    value = transport.read8(C.REG_FWHW_TXQ_CTRL)
    transport.write8(C.REG_FWHW_TXQ_CTRL, value | 0x80)
    transport.write8(0x0640, 0x40)
    transport.write16(0x0550, 0x1010)
    transport.write16(0x0540, 0x6404)
    transport.write8(0x0559, 0x02)
    transport.write16(0x0510, 0x660F)

    value = transport.read8(C.REG_RXDMA_PRO_8723B)
    value = (value & ~0x3C) | 0x1E
    transport.write8(C.REG_RXDMA_PRO_8723B, value)
    value = transport.read8(0x04C7)
    transport.write8(0x04C7, value | 0x80)
    transport.write16(0x04CA, 0x1F1F)
    transport.write8(0x0456, 0x5E)
    transport.write32(0x0458, 0xFFFFFFFF)
    transport.write8(0x060C, 0x18)
    transport.write8(0x0512, 0)
    transport.write8(C.REG_FWHW_TXQ_CTRL, 0x80)
    transport.write32(0x0460, 0x03086666)
    transport.write8(0x055C, 0x50)
    transport.write8(0x0638, 0x50)
    value = transport.read8(C.REG_RSV_CTRL)
    transport.write8(C.REG_RSV_CTRL, value | (1 << 5) | (1 << 6))

    trx = transport.read8(C.REG_TRXDMA_CTRL) & ~(1 << 2)
    aggregation = transport.read32(C.REG_RXDMA_AGG_PG_TH)
    aggregation &= ~(1 << 31)
    aggregation &= ~0x0000FF0F
    transport.write8(C.REG_TRXDMA_CTRL, trx)
    transport.write32(C.REG_RXDMA_AGG_PG_TH, aggregation)

    transport.write16(0x04C0, 0x0400)
    transport.write16(0x04C2, 0x0400)
    value = transport.read32(C.REG_FPGA0_RF_MODE)
    transport.write32(C.REG_FPGA0_RF_MODE, value | (1 << 24) | (1 << 25))
    transport.write32(C.REG_CAM_CMD, 0xC0000000)
