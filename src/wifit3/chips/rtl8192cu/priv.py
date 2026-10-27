"""The slices of struct rtl_priv / struct rtl_usb (wifi.h, usb.h) the rtl8192cu port reads and writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from . import constants as C


class RegisterIo(Protocol):
    """rtlpriv->io: the synchronous register accessors usb.c installs."""
    def read8(self, addr: int) -> int: ...
    def read16(self, addr: int) -> int: ...
    def read32(self, addr: int) -> int: ...
    def write8(self, addr: int, value: int) -> None: ...
    def write16(self, addr: int, value: int) -> None: ...
    def write32(self, addr: int, value: int) -> None: ...


def _rf_path_rows(width: int) -> list[list[int]]:
    return [[0] * width for _ in range(C.MAX_RF_PATH)]


@dataclass
class RtlHal:
    version: int = C.VERSION_UNKNOWN
    hw_type: int = C.HARDWARE_TYPE_RTL8192E
    oem_id: int = C.RT_CID_DEFAULT
    fw_ready: bool = False
    fw_name: str = ""


@dataclass
class RtlPhy:
    rf_type: int = C.RF_1T1R


@dataclass
class RtlDm:
    rfpath_rxenable: list[bool] = field(default_factory=lambda: [False] * 4)
    dm_initialgain_enable: bool = False
    dm_flag: int = 0
    disable_framebursting: bool = False
    thermalvalue: int = 0


@dataclass
class RtlLedCtl:
    led_opendrain: bool = False


@dataclass
class RtlEfuse:
    autoload_failflag: bool = False
    epromtype: int = C.EEPROM_93C46
    efuse_map: list[bytearray] = field(
        default_factory=lambda: [bytearray(C.EFUSE_MAX_LOGICAL_SIZE), bytearray(C.EFUSE_MAX_LOGICAL_SIZE)])
    efuse_usedbytes: int = 0
    efuse_usedpercentage: int = 0
    eeprom_vid: int = 0
    eeprom_did: int = 0
    eeprom_svid: int = 0
    eeprom_smid: int = 0
    eeprom_oemid: int = 0
    eeprom_channelplan: int = 0
    eeprom_version: int = 0
    channel_plan: int = 0
    board_type: int = 0
    external_pa: int = 0
    dev_addr: bytes = bytes(6)
    txpwr_fromeprom: bool = False
    eeprom_tssi: list[int] = field(default_factory=lambda: [0, 0])
    eeprom_pwrlimit_ht20: list[int] = field(default_factory=lambda: [0] * C.CHANNEL_GROUP_MAX)
    eeprom_pwrlimit_ht40: list[int] = field(default_factory=lambda: [0] * C.CHANNEL_GROUP_MAX)
    eeprom_chnlarea_txpwr_cck: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_GROUP_MAX))
    eeprom_chnlarea_txpwr_ht40_1s: list[list[int]] = field(
        default_factory=lambda: _rf_path_rows(C.CHANNEL_GROUP_MAX))
    eprom_chnl_txpwr_ht40_2sdf: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_GROUP_MAX))
    pwrgroup_ht20: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    pwrgroup_ht40: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwrlevel_cck: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwrlevel_ht40_1s: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwrlevel_ht40_2s: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwr_ht20diff: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwr_legacyhtdiff: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    legacy_ht_txpowerdiff: int = 0
    eeprom_regulatory: int = 0
    eeprom_thermalmeter: int = 0
    thermalmeter: list[int] = field(default_factory=lambda: [0, 0])
    apk_thermalmeterignore: bool = False


@dataclass
class RtlUsb:
    in_ep: int = 0
    in_ep_nums: int = 0
    out_ep_nums: int = 0
    out_eps: list[int] = field(default_factory=lambda: [0] * C.RTL_USB_MAX_BULKOUT_NUM)
    out_queue_sel: int = 0
    ep_mapping: list[int] = field(default_factory=lambda: [0] * C.RTL_TXQ_NUM)
    max_bulk_out_size: int = 0
    rx_max_size: int = 0
    rx_urb_num: int = 0


@dataclass
class RtlPriv:
    io: RegisterIo
    rtlhal: RtlHal = field(default_factory=RtlHal)
    phy: RtlPhy = field(default_factory=RtlPhy)
    dm: RtlDm = field(default_factory=RtlDm)
    ledctl: RtlLedCtl = field(default_factory=RtlLedCtl)
    efuse: RtlEfuse = field(default_factory=RtlEfuse)
    usb: RtlUsb = field(default_factory=RtlUsb)
