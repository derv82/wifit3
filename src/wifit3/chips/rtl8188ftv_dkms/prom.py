"""RTL8188FTV DKMS EFUSE parses + adapter-info orchestration (M2, mostly pure).

Ported from ``_ReadPROMContent`` + ``InitAdapterVariablesByPROM_8188FU``
(hal/rtl8188f/usb/usb_halinit.c:2199-2268), ``EFUSE_Read1Byte``
(core/efuse/rtw_efuse.c:311-360), the ``Hal_EfuseParse*`` suite
(hal/rtl8188f/rtl8188f_hal_init.c:3897-4460), ``hal_config_macaddr``
(hal/hal_com.c:3543-3587) and ``rtw_check_invalid_mac_address``. Wire I/O
lives in efuse.py; everything here is byte surgery on the 512 B shadow map,
except ``parse_kfree`` (two ``EFUSE_Read1Byte`` physical reads).
``Hal_EfuseParseVoltage_8188F`` is ``#if 0``'d out in the source and
``Hal_EfuseParseAntennaDiversity_8188F`` compiles empty (CONFIG_ANTENNA_
DIVERSITY=n); ``Hal_EfuseParseBTCoexistInfo``/LED/rate-option calls are
commented out, ``Hal_EfusePgPacketRead`` is not on the read graph (the walker
uses ``efuse_OneByteRead`` directly), and ``Hal_EEValueCheck`` has no callers.
None of those are ported.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import chplan, efuse
from . import constants as C


def BIT(n: int) -> int:
    return 1 << n


# include/hal_pg.h:282-322 (8188FE/8188FU/8188FS section)
EEPROM_TX_PWR_INX_8188F = 0x10
EEPROM_ChannelPlan_8188F = 0xB8
EEPROM_XTAL_8188F = 0xB9
EEPROM_THERMAL_METER_8188F = 0xBA
EEPROM_RF_BOARD_OPTION_8188F = 0xC1
EEPROM_FEATURE_OPTION_8188F = 0xC2
EEPROM_VERSION_8188F = 0xC4
EEPROM_CustomID_8188F = 0xC5
EEPROM_COUNTRY_CODE_8188F = 0xCB
EEPROM_VID_8188FU = 0xD0
EEPROM_PID_8188FU = 0xD2
EEPROM_USB_OPTIONAL_FUNCTION0_8188FU = 0xD4
EEPROM_MAC_ADDR_8188FU = 0xD7

# include/hal_pg.h:465,475,487-492,522-530
RTL_EEPROM_ID = 0x8129
EEPROM_Default_ThermalMeter_8188F = 0x18
EEPROM_Default_CrystalCap_8188F = 0x20
EEPROM_Default_TxPowerLevel = 0x22
EEPROM_DEFAULT_24G_INDEX = 0x2D
EEPROM_DEFAULT_24G_HT20_DIFF = 0x02
EEPROM_DEFAULT_24G_OFDM_DIFF = 0x04
EEPROM_DEFAULT_DIFF = 0xFE
EEPROM_DEFAULT_BOARD_OPTION = 0x00

# include/hal_pg.h:594-600
MAX_RF_PATH = 4
MAX_CHNL_GROUP_24G = 6
MAX_TX_COUNT = 4
CENTER_CH_2G_NUM = 14

# include/hal_pg.h:24-31,286-290 + THERMAL_K_MEAN offset (KFree body)
PPG_BB_GAIN_2G_TX_OFFSET_MASK = 0x0F
PPG_THERMAL_OFFSET_MASK = 0x1F
PPG_BB_GAIN_2G_TXA_OFFSET_8188F = 0xEE
PPG_THERMAL_OFFSET_8188F = 0xEF
EEPROM_TX_PWR_CALIBRATE_RATE_8188F = 0xC8
PPG_THERMAL_OFFSET_8188F = 0xEF
THERMAL_K_MEAN_OFFSET_8188F = 5
KFREE_FLAG_ON = BIT(0)
KFREE_FLAG_THERMAL_K_ON = BIT(1)

# os_dep/linux/os_intfs.c:201 (module-param default)
HWPND_MODE_DEFAULT = 2


def _sign4(v: int) -> int:
    return (v | 0xF0) - 256 if v & BIT(3) else v


def chnl_group_2g(channel: int) -> int:
    if 1 <= channel <= 2:
        return 0
    if 3 <= channel <= 5:
        return 1
    if 6 <= channel <= 8:
        return 2
    if 9 <= channel <= 11:
        return 3
    return 4


def check_invalid_mac(mac: bytes, check_local_bit: bool) -> bool:
    if mac == b"\x00" * 6 or mac == b"\xff" * 6:
        return True
    if mac[0] & BIT(0):
        return True
    return check_local_bit and bool(mac[0] & BIT(1))


def _check_txpwr(value: int) -> int:
    return value if value <= 63 else EEPROM_Default_TxPowerLevel


@dataclass
class TxPower24G:
    cck_base: list = field(default_factory=list)
    bw40_base: list = field(default_factory=list)
    bw40_diff: list = field(default_factory=list)
    bw20_diff: list = field(default_factory=list)
    ofdm_diff: list = field(default_factory=list)
    cck_diff: list = field(default_factory=list)


MAX_CHNL_GROUP_5G = 14
TX_PWR_DIFF_OFFSET_5G = 10


def read_power_value(prom: bytes, autoload_fail: bool) -> TxPower24G:
    out = TxPower24G()
    addr = EEPROM_TX_PWR_INX_8188F
    if not autoload_fail and prom[addr + 1] == 0xFF:
        autoload_fail = True
    for _ in range(MAX_RF_PATH):
        cck, bw40, bw40d, bw20, ofdm, cckd = [], [], [], [], [], []
        if autoload_fail:
            cck = [EEPROM_DEFAULT_24G_INDEX] * MAX_CHNL_GROUP_24G
            bw40 = [EEPROM_DEFAULT_24G_INDEX] * MAX_CHNL_GROUP_24G
            bw40d = [EEPROM_DEFAULT_DIFF] * MAX_TX_COUNT
            bw20 = [EEPROM_DEFAULT_24G_HT20_DIFF] + [EEPROM_DEFAULT_DIFF] * 3
            ofdm = [EEPROM_DEFAULT_24G_OFDM_DIFF] + [EEPROM_DEFAULT_DIFF] * 3
            cckd = [EEPROM_DEFAULT_DIFF] * MAX_TX_COUNT
        else:
            for _ in range(MAX_CHNL_GROUP_24G):
                v = prom[addr]
                addr += 1
                cck.append(v if v != 0xFF else EEPROM_DEFAULT_24G_INDEX)
            for _ in range(MAX_CHNL_GROUP_24G - 1):
                v = prom[addr]
                addr += 1
                bw40.append(v if v != 0xFF else EEPROM_DEFAULT_24G_INDEX)
            for tx in range(MAX_TX_COUNT):
                if tx == 0:
                    bw40d.append(0)
                    bw20.append(_sign4((prom[addr] & 0xF0) >> 4))
                    ofdm.append(_sign4(prom[addr] & 0x0F))
                    cckd.append(0)
                    addr += 1
                else:
                    bw40d.append(_sign4((prom[addr] & 0xF0) >> 4))
                    bw20.append(_sign4(prom[addr] & 0x0F))
                    addr += 1
                    ofdm.append(_sign4((prom[addr] & 0xF0) >> 4))
                    cckd.append(_sign4(prom[addr] & 0x0F))
                    addr += 1
            addr += MAX_CHNL_GROUP_5G + TX_PWR_DIFF_OFFSET_5G
        out.cck_base.append(cck)
        out.bw40_base.append(bw40)
        out.bw40_diff.append(bw40d)
        out.bw20_diff.append(bw20)
        out.ofdm_diff.append(ofdm)
        out.cck_diff.append(cckd)
    return out


@dataclass
class EfuseParams:
    eeprom_size: int = 4
    eeprom_or_efuse: bool = False
    autoload_fail: bool = False
    vid: int = 0
    pid: int = 0
    version: int = 1
    mac: bytes = b"\x00" * 6
    txpower: TxPower24G = field(default_factory=TxPower24G)
    cck_base_ch: list = field(default_factory=list)
    bw40_base_ch: list = field(default_factory=list)
    regulatory: int = 0
    chplan: int = chplan.RTW_CHPLAN_WORLD_NULL
    chplan_disable_sw: bool = False
    thermal: int = EEPROM_Default_ThermalMeter_8188F
    crystal: int = EEPROM_Default_CrystalCap_8188F
    customer: int = 0
    hw_powerdown: bool | int = False
    remote_wakeup: bool = False
    kfree_flag: int = 0
    kfree_bb_gain: int = 0
    kfree_thermal: int = 0


def parse_txpower(prom: bytes, autoload_fail: bool, params: EfuseParams) -> None:
    info = read_power_value(prom, autoload_fail)
    params.txpower = info
    for rf in range(MAX_RF_PATH):
        cck_ch, bw40_ch = [], []
        for ch in range(CENTER_CH_2G_NUM):
            group = chnl_group_2g(ch + 1)
            if ch == 14 - 1:
                cck_ch.append(info.cck_base[rf][5])
            else:
                cck_ch.append(info.cck_base[rf][group])
            bw40_ch.append(info.bw40_base[rf][group])
        params.cck_base_ch.append(cck_ch)
        params.bw40_base_ch.append(bw40_ch)
    if not autoload_fail:
        params.regulatory = prom[EEPROM_RF_BOARD_OPTION_8188F] & 0x7
        if prom[EEPROM_RF_BOARD_OPTION_8188F] == 0xFF:
            params.regulatory = EEPROM_DEFAULT_BOARD_OPTION & 0x7
    else:
        params.regulatory = 0


def parse_chnlplan(prom: bytes, autoload_fail: bool, params: EfuseParams,
                   sw_alpha2: bytes = b"\xff\xff",
                   sw_chplan: int = chplan.RTW_CHPLAN_MAX) -> None:
    plan, disable_sw, _country = chplan.config_channel_plan(
        prom[EEPROM_COUNTRY_CODE_8188F:EEPROM_COUNTRY_CODE_8188F + 2],
        prom[EEPROM_ChannelPlan_8188F], sw_alpha2, sw_chplan,
        chplan.RTW_CHPLAN_WORLD_NULL, autoload_fail)
    params.chplan = plan
    params.chplan_disable_sw = disable_sw


def parse_thermal(prom: bytes, autoload_fail: bool, params: EfuseParams) -> None:
    params.thermal = prom[EEPROM_THERMAL_METER_8188F] \
        if not autoload_fail else EEPROM_Default_ThermalMeter_8188F
    if params.thermal == 0xFF or autoload_fail:
        params.thermal = EEPROM_Default_ThermalMeter_8188F


def parse_power_saving(prom: bytes, autoload_fail: bool, params: EfuseParams,
                       hwpdn_mode: int = HWPND_MODE_DEFAULT) -> None:
    if autoload_fail:
        params.hw_powerdown = False
        params.remote_wakeup = False
    else:
        if hwpdn_mode == 2:
            params.hw_powerdown = prom[EEPROM_FEATURE_OPTION_8188F] & BIT(4)
        else:
            params.hw_powerdown = hwpdn_mode
        params.remote_wakeup = bool(prom[EEPROM_USB_OPTIONAL_FUNCTION0_8188FU] & BIT(1))


def parse_kfree(t, prom: bytes, params: EfuseParams) -> None:
    ok, pg_pwrtrim = efuse_read1byte(t, PPG_BB_GAIN_2G_TXA_OFFSET_8188F)
    ok, pg_therm = efuse_read1byte(t, PPG_THERMAL_OFFSET_8188F)
    if pg_pwrtrim != 0xFF:
        v = pg_pwrtrim & PPG_BB_GAIN_2G_TX_OFFSET_MASK
        params.kfree_bb_gain = 0 if v == PPG_BB_GAIN_2G_TX_OFFSET_MASK \
            else (v >> 1) if v & 0x01 else -(v >> 1)
        params.kfree_flag |= KFREE_FLAG_ON
    if pg_therm != 0xFF:
        v = pg_therm & PPG_THERMAL_OFFSET_MASK
        params.kfree_thermal = (0 if v == PPG_THERMAL_OFFSET_MASK
                                else (v >> 1) if v & 0x01 else -(v >> 1)) \
            - THERMAL_K_MEAN_OFFSET_8188F
        if (prom[EEPROM_TX_PWR_CALIBRATE_RATE_8188F] >> 5) & 1:
            params.kfree_flag |= KFREE_FLAG_THERMAL_K_ON
    if params.kfree_flag & KFREE_FLAG_THERMAL_K_ON:
        params.thermal -= params.kfree_thermal


def efuse_read1byte(t, address: int) -> tuple[bool, int]:
    if address >= C.EFUSE_REAL_CONTENT_LEN_8188F:
        return True, 0xFF
    t.write8(C.EFUSE_CTRL + 1, address & 0xFF)
    t.write8(C.EFUSE_CTRL + 2, ((address >> 8) & 0x03) | (t.read8(C.EFUSE_CTRL + 2) & 0xFC))
    t.write8(C.EFUSE_CTRL + 3, t.read8(C.EFUSE_CTRL + 3) & 0x7F)
    value = t.read8(C.EFUSE_CTRL + 3)
    k = 0
    while not value & 0x80:
        value = t.read8(C.EFUSE_CTRL + 3)
        k += 1
        if k == 1000:
            k = 0
            break
    return True, t.read8(C.EFUSE_CTRL)


def read_adapter_info(t, smic: bool) -> tuple[EfuseParams, bytes]:
    params = EfuseParams()
    params.eeprom_size = efuse.get_eeprom_size(t)
    efuse.cell_select(t)
    boot = t.read8(C.REG_9346CR)
    params.eeprom_or_efuse = bool(boot & C.EEPROMSEL)
    params.autoload_fail = not bool(boot & C.EEPROM_EN)
    efuse.power_switch(t, False, True)
    table = efuse.read_section_map(t, smic)
    efuse.power_switch(t, False, False)
    prom = table
    eeprom_id = int.from_bytes(prom[0:2], "little")
    if eeprom_id != RTL_EEPROM_ID:
        params.autoload_fail = True
    fail = params.autoload_fail
    params.vid = 0 if fail else int.from_bytes(prom[EEPROM_VID_8188FU:EEPROM_VID_8188FU + 2], "little")
    params.pid = 0 if fail else int.from_bytes(prom[EEPROM_PID_8188FU:EEPROM_PID_8188FU + 2], "little")
    params.version = 1 if fail else prom[EEPROM_VERSION_8188F]
    mac = bytes(prom[EEPROM_MAC_ADDR_8188FU:EEPROM_MAC_ADDR_8188FU + 6])
    params.mac = mac if not fail and not check_invalid_mac(mac, True) else b"\x00" * 6
    parse_txpower(prom, fail, params)
    parse_chnlplan(prom, fail, params)
    parse_thermal(prom, fail, params)
    parse_power_saving(prom, fail, params)
    parse_antenna_diversity()
    parse_eeprom_ver(prom, fail, params)
    params.customer = 0 if fail else prom[EEPROM_CustomID_8188F]
    params.crystal = EEPROM_Default_CrystalCap_8188F \
        if fail or prom[EEPROM_XTAL_8188F] == 0xFF else prom[EEPROM_XTAL_8188F]
    parse_kfree(t, prom, params)
    return params, table


def parse_eeprom_ver(prom: bytes, autoload_fail: bool, params: EfuseParams) -> None:
    params.version = 1 if autoload_fail else prom[EEPROM_VERSION_8188F]


def parse_antenna_diversity() -> None:
    pass

