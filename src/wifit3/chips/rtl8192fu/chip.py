from dataclasses import dataclass

from . import constants as C
from .transport import RTL8192FUTransport


@dataclass(frozen=True)
class ChipIdentity:
    chip_cut: int
    vendor: str
    rom_rev: int
    has_wifi: bool
    has_bluetooth: bool
    has_gps: bool
    tx_queue_count: int


def identify_chip(transport: RTL8192FUTransport) -> ChipIdentity:
    sys_cfg = transport.read32(C.REG_SYS_CFG)
    chip_cut = (sys_cfg & C.SYS_CFG_CHIP_VERSION_MASK) >> 12
    if sys_cfg & C.SYS_CFG_TRP_VAUX_EN:
        raise OSError("unsupported RTL8192FU test chip")

    multi_func = transport.read32(C.REG_MULTI_FUNC_CTRL)
    gpio_outsts = transport.read32(C.REG_GPIO_OUTSTS)
    endpoints = transport.endpoints
    if endpoints is None:
        raise RuntimeError("USB endpoints have not been parsed")

    return ChipIdentity(
        chip_cut=chip_cut,
        vendor="UMC" if sys_cfg & C.SYS_CFG_VENDOR_ID else "TSMC",
        rom_rev=(gpio_outsts & C.GPIO_RF_RL_ID) >> 28,
        has_wifi=bool(multi_func & C.MULTI_WIFI_FUNC_EN),
        has_bluetooth=bool(multi_func & C.MULTI_BT_FUNC_EN),
        has_gps=bool(multi_func & C.MULTI_GPS_FUNC_EN),
        tx_queue_count=min(len(endpoints.bulk_out), 3),
    )
