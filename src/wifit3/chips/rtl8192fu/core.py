"""Port of rtl8xxxu core.c (v6.19.14): the functions on the RTL8192FU call graph, RTL8192F paths only."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Callable, Protocol

from . import constants as C


class Rtl8xxxuTransport(Protocol):
    def read8(self, address: int) -> int: ...
    def read16(self, address: int) -> int: ...
    def read32(self, address: int) -> int: ...
    def write8(self, address: int, value: int) -> None: ...
    def write16(self, address: int, value: int) -> None: ...
    def write32(self, address: int, value: int) -> None: ...
    def write_block(self, address: int, data: bytes) -> None: ...
    def bulk_write(self, endpoint: int, data: bytes) -> None: ...


class Rtl8xxxuRfpath(IntEnum):
    RF_A = 0
    RF_B = 1


RF_A = Rtl8xxxuRfpath.RF_A
RF_B = Rtl8xxxuRfpath.RF_B


class Nl80211Iftype(IntEnum):
    """include/uapi/linux/nl80211.h enum nl80211_iftype."""
    NL80211_IFTYPE_UNSPECIFIED = 0
    NL80211_IFTYPE_ADHOC = 1
    NL80211_IFTYPE_STATION = 2
    NL80211_IFTYPE_AP = 3
    NL80211_IFTYPE_MONITOR = 6


class Ieee80211FilterFlags(IntFlag):
    """include/net/mac80211.h enum ieee80211_filter_flags."""
    FIF_ALLMULTI = 1 << 1
    FIF_FCSFAIL = 1 << 2
    FIF_PLCPFAIL = 1 << 3
    FIF_BCN_PRBRESP_PROMISC = 1 << 4
    FIF_CONTROL = 1 << 5
    FIF_OTHER_BSS = 1 << 6
    FIF_PSPOLL = 1 << 7
    FIF_PROBE_REQ = 1 << 8


@dataclass
class Rtl8xxxuCfoTracking:
    adjust: bool = True
    atc_status: bool = False
    cfo_tail: list[int] = field(default_factory=lambda: [0, 0])
    cfo_cnt: list[int] = field(default_factory=lambda: [0, 0])
    crystal_cap: int = 0
    packet_count: int = 0
    packet_count_pre: int = 0


@dataclass
class Rtl8xxxuBtcoex:
    bt_status: int = 0
    bt_busy: bool = False
    has_sco: bool = False
    has_a2dp: bool = False
    has_hid: bool = False
    has_pan: bool = False
    hid_only: bool = False
    a2dp_only: bool = False
    c2h_bt_inquiry: bool = False


@dataclass(frozen=True)
class Rtl8xxxuFileops:
    identify_chip: Callable[["Rtl8xxxuPriv"], None]
    parse_efuse: Callable[["Rtl8xxxuPriv"], None]
    power_on: Callable[["Rtl8xxxuPriv"], None]
    power_off: Callable[["Rtl8xxxuPriv"], None]
    reset_8051: Callable[["Rtl8xxxuPriv"], None]
    init_phy_bb: Callable[["Rtl8xxxuPriv"], None]
    init_phy_rf: Callable[["Rtl8xxxuPriv"], None]
    phy_lc_calibrate: Callable[["Rtl8xxxuPriv"], None]
    phy_iq_calibrate: Callable[["Rtl8xxxuPriv"], None]
    config_channel: Callable[["Rtl8xxxuPriv", int], None]
    init_aggregation: Callable[["Rtl8xxxuPriv"], None]
    enable_rf: Callable[["Rtl8xxxuPriv"], None]
    disable_rf: Callable[["Rtl8xxxuPriv"], None]
    usb_quirks: Callable[["Rtl8xxxuPriv"], None]
    set_tx_power: Callable[["Rtl8xxxuPriv", int, bool], None]
    set_crystal_cap: Callable[["Rtl8xxxuPriv", int], None]
    led_classdev_brightness_set: Callable[["Rtl8xxxuPriv", int], None]
    mactable: tuple[tuple[int, int], ...]
    writeN_block_size: int
    trxff_boundary: int
    pbp_rx: int
    pbp_tx: int
    total_page_num: int
    page_num_hi: int
    page_num_lo: int
    page_num_norm: int
    ampdu_max_time: int
    ustime_tsf_edca: int
    max_aggr_num: int
    has_tx_report: bool
    gen2_thermal_meter: bool
    needs_full_init: bool
    init_reg_rxfltmap: bool
    init_reg_pkt_life_time: bool
    init_reg_hmtfr: bool


@dataclass
class Rtl8xxxuPriv:
    transport: Rtl8xxxuTransport
    fops: Rtl8xxxuFileops
    chip_name: str = ""
    chip_vendor: str = ""
    rf_paths: int = 0
    rx_paths: int = 0
    tx_paths: int = 0
    chip_cut: int = 0
    rom_rev: int = 0
    has_wifi: bool = False
    has_bluetooth: bool = False
    has_gps: bool = False
    is_multi_func: bool = False
    vendor_umc: bool = False
    has_eeprom: bool = False
    boot_eeprom: bool = False
    pipe_in: int = 0
    pipe_interrupt: int = 0
    out_ep: list[int] = field(default_factory=list)
    nr_out_eps: int = 0
    ep_tx_count: int = 0
    ep_tx_high_queue: bool = False
    ep_tx_normal_queue: bool = False
    ep_tx_low_queue: bool = False
    pipe_out: dict[int, int] = field(default_factory=dict)
    efuse_wifi: bytearray = field(default_factory=lambda: bytearray(b"\xff" * C.EFUSE_MAP_LEN))
    mac_addr: bytes = bytes(6)
    cck_tx_power_index_A: list[int] = field(default_factory=lambda: [0] * 6)
    cck_tx_power_index_B: list[int] = field(default_factory=lambda: [0] * 6)
    ht40_1s_tx_power_index_A: list[int] = field(default_factory=lambda: [0] * 5)
    ht40_1s_tx_power_index_B: list[int] = field(default_factory=lambda: [0] * 5)
    ofdm_tx_power_diff: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)] * 4)
    ht20_tx_power_diff: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)] * 4)
    ht40_tx_power_diff: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)] * 4)
    default_crystal_cap: int = 0
    rfe_type: int = 0
    fw_data: bytes = b""
    fw_size: int = 0
    regrcr: int = 0
    adda_backup: list[int] = field(default_factory=list)
    mac_backup: list[int] = field(default_factory=list)
    bb_backup: list[int] = field(default_factory=list)
    cck_new_agc: bool = False
    cfo_tracking: Rtl8xxxuCfoTracking = field(default_factory=Rtl8xxxuCfoTracking)
    vif_type: Nl80211Iftype | None = None
    vif_addr: bytes = bytes(6)
    is_associated: bool = False
    bss_bssid: bytes = bytes(6)
    c2hcmd_queue: list[bytes] = field(default_factory=list)
    bt_coex: Rtl8xxxuBtcoex = field(default_factory=Rtl8xxxuBtcoex)
    next_mbox: int = 0


RTL8XXXU_FIRMWARE_HEADER_SIZE = 32  # sizeof(struct rtl8xxxu_firmware_header), rtl8xxxu.h:908
LED_OFF = 0  # include/linux/leds.h enum led_brightness
LED_ON = 1


def _ffs(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def u32_get_bits(value: int, mask: int) -> int:
    return (value & mask) >> _ffs(mask)


def u32_replace_bits(value: int, new: int, mask: int) -> int:
    return (value & ~mask) | ((new << _ffs(mask)) & mask)


def rtl8xxxu_read8(priv: Rtl8xxxuPriv, addr: int) -> int:
    return priv.transport.read8(addr)


def rtl8xxxu_read16(priv: Rtl8xxxuPriv, addr: int) -> int:
    return priv.transport.read16(addr)


def rtl8xxxu_read32(priv: Rtl8xxxuPriv, addr: int) -> int:
    return priv.transport.read32(addr)


def rtl8xxxu_write8(priv: Rtl8xxxuPriv, addr: int, val: int) -> None:
    priv.transport.write8(addr, val & 0xFF)


def rtl8xxxu_write16(priv: Rtl8xxxuPriv, addr: int, val: int) -> None:
    priv.transport.write16(addr, val & 0xFFFF)


def rtl8xxxu_write32(priv: Rtl8xxxuPriv, addr: int, val: int) -> None:
    priv.transport.write32(addr, val & 0xFFFFFFFF)


def rtl8xxxu_write8_set(priv: Rtl8xxxuPriv, addr: int, bits: int) -> None:
    rtl8xxxu_write8(priv, addr, rtl8xxxu_read8(priv, addr) | bits)


def rtl8xxxu_write8_clear(priv: Rtl8xxxuPriv, addr: int, bits: int) -> None:
    rtl8xxxu_write8(priv, addr, rtl8xxxu_read8(priv, addr) & ~bits)


def rtl8xxxu_write16_set(priv: Rtl8xxxuPriv, addr: int, bits: int) -> None:
    rtl8xxxu_write16(priv, addr, rtl8xxxu_read16(priv, addr) | bits)


def rtl8xxxu_write16_clear(priv: Rtl8xxxuPriv, addr: int, bits: int) -> None:
    rtl8xxxu_write16(priv, addr, rtl8xxxu_read16(priv, addr) & ~bits)


def rtl8xxxu_write32_set(priv: Rtl8xxxuPriv, addr: int, bits: int) -> None:
    rtl8xxxu_write32(priv, addr, rtl8xxxu_read32(priv, addr) | bits)


def rtl8xxxu_write32_clear(priv: Rtl8xxxuPriv, addr: int, bits: int) -> None:
    rtl8xxxu_write32(priv, addr, rtl8xxxu_read32(priv, addr) & ~bits)


def rtl8xxxu_write32_mask(priv: Rtl8xxxuPriv, addr: int, mask: int, val: int) -> None:
    orig = rtl8xxxu_read32(priv, addr)
    rtl8xxxu_write32(priv, addr, (orig & ~mask) | ((val << _ffs(mask)) & mask))


def rtl8xxxu_write_rfreg_mask(priv: Rtl8xxxuPriv, path: int, reg: int, mask: int, val: int) -> None:
    orig = rtl8xxxu_read_rfreg(priv, path, reg)
    rtl8xxxu_write_rfreg(priv, path, reg, (orig & ~mask) | ((val << _ffs(mask)) & mask))


def rtl8xxxu_writeN(priv: Rtl8xxxuPriv, addr: int, buf: bytes) -> None:
    blocksize = priv.fops.writeN_block_size
    for offset in range(0, len(buf), blocksize):
        priv.transport.write_block(addr + offset, buf[offset:offset + blocksize])


@dataclass(frozen=True)
class Rtl8xxxuRfregs:
    hssiparm1: int
    hssiparm2: int
    lssiparm: int
    hspiread: int
    lssiread: int
    rf_sw_ctrl: int


rtl8xxxu_rfregs = (
    Rtl8xxxuRfregs(C.REG_FPGA0_XA_HSSI_PARM1, C.REG_FPGA0_XA_HSSI_PARM2, C.REG_FPGA0_XA_LSSI_PARM,
                   C.REG_HSPI_XA_READBACK, C.REG_FPGA0_XA_LSSI_READBACK, C.REG_FPGA0_XA_RF_SW_CTRL),
    Rtl8xxxuRfregs(C.REG_FPGA0_XB_HSSI_PARM1, C.REG_FPGA0_XB_HSSI_PARM2, C.REG_FPGA0_XB_LSSI_PARM,
                   C.REG_HSPI_XB_READBACK, C.REG_FPGA0_XB_LSSI_READBACK, C.REG_FPGA0_XB_RF_SW_CTRL),
)


def rtl8xxxu_read_rfreg(priv: Rtl8xxxuPriv, path: int, reg: int) -> int:
    hssia = rtl8xxxu_read32(priv, C.REG_FPGA0_XA_HSSI_PARM2)
    if path != RF_A:
        val32 = rtl8xxxu_read32(priv, rtl8xxxu_rfregs[path].hssiparm2)
    else:
        val32 = hssia

    val32 &= ~C.FPGA0_HSSI_PARM2_ADDR_MASK
    val32 |= reg << C.FPGA0_HSSI_PARM2_ADDR_SHIFT
    val32 |= C.FPGA0_HSSI_PARM2_EDGE_READ
    hssia &= ~C.FPGA0_HSSI_PARM2_EDGE_READ
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM2, hssia)
    time.sleep(10e-6)

    rtl8xxxu_write32(priv, rtl8xxxu_rfregs[path].hssiparm2, val32)
    time.sleep(100e-6)

    hssia |= C.FPGA0_HSSI_PARM2_EDGE_READ
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM2, hssia)
    time.sleep(10e-6)

    val32 = rtl8xxxu_read32(priv, rtl8xxxu_rfregs[path].hssiparm1)
    if val32 & C.FPGA0_HSSI_PARM1_PI:
        retval = rtl8xxxu_read32(priv, rtl8xxxu_rfregs[path].hspiread)
    else:
        retval = rtl8xxxu_read32(priv, rtl8xxxu_rfregs[path].lssiread)
    return retval & 0xFFFFF


def rtl8xxxu_write_rfreg(priv: Rtl8xxxuPriv, path: int, reg: int, data: int) -> None:
    data &= C.FPGA0_LSSI_PARM_DATA_MASK
    dataaddr = (reg << C.FPGA0_LSSI_PARM_ADDR_SHIFT) | data
    rtl8xxxu_write32(priv, rtl8xxxu_rfregs[path].lssiparm, dataaddr)
    time.sleep(1e-6)


def rtl8xxxu_gen2_h2c_cmd(priv: Rtl8xxxuPriv, h2c: bytes) -> bool:
    """False where the C returns -EBUSY (mailbox busy)."""
    mbox_nr = priv.next_mbox
    mbox_reg = C.REG_HMBOX_0 + (mbox_nr * 4)
    mbox_ext_reg = C.REG_HMBOX_EXT0_8723B + (mbox_nr * 4)

    retry = 100
    while True:
        if not rtl8xxxu_read8(priv, C.REG_HMTFR) & C.BIT(mbox_nr):
            break
        expired = retry == 0
        retry -= 1
        if expired:
            break
    if retry == 0:  # sic: core.c:1023 tests !retry after do { } while (retry--)
        return False

    padded = h2c.ljust(8, b"\x00")
    if len(h2c) > 4:
        rtl8xxxu_write32(priv, mbox_ext_reg, int.from_bytes(padded[4:8], "little"))
    rtl8xxxu_write32(priv, mbox_reg, int.from_bytes(padded[0:4], "little"))

    priv.next_mbox = (mbox_nr + 1) % C.H2C_MAX_MBOX
    return True


def rtl8xxxu_set_linktype(priv: Rtl8xxxuPriv, linktype: Nl80211Iftype, port_num: int) -> None:
    if linktype == Nl80211Iftype.NL80211_IFTYPE_UNSPECIFIED:
        link = C.MSR_LINKTYPE_NONE
    elif linktype == Nl80211Iftype.NL80211_IFTYPE_ADHOC:
        link = C.MSR_LINKTYPE_ADHOC
    elif linktype == Nl80211Iftype.NL80211_IFTYPE_STATION:
        link = C.MSR_LINKTYPE_STATION
    elif linktype == Nl80211Iftype.NL80211_IFTYPE_AP:
        link = C.MSR_LINKTYPE_AP
    else:
        return

    if port_num == 0:
        val8 = (rtl8xxxu_read8(priv, C.REG_MSR) & 0x0C) | link
    elif port_num == 1:
        val8 = (rtl8xxxu_read8(priv, C.REG_MSR) & 0x03) | link << 2
    else:
        return
    rtl8xxxu_write8(priv, C.REG_MSR, val8)


def rtl8xxxu_set_retry(priv: Rtl8xxxuPriv, short_retry: int, long_retry: int) -> None:
    val16 = ((short_retry << C.RETRY_LIMIT_SHORT_SHIFT) & C.RETRY_LIMIT_SHORT_MASK) | \
        ((long_retry << C.RETRY_LIMIT_LONG_SHIFT) & C.RETRY_LIMIT_LONG_MASK)
    rtl8xxxu_write16(priv, C.REG_RETRY_LIMIT, val16)


def rtl8xxxu_set_spec_sifs(priv: Rtl8xxxuPriv, cck: int, ofdm: int) -> None:
    val16 = ((cck << C.SPEC_SIFS_CCK_SHIFT) & C.SPEC_SIFS_CCK_MASK) | \
        ((ofdm << C.SPEC_SIFS_OFDM_SHIFT) & C.SPEC_SIFS_OFDM_MASK)
    rtl8xxxu_write16(priv, C.REG_SPEC_SIFS, val16)


def rtl8xxxu_identify_vendor_1bit(priv: Rtl8xxxuPriv, vendor: int) -> None:
    if vendor:
        priv.chip_vendor = "UMC"
        priv.vendor_umc = True
    else:
        priv.chip_vendor = "TSMC"


def rtl8xxxu_config_endpoints_no_sie(priv: Rtl8xxxuPriv) -> None:
    if not 1 <= priv.nr_out_eps <= 6:
        raise OSError("Unsupported USB TX end-points")
    if priv.nr_out_eps >= 3:
        priv.ep_tx_low_queue = True
        priv.ep_tx_count += 1
    if priv.nr_out_eps >= 2:
        priv.ep_tx_normal_queue = True
        priv.ep_tx_count += 1
    priv.ep_tx_high_queue = True
    priv.ep_tx_count += 1


def rtl8xxxu_read_efuse8(priv: Rtl8xxxuPriv, offset: int) -> int | None:
    """None where the C returns -EIO (poll timeout)."""
    rtl8xxxu_write8(priv, C.REG_EFUSE_CTRL + 1, offset & 0xFF)
    val8 = rtl8xxxu_read8(priv, C.REG_EFUSE_CTRL + 2)
    val8 &= 0xFC
    val8 |= (offset >> 8) & 0x03
    rtl8xxxu_write8(priv, C.REG_EFUSE_CTRL + 2, val8)

    val8 = rtl8xxxu_read8(priv, C.REG_EFUSE_CTRL + 3)
    rtl8xxxu_write8(priv, C.REG_EFUSE_CTRL + 3, val8 & 0x7F)

    rtl8xxxu_read32(priv, C.REG_EFUSE_CTRL)
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if rtl8xxxu_read32(priv, C.REG_EFUSE_CTRL) & C.BIT(31):
            break
    else:
        return None

    time.sleep(50e-6)
    return rtl8xxxu_read32(priv, C.REG_EFUSE_CTRL) & 0xFF


def rtl8xxxu_read_efuse(priv: Rtl8xxxuPriv) -> None:
    val16 = rtl8xxxu_read16(priv, C.REG_9346CR)
    if val16 & C.EEPROM_ENABLE:
        priv.has_eeprom = True
    if val16 & C.EEPROM_BOOT:
        priv.boot_eeprom = True

    if priv.is_multi_func:
        val32 = rtl8xxxu_read32(priv, C.REG_EFUSE_TEST)
        val32 = (val32 & ~C.EFUSE_SELECT_MASK) | C.EFUSE_WIFI_SELECT
        rtl8xxxu_write32(priv, C.REG_EFUSE_TEST, val32)

    rtl8xxxu_write8(priv, C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_ENABLE)
    try:
        val16 = rtl8xxxu_read16(priv, C.REG_SYS_ISO_CTRL)
        if not val16 & C.SYS_ISO_PWC_EV12V:
            rtl8xxxu_write16(priv, C.REG_SYS_ISO_CTRL, val16 | C.SYS_ISO_PWC_EV12V)
        val16 = rtl8xxxu_read16(priv, C.REG_SYS_FUNC)
        if not val16 & C.SYS_FUNC_ELDR:
            rtl8xxxu_write16(priv, C.REG_SYS_FUNC, val16 | C.SYS_FUNC_ELDR)
        val16 = rtl8xxxu_read16(priv, C.REG_SYS_CLKR)
        if not val16 & C.SYS_CLK_LOADER_ENABLE or not val16 & C.SYS_CLK_ANA8M:
            rtl8xxxu_write16(priv, C.REG_SYS_CLKR, val16 | C.SYS_CLK_LOADER_ENABLE | C.SYS_CLK_ANA8M)

        priv.efuse_wifi = bytearray(b"\xff" * C.EFUSE_MAP_LEN)
        efuse_addr = 0
        while efuse_addr < C.EFUSE_REAL_CONTENT_LEN_8723A:
            header = _read_efuse8_or_raise(priv, efuse_addr)
            efuse_addr += 1
            if header == 0xFF:
                return

            if (header & 0x1F) == 0x0F:
                offset = (header & 0xE0) >> 5
                extheader = _read_efuse8_or_raise(priv, efuse_addr)
                efuse_addr += 1
                if (extheader & 0x0F) == 0x0F:
                    continue
                offset |= (extheader & 0xF0) >> 1
                word_mask = extheader & 0x0F
            else:
                offset = (header >> 4) & 0x0F
                word_mask = header & 0x0F

            map_addr = offset * 8
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if word_mask & C.BIT(i):
                    map_addr += 2
                    continue
                val8 = _read_efuse8_or_raise(priv, efuse_addr)
                efuse_addr += 1
                if map_addr >= C.EFUSE_MAP_LEN - 1:
                    raise ValueError(f"Illegal map_addr ({map_addr:04x}), efuse corrupt!")
                priv.efuse_wifi[map_addr] = val8
                map_addr += 1
                priv.efuse_wifi[map_addr] = _read_efuse8_or_raise(priv, efuse_addr)
                efuse_addr += 1
                map_addr += 1
    finally:
        rtl8xxxu_write8(priv, C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_DISABLE)


def _read_efuse8_or_raise(priv: Rtl8xxxuPriv, offset: int) -> int:
    value = rtl8xxxu_read_efuse8(priv, offset)
    if value is None:
        raise OSError(f"EFUSE read timed out at offset {offset:#x}")
    return value


def rtl8xxxu_start_firmware(priv: Rtl8xxxuPriv) -> None:
    for _ in range(C.RTL8XXXU_FIRMWARE_POLL_MAX):
        if rtl8xxxu_read32(priv, C.REG_MCU_FW_DL) & C.MCU_FW_DL_CSUM_REPORT:
            break
    else:
        raise OSError("Firmware checksum poll timed out")

    val32 = rtl8xxxu_read32(priv, C.REG_MCU_FW_DL)
    val32 |= C.MCU_FW_DL_READY
    val32 &= ~C.MCU_WINT_INIT_READY
    rtl8xxxu_write32(priv, C.REG_MCU_FW_DL, val32)

    priv.fops.reset_8051(priv)

    for _ in range(C.RTL8XXXU_FIRMWARE_POLL_MAX):
        if rtl8xxxu_read32(priv, C.REG_MCU_FW_DL) & C.MCU_WINT_INIT_READY:
            break
        time.sleep(100e-6)
    else:
        raise OSError("Firmware failed to start")

    if priv.fops.init_reg_hmtfr:
        rtl8xxxu_write8(priv, C.REG_HMTFR, 0x0F)


def rtl8xxxu_download_firmware(priv: Rtl8xxxuPriv) -> None:
    reg_fw_start_address = C.REG_FW_START_ADDRESS_8192F
    reg_mcu_fw_dl = C.REG_MCU_FW_DL

    rtl8xxxu_write8(priv, C.REG_SYS_FUNC + 1, rtl8xxxu_read8(priv, C.REG_SYS_FUNC + 1) | 4)
    rtl8xxxu_write16(priv, C.REG_SYS_FUNC, rtl8xxxu_read16(priv, C.REG_SYS_FUNC) | C.SYS_FUNC_CPU_ENABLE)

    if rtl8xxxu_read8(priv, reg_mcu_fw_dl) & C.MCU_FW_RAM_SEL:
        rtl8xxxu_write8(priv, reg_mcu_fw_dl, 0x00)
        priv.fops.reset_8051(priv)

    rtl8xxxu_write8(priv, reg_mcu_fw_dl, rtl8xxxu_read8(priv, reg_mcu_fw_dl) | C.MCU_FW_DL_ENABLE)
    rtl8xxxu_write32(priv, reg_mcu_fw_dl, rtl8xxxu_read32(priv, reg_mcu_fw_dl) & ~C.BIT(19))
    rtl8xxxu_write8(priv, reg_mcu_fw_dl, rtl8xxxu_read8(priv, reg_mcu_fw_dl) | C.MCU_FW_DL_CSUM_REPORT)

    fwptr = priv.fw_data[RTL8XXXU_FIRMWARE_HEADER_SIZE:]
    try:
        for i, offset in enumerate(range(0, priv.fw_size, C.RTL_FW_PAGE_SIZE)):
            val8 = rtl8xxxu_read8(priv, reg_mcu_fw_dl + 2) & 0xF8
            rtl8xxxu_write8(priv, reg_mcu_fw_dl + 2, val8 | i)
            rtl8xxxu_writeN(priv, reg_fw_start_address, fwptr[offset:offset + C.RTL_FW_PAGE_SIZE])
    finally:
        val16 = rtl8xxxu_read16(priv, reg_mcu_fw_dl)
        rtl8xxxu_write16(priv, reg_mcu_fw_dl, val16 & ~C.MCU_FW_DL_ENABLE)


def rtl8xxxu_load_firmware(priv: Rtl8xxxuPriv, fw_data: bytes) -> None:
    priv.fw_data = fw_data
    priv.fw_size = len(fw_data) - RTL8XXXU_FIRMWARE_HEADER_SIZE
    signature = int.from_bytes(fw_data[0:2], "little")
    if signature & 0xFFF0 not in (0x92E0, 0x92C0, 0x88E0, 0x88C0, 0x5300, 0x2300, 0x88F0, 0x10B0, 0x92F0):
        raise ValueError(f"Invalid firmware signature: 0x{signature:04x}")


def rtl8xxxu_firmware_self_reset(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8(priv, C.REG_HMTFR + 3, 0x20)
    for _ in range(100):
        if not rtl8xxxu_read16(priv, C.REG_SYS_FUNC) & C.SYS_FUNC_CPU_ENABLE:
            return
        time.sleep(50e-6)
    val16 = rtl8xxxu_read16(priv, C.REG_SYS_FUNC)
    rtl8xxxu_write16(priv, C.REG_SYS_FUNC, val16 & ~C.SYS_FUNC_CPU_ENABLE)


def rtl8xxxu_init_mac(priv: Rtl8xxxuPriv) -> None:
    for reg, val in priv.fops.mactable:
        if reg == 0xFFFF and val == 0xFF:
            break
        rtl8xxxu_write8(priv, reg, val)


def rtl8xxxu_init_phy_regs(priv: Rtl8xxxuPriv, array: tuple[tuple[int, int], ...]) -> None:
    for reg, val in array:
        if reg == 0xFFFF and val == 0xFFFFFFFF:
            break
        rtl8xxxu_write32(priv, reg, val)
        time.sleep(1e-6)


def rtl8xxxu_init_phy_bb(priv: Rtl8xxxuPriv) -> None:
    priv.fops.init_phy_bb(priv)

    if priv.tx_paths == 1 and priv.rx_paths == 2:
        # TODO: verify, untested here, needs a 1T2R RTL8192FU (core.c:2316)
        val32 = rtl8xxxu_read32(priv, C.REG_FPGA0_TX_INFO)
        val32 &= ~(C.BIT(0) | C.BIT(1))
        val32 |= C.BIT(1)
        rtl8xxxu_write32(priv, C.REG_FPGA0_TX_INFO, val32)

        val32 = rtl8xxxu_read32(priv, C.REG_FPGA1_TX_INFO)
        val32 &= ~0x300033
        val32 |= 0x200022
        rtl8xxxu_write32(priv, C.REG_FPGA1_TX_INFO, val32)

        val32 = rtl8xxxu_read32(priv, C.REG_CCK0_AFE_SETTING)
        val32 &= ~C.CCK0_AFE_RX_MASK
        val32 &= 0x00FFFFFF
        val32 |= 0x40000000
        val32 |= C.CCK0_AFE_RX_ANT_B
        rtl8xxxu_write32(priv, C.REG_CCK0_AFE_SETTING, val32)

        val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_TRX_PATH_ENABLE)
        val32 &= ~(C.OFDM_RF_PATH_RX_MASK | C.OFDM_RF_PATH_TX_MASK)
        val32 |= C.OFDM_RF_PATH_RX_A | C.OFDM_RF_PATH_RX_B | C.OFDM_RF_PATH_TX_B
        rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, val32)

        val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_AGC_PARM1)
        val32 &= ~(C.BIT(4) | C.BIT(5))
        val32 |= C.BIT(4)
        rtl8xxxu_write32(priv, C.REG_OFDM0_AGC_PARM1, val32)

        for reg in (C.REG_TX_CCK_RFON, C.REG_TX_CCK_BBON, C.REG_TX_OFDM_RFON,
                    C.REG_TX_OFDM_BBON, C.REG_TX_TO_TX):
            val32 = rtl8xxxu_read32(priv, reg)
            val32 &= ~(C.BIT(27) | C.BIT(26))
            val32 |= C.BIT(27)
            rtl8xxxu_write32(priv, reg, val32)

    priv.fops.set_crystal_cap(priv, priv.default_crystal_cap)


def rtl8xxxu_init_rf_regs(priv: Rtl8xxxuPriv, array: tuple[tuple[int, int], ...], path: int) -> None:
    delays = {0xFE: 50e-3, 0xFD: 5e-3, 0xFC: 1e-3, 0xFB: 50e-6, 0xFA: 5e-6, 0xF9: 1e-6}
    for reg, val in array:
        if reg == 0xFF and val == 0xFFFFFFFF:
            break
        if reg in delays:
            time.sleep(delays[reg])
            continue
        rtl8xxxu_write_rfreg(priv, path, reg, val)
        time.sleep(1e-6)


def rtl8xxxu_init_phy_rf(priv: Rtl8xxxuPriv, table: tuple[tuple[int, int], ...], path: int) -> None:
    if path == RF_A:
        reg_sw_ctrl = C.REG_FPGA0_XA_RF_SW_CTRL
        reg_int_oe = C.REG_FPGA0_XA_RF_INT_OE
        reg_hssi_parm2 = C.REG_FPGA0_XA_HSSI_PARM2
    else:
        reg_sw_ctrl = C.REG_FPGA0_XB_RF_SW_CTRL
        reg_int_oe = C.REG_FPGA0_XB_RF_INT_OE
        reg_hssi_parm2 = C.REG_FPGA0_XB_HSSI_PARM2

    rfsi_rfenv = rtl8xxxu_read16(priv, reg_sw_ctrl) & C.FPGA0_RF_RFENV

    rtl8xxxu_write32(priv, reg_int_oe, rtl8xxxu_read32(priv, reg_int_oe) | C.BIT(20))
    time.sleep(1e-6)
    rtl8xxxu_write32(priv, reg_int_oe, rtl8xxxu_read32(priv, reg_int_oe) | C.BIT(4))
    time.sleep(1e-6)

    rtl8xxxu_write32(priv, reg_hssi_parm2, rtl8xxxu_read32(priv, reg_hssi_parm2) & ~C.FPGA0_HSSI_3WIRE_ADDR_LEN)
    time.sleep(1e-6)
    rtl8xxxu_write32(priv, reg_hssi_parm2, rtl8xxxu_read32(priv, reg_hssi_parm2) & ~C.FPGA0_HSSI_3WIRE_DATA_LEN)
    time.sleep(1e-6)

    rtl8xxxu_init_rf_regs(priv, table, path)

    val16 = rtl8xxxu_read16(priv, reg_sw_ctrl)
    val16 &= ~C.FPGA0_RF_RFENV
    val16 |= rfsi_rfenv
    rtl8xxxu_write16(priv, reg_sw_ctrl, val16)


def rtl8xxxu_auto_llt_table(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write32(priv, C.REG_AUTO_LLT, rtl8xxxu_read32(priv, C.REG_AUTO_LLT) | C.AUTO_LLT_INIT_LLT)
    for _ in range(500):
        if not rtl8xxxu_read32(priv, C.REG_AUTO_LLT) & C.AUTO_LLT_INIT_LLT:
            return
        time.sleep(2e-6)
    raise OSError("LLT table init failed")


def rtl8xxxu_init_queue_priority(priv: Rtl8xxxuPriv) -> None:
    if priv.ep_tx_count == 1:
        if priv.ep_tx_high_queue:
            hi = C.TRXDMA_QUEUE_HIGH
        elif priv.ep_tx_low_queue:
            hi = C.TRXDMA_QUEUE_LOW
        elif priv.ep_tx_normal_queue:
            hi = C.TRXDMA_QUEUE_NORMAL
        else:
            raise OSError("init_queue_priority: no TX queue")
        hiq = mgq = bkq = beq = viq = voq = hi
        hip = mgp = bkp = bep = vip = vop = 0
    elif priv.ep_tx_count == 2:
        if priv.ep_tx_high_queue and priv.ep_tx_low_queue:
            hi, lo = C.TRXDMA_QUEUE_HIGH, C.TRXDMA_QUEUE_LOW
        elif priv.ep_tx_normal_queue and priv.ep_tx_low_queue:
            hi, lo = C.TRXDMA_QUEUE_NORMAL, C.TRXDMA_QUEUE_LOW
        elif priv.ep_tx_high_queue and priv.ep_tx_normal_queue:
            hi, lo = C.TRXDMA_QUEUE_HIGH, C.TRXDMA_QUEUE_NORMAL
        else:
            raise OSError("init_queue_priority: invalid TX queue pair")
        hiq = mgq = viq = voq = hi
        bkq = beq = lo
        hip = mgp = vip = vop = 0
        bkp = bep = 1
    elif priv.ep_tx_count == 3:
        beq = C.TRXDMA_QUEUE_LOW
        bkq = C.TRXDMA_QUEUE_LOW
        viq = C.TRXDMA_QUEUE_NORMAL
        voq = C.TRXDMA_QUEUE_HIGH
        mgq = C.TRXDMA_QUEUE_HIGH
        hiq = C.TRXDMA_QUEUE_HIGH
        hip = hiq ^ 3
        mgp = mgq ^ 3
        bkp = bkq ^ 3
        bep = beq ^ 3
        vip = viq ^ 3
        vop = viq ^ 3  # sic: core.c:2661 uses viq for vop
    else:
        raise OSError(f"init_queue_priority: {priv.ep_tx_count} TX queues")

    val32 = rtl8xxxu_read32(priv, C.REG_TRXDMA_CTRL)
    val32 &= 0x7
    val32 |= (voq << C.TRXDMA_CTRL_VOQ_SHIFT_8192F) | \
        (viq << C.TRXDMA_CTRL_VIQ_SHIFT_8192F) | \
        (beq << C.TRXDMA_CTRL_BEQ_SHIFT_8192F) | \
        (bkq << C.TRXDMA_CTRL_BKQ_SHIFT_8192F) | \
        (mgq << C.TRXDMA_CTRL_MGQ_SHIFT_8192F) | \
        (hiq << C.TRXDMA_CTRL_HIQ_SHIFT_8192F)
    rtl8xxxu_write32(priv, C.REG_TRXDMA_CTRL, val32)

    priv.pipe_out = {
        C.TXDESC_QUEUE_VO: priv.out_ep[vop],
        C.TXDESC_QUEUE_VI: priv.out_ep[vip],
        C.TXDESC_QUEUE_BE: priv.out_ep[bep],
        C.TXDESC_QUEUE_BK: priv.out_ep[bkp],
        C.TXDESC_QUEUE_BEACON: priv.out_ep[0],
        C.TXDESC_QUEUE_MGNT: priv.out_ep[mgp],
        C.TXDESC_QUEUE_HIGH: priv.out_ep[hip],
        C.TXDESC_QUEUE_CMD: priv.out_ep[0],
    }


def rtl8xxxu_set_mac(priv: Rtl8xxxuPriv, port_num: int) -> None:
    reg = C.REG_MACID if port_num == 0 else C.REG_MACID1
    for i in range(6):
        rtl8xxxu_write8(priv, reg + i, priv.vif_addr[i])


def rtl8xxxu_set_bssid(priv: Rtl8xxxuPriv, bssid: bytes, port_num: int) -> None:
    reg = C.REG_BSSID if port_num == 0 else C.REG_BSSID1
    for i in range(6):
        rtl8xxxu_write8(priv, reg + i, bssid[i])


def rtl8xxxu_flush_fifo(priv: Rtl8xxxuPriv) -> bool:
    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0xFF)
    rtl8xxxu_write32(priv, C.REG_RXPKT_NUM, rtl8xxxu_read32(priv, C.REG_RXPKT_NUM) | C.RXPKT_NUM_RW_RELEASE_EN)

    idle = False
    for _ in range(101):
        if rtl8xxxu_read32(priv, C.REG_RXPKT_NUM) & C.RXPKT_NUM_RXDMA_IDLE:
            idle = True
            break

    rtl8xxxu_write16(priv, C.REG_RQPN_NPQ, 0)
    rtl8xxxu_write32(priv, C.REG_RQPN, 0x80000000)
    time.sleep(2e-3)
    return idle


def rtl8xxxu_gen2_usb_quirks(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write32_set(priv, C.REG_TXDMA_OFFSET_CHK, C.TXDMA_OFFSET_DROP_DATA_EN)


def rtl8xxxu_init_queue_reserved_page(priv: Rtl8xxxuPriv) -> None:
    fops = priv.fops
    hq = fops.page_num_hi if priv.ep_tx_high_queue else 0
    lq = fops.page_num_lo if priv.ep_tx_low_queue else 0
    nq = fops.page_num_norm if priv.ep_tx_normal_queue else 0
    eq = 0

    rtl8xxxu_write32(priv, C.REG_RQPN_NPQ, (nq << C.RQPN_NPQ_SHIFT) | (eq << C.RQPN_EPQ_SHIFT))

    pubq = fops.total_page_num - hq - lq - nq - 1
    val32 = C.RQPN_LOAD
    val32 |= hq << C.RQPN_HI_PQ_SHIFT
    val32 |= lq << C.RQPN_LO_PQ_SHIFT
    val32 |= pubq << C.RQPN_PUB_PQ_SHIFT
    rtl8xxxu_write32(priv, C.REG_RQPN, val32)


def rtl8xxxu_init_burst(priv: Rtl8xxxuPriv) -> None:
    val8 = rtl8xxxu_read8(priv, C.REG_RXDMA_PRO_8723B)
    val8 = u32_replace_bits(val8, 1, C.RXDMA_PRO_DMA_BURST_SIZE)
    val8 = u32_replace_bits(val8, 3, C.RXDMA_PRO_DMA_BURST_CNT)
    val8 |= C.RXDMA_PRO_DMA_MODE
    rtl8xxxu_write8(priv, C.REG_RXDMA_PRO_8723B, val8)

    rtl8xxxu_write8_set(priv, C.REG_HT_SINGLE_AMPDU_8723B, C.HT_SINGLE_AMPDU_ENABLE)

    rtl8xxxu_write16(priv, C.REG_MAX_AGGR_NUM, priv.fops.max_aggr_num)
    rtl8xxxu_write8(priv, C.REG_AMPDU_MAX_TIME_8723B, priv.fops.ampdu_max_time)
    rtl8xxxu_write32(priv, C.REG_AGGLEN_LMT, 0xFFFFFFFF)
    rtl8xxxu_write8(priv, C.REG_RX_PKT_LIMIT, 0x18)
    rtl8xxxu_write8(priv, C.REG_PIFS, 0x00)
    rtl8xxxu_write8(priv, C.REG_FWHW_TXQ_CTRL, C.FWHW_TXQ_CTRL_AMPDU_RETRY)
    rtl8xxxu_write32(priv, C.REG_FAST_EDCA_CTRL, 0x03086666)
    rtl8xxxu_write8(priv, C.REG_USTIME_TSF_8723B, priv.fops.ustime_tsf_edca)
    rtl8xxxu_write8(priv, C.REG_USTIME_EDCA, priv.fops.ustime_tsf_edca)

    rtl8xxxu_write8_set(priv, C.REG_RSV_CTRL, C.RSV_CTRL_WLOCK_1C | C.RSV_CTRL_DIS_PRST)


def rtl8xxxu_init_device(priv: Rtl8xxxuPriv, progress: Callable[[float, str], None] = lambda _v, _m: None) -> None:
    fops = priv.fops

    val8 = rtl8xxxu_read8(priv, C.REG_CR)
    val16 = rtl8xxxu_read16(priv, C.REG_SYS_CLKR)
    macpower = not (val8 == 0xEA or not val16 & C.SYS_CLK_MAC_CLK_ENABLE)
    if fops.needs_full_init:
        macpower = False

    progress(0.20, "Power on")
    fops.power_on(priv)

    if not macpower:
        rtl8xxxu_init_queue_reserved_page(priv)

    rtl8xxxu_init_queue_priority(priv)

    rtl8xxxu_write16(priv, C.REG_TRXFF_BNDY + 2, fops.trxff_boundary)

    progress(0.25, "Firmware download")
    rtl8xxxu_download_firmware(priv)
    rtl8xxxu_start_firmware(priv)

    progress(0.45, "MAC/BB/RF tables")

    rtl8xxxu_init_mac(priv)
    rtl8xxxu_init_phy_bb(priv)
    fops.init_phy_rf(priv)

    rtl8xxxu_write16_set(priv, C.REG_AFE_CTRL4, C.BIT(4) | C.BIT(15))

    val32 = C.FPGA0_RF_TRSW | C.FPGA0_RF_TRSWB | C.FPGA0_RF_ANTSW | C.FPGA0_RF_ANTSWB | \
        ((C.FPGA0_RF_ANTSW | C.FPGA0_RF_ANTSWB) << C.FPGA0_RF_BD_CTRL_SHIFT)
    val32 |= C.FPGA0_RF_PAPE | (C.FPGA0_RF_PAPE << C.FPGA0_RF_BD_CTRL_SHIFT)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XAB_RF_SW_CTRL, val32)

    if not macpower:
        val8 = fops.total_page_num + 1
        rtl8xxxu_write8(priv, C.REG_TXPKTBUF_BCNQ_BDNY, val8)
        rtl8xxxu_write8(priv, C.REG_TXPKTBUF_MGQ_BDNY, val8)
        rtl8xxxu_write8(priv, C.REG_TXPKTBUF_WMAC_LBK_BF_HD, val8)
        rtl8xxxu_write8(priv, C.REG_TRXFF_BNDY, val8)
        rtl8xxxu_write8(priv, C.REG_TDECTRL + 1, val8)

    val8 = (fops.pbp_rx << C.PBP_PAGE_SIZE_RX_SHIFT) | (fops.pbp_tx << C.PBP_PAGE_SIZE_TX_SHIFT)
    rtl8xxxu_write8(priv, C.REG_PBP, val8)

    if not macpower:
        rtl8xxxu_auto_llt_table(priv)
        fops.usb_quirks(priv)

        if fops.has_tx_report:
            val8 = rtl8xxxu_read8(priv, C.REG_TX_REPORT_CTRL)
            val8 |= C.TX_REPORT_CTRL_TIMER_ENABLE
            rtl8xxxu_write8(priv, C.REG_TX_REPORT_CTRL, val8)
            rtl8xxxu_write8(priv, C.REG_TX_REPORT_CTRL + 1, 0x02)
            rtl8xxxu_write16(priv, C.REG_TX_REPORT_TIME, 0xCDF0)
            val8 = rtl8xxxu_read8(priv, 0xA3)  # core.c:4087 "tmp ps ?"
            rtl8xxxu_write8(priv, 0xA3, val8 & 0xF8)

        rtl8xxxu_write8(priv, C.REG_EARLY_MODE_CONTROL_8710B, 0)

    rtl8xxxu_write8(priv, C.REG_RX_DRVINFO_SZ, 4)

    val32 = C.RCR_ACCEPT_PHYS_MATCH | C.RCR_ACCEPT_MCAST | C.RCR_ACCEPT_BCAST | \
        C.RCR_ACCEPT_MGMT_FRAME | C.RCR_HTC_LOC_CTRL | \
        C.RCR_APPEND_PHYSTAT | C.RCR_APPEND_ICV | C.RCR_APPEND_MIC
    rtl8xxxu_write32(priv, C.REG_RCR, val32)
    priv.regrcr = val32

    if fops.init_reg_rxfltmap:
        rtl8xxxu_write16(priv, C.REG_RXFLTMAP2, 0xFFFF)
        rtl8xxxu_write16(priv, C.REG_RXFLTMAP1, 0x400)
        rtl8xxxu_write16(priv, C.REG_RXFLTMAP0, 0xFFFF)

    val32 = rtl8xxxu_read32(priv, C.REG_RESPONSE_RATE_SET)
    val32 &= ~C.RESPONSE_RATE_BITMAP_ALL
    val32 |= C.RESPONSE_RATE_RRSR_CCK_ONLY_1M
    rtl8xxxu_write32(priv, C.REG_RESPONSE_RATE_SET, val32)

    rtl8xxxu_set_spec_sifs(priv, 0x10, 0x10)
    rtl8xxxu_set_retry(priv, 0x30, 0x30)
    rtl8xxxu_set_spec_sifs(priv, 0x0A, 0x10)

    rtl8xxxu_write16(priv, C.REG_MAC_SPEC_SIFS, 0x100A)
    rtl8xxxu_write16(priv, C.REG_SIFS_CCK, 0x100A)
    rtl8xxxu_write16(priv, C.REG_SIFS_OFDM, 0x100A)

    rtl8xxxu_write32(priv, C.REG_EDCA_BE_PARAM, 0x005EA42B)
    rtl8xxxu_write32(priv, C.REG_EDCA_BK_PARAM, 0x0000A44F)
    rtl8xxxu_write32(priv, C.REG_EDCA_VI_PARAM, 0x005EA324)
    rtl8xxxu_write32(priv, C.REG_EDCA_VO_PARAM, 0x002FA226)

    rtl8xxxu_write32(priv, C.REG_DARFRC, 0x00000000)
    rtl8xxxu_write32(priv, C.REG_DARFRC + 4, 0x10080404)
    rtl8xxxu_write32(priv, C.REG_RARFRC, 0x04030201)
    rtl8xxxu_write32(priv, C.REG_RARFRC + 4, 0x08070605)

    rtl8xxxu_write8_set(priv, C.REG_FWHW_TXQ_CTRL, C.FWHW_TXQ_CTRL_AMPDU_RETRY)

    rtl8xxxu_write8(priv, C.REG_ACKTO, 0x40)

    val16 = C.BEACON_DISABLE_TSF_UPDATE | (C.BEACON_DISABLE_TSF_UPDATE << 8)
    rtl8xxxu_write16(priv, C.REG_BEACON_CTRL, val16)
    rtl8xxxu_write16(priv, C.REG_TBTT_PROHIBIT, 0x6404)
    rtl8xxxu_write8(priv, C.REG_BEACON_DMA_TIME, C.BEACON_DMA_ATIME_INT_TIME)
    rtl8xxxu_write16(priv, C.REG_BEACON_TCFG, 0x660F)

    rtl8xxxu_init_burst(priv)
    fops.init_aggregation(priv)

    if fops.init_reg_pkt_life_time:
        rtl8xxxu_write16(priv, C.REG_PKT_VO_VI_LIFE_TIME, 0x0400)
        rtl8xxxu_write16(priv, C.REG_PKT_BE_BK_LIFE_TIME, 0x0400)

    rtl8xxxu_write32_set(priv, C.REG_FPGA0_RF_MODE, C.FPGA_RF_MODE_CCK | C.FPGA_RF_MODE_OFDM)

    rtl8xxxu_write32(priv, C.REG_CAM_CMD, C.CAM_CMD_POLLING | C.BIT(30))

    fops.set_tx_power(priv, 1, False)

    rtl8xxxu_write8_set(priv, C.REG_LEDCFG2, C.LEDCFG2_DPDT_SELECT)

    rtl8xxxu_write8(priv, C.REG_HWSEQ_CTRL, 0xFF)

    rtl8xxxu_write32(priv, C.REG_BAR_MODE_CTRL, 0x0201FFFF)

    rtl8xxxu_write8_clear(priv, C.REG_GPIO_MUXCFG, C.GPIO_MUXCFG_IO_SEL_ENBT)

    progress(0.65, "LC + IQ calibration")
    fops.phy_lc_calibrate(priv)
    fops.phy_iq_calibrate(priv)

    if fops.gen2_thermal_meter:
        rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_T_METER_8723B, 0x37CF8)
    else:
        rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_T_METER, 0x60)

    rtl8xxxu_write8(priv, C.REG_NAV_UPPER, (30000 + C.NAV_UPPER_UNIT - 1) // C.NAV_UPPER_UNIT)

    rtl8xxxu_write32_set(priv, C.REG_FWHW_TXQ_CTRL, C.FWHW_TXQ_CTRL_XMIT_MGMT_ACK)

    val32 = rtl8xxxu_read32(priv, 0xA9C)  # core.c:4385, no named register
    priv.cck_new_agc = bool(u32_get_bits(val32, C.BIT(17)))

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM1_CFO_TRACKING)
    priv.cfo_tracking.atc_status = bool(val32 & C.CFO_TRACKING_ATC_STATUS)
    priv.cfo_tracking.adjust = True
    priv.cfo_tracking.crystal_cap = priv.default_crystal_cap


def rtl8xxxu_sw_scan_start(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8_set(priv, C.REG_BEACON_CTRL, C.BEACON_DISABLE_TSF_UPDATE)


def rtl8xxxu_sw_scan_complete(priv: Rtl8xxxuPriv) -> None:
    rtl8xxxu_write8_clear(priv, C.REG_BEACON_CTRL, C.BEACON_DISABLE_TSF_UPDATE)


def rtl8xxxu_config(priv: Rtl8xxxuPriv, channel: int) -> None:
    """IEEE80211_CONF_CHANGE_CHANNEL at NL80211_CHAN_WIDTH_20 (we only tune 20 MHz)."""
    priv.fops.set_tx_power(priv, channel, False)
    priv.fops.config_channel(priv, channel)


def rtl8xxxu_configure_filter(priv: Rtl8xxxuPriv, total_flags: Ieee80211FilterFlags) -> None:
    rcr = priv.regrcr
    if total_flags & Ieee80211FilterFlags.FIF_FCSFAIL:
        rcr |= C.RCR_ACCEPT_CRC32
    else:
        rcr &= ~C.RCR_ACCEPT_CRC32

    if total_flags & Ieee80211FilterFlags.FIF_BCN_PRBRESP_PROMISC:
        rcr &= ~(C.RCR_CHECK_BSSID_BEACON | C.RCR_CHECK_BSSID_MATCH)
    else:
        rcr |= C.RCR_CHECK_BSSID_BEACON | C.RCR_CHECK_BSSID_MATCH

    if priv.vif_type == Nl80211Iftype.NL80211_IFTYPE_AP:
        rcr &= ~(C.RCR_CHECK_BSSID_MATCH | C.RCR_CHECK_BSSID_BEACON)

    if total_flags & Ieee80211FilterFlags.FIF_CONTROL:
        rcr |= C.RCR_ACCEPT_CTRL_FRAME
    else:
        rcr &= ~C.RCR_ACCEPT_CTRL_FRAME

    if total_flags & Ieee80211FilterFlags.FIF_OTHER_BSS:
        rcr |= C.RCR_ACCEPT_AP
    else:
        rcr &= ~C.RCR_ACCEPT_AP

    if total_flags & Ieee80211FilterFlags.FIF_PSPOLL:
        rcr |= C.RCR_ACCEPT_PM
    else:
        rcr &= ~C.RCR_ACCEPT_PM

    rtl8xxxu_write32(priv, C.REG_RCR, rcr)
    priv.regrcr = rcr


def rtl8xxxu_start(priv: Rtl8xxxuPriv) -> None:
    """Register side of rtl8xxxu_start; the caller starts the RX reader and the 2 s watchdog."""
    priv.fops.enable_rf(priv)
    rtl8xxxu_write16(priv, C.REG_RXFLTMAP2, 0xFFFF)
    rtl8xxxu_write16(priv, C.REG_RXFLTMAP0, 0xFFFF)
    rtl8xxxu_write32_mask(priv, C.REG_OFDM0_XA_AGC_CORE1, C.OFDM0_X_AGC_CORE1_IGI_MASK, 0x1E)


def rtl8xxxu_stop(priv: Rtl8xxxuPriv) -> None:
    """Register side of rtl8xxxu_stop; the caller stops the RX reader and the watchdog."""
    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0xFF)
    rtl8xxxu_write16(priv, C.REG_RXFLTMAP0, 0x0000)
    rtl8xxxu_write16(priv, C.REG_RXFLTMAP2, 0x0000)
    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0xFF)
    priv.fops.disable_rf(priv)


def rtl8xxxu_set_atc_status(priv: Rtl8xxxuPriv, atc_status: bool) -> None:
    cfo = priv.cfo_tracking
    if atc_status == cfo.atc_status:
        return
    cfo.atc_status = atc_status
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM1_CFO_TRACKING)
    if atc_status:
        val32 |= C.CFO_TRACKING_ATC_STATUS
    else:
        val32 &= ~C.CFO_TRACKING_ATC_STATUS
    rtl8xxxu_write32(priv, C.REG_OFDM1_CFO_TRACKING, val32)


def rtl8xxxu_track_cfo(priv: Rtl8xxxuPriv) -> None:
    cfo = priv.cfo_tracking
    if not priv.is_associated:
        cfo.adjust = True
        if cfo.crystal_cap > priv.default_crystal_cap:
            priv.fops.set_crystal_cap(priv, cfo.crystal_cap - 1)
        elif cfo.crystal_cap < priv.default_crystal_cap:
            priv.fops.set_crystal_cap(priv, cfo.crystal_cap + 1)
        rtl8xxxu_set_atc_status(priv, True)
        return
    # TODO: verify, untested here, needs an associated station vif (core.c:7251); wifit3 never associates.


def rtl8xxxu_watchdog_callback(priv: Rtl8xxxuPriv) -> None:
    """One 2 s tick; no stations exist in monitor mode, so rtl8xxxu_ra_iter never runs."""
    rtl8xxxu_track_cfo(priv)


def rtl8xxxu_init_led(priv: Rtl8xxxuPriv) -> None:
    priv.fops.led_classdev_brightness_set(priv, C.RTL8XXXU_HW_LED_CONTROL)


def rtl8xxxu_deinit_led(priv: Rtl8xxxuPriv) -> None:
    priv.fops.led_classdev_brightness_set(priv, LED_OFF)


MONITOR_FILTER_FLAGS = (
    Ieee80211FilterFlags.FIF_BCN_PRBRESP_PROMISC
    | Ieee80211FilterFlags.FIF_CONTROL
    | Ieee80211FilterFlags.FIF_OTHER_BSS
    | Ieee80211FilterFlags.FIF_PSPOLL
)
"""What mac80211 passes configure_filter for a monitor interface (the RCR the captures record)."""


def rtl8xxxu_probe(priv: Rtl8xxxuPriv, fw_data: bytes,
                   progress: Callable[[float, str], None] = lambda _v, _m: None) -> None:
    """Register I/O of rtl8xxxu_probe (core.c:7787-7890), after rtl8xxxu_parse_usb."""
    progress(0.10, "Identify chip")
    priv.fops.identify_chip(priv)
    progress(0.12, "Read EFUSE")
    rtl8xxxu_read_efuse(priv)
    priv.fops.parse_efuse(priv)
    rtl8xxxu_load_firmware(priv, fw_data)
    rtl8xxxu_init_device(priv, progress)
    rtl8xxxu_init_led(priv)


def start_monitor_interface(priv: Rtl8xxxuPriv) -> None:
    """What mac80211 calls bringing up a monitor interface: start, configure_filter, config(channel 1)."""
    rtl8xxxu_start(priv)
    rtl8xxxu_configure_filter(priv, MONITOR_FILTER_FLAGS)
    rtl8xxxu_config(priv, 1)
