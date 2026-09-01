"""RTL8822CU EFUSE read and logical-map decoder.

The physical 512-byte WIFI EFUSE is read through ``REG_EFUSE_CTRL`` and
expanded into the RTL8822C 768-byte logical shadow map. The control-register
writes select an address only; they never program EFUSE cells.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import constants
from .constants import (
    BIT_AUTOLOAD_SUS,
    BIT_BOARD_OPTION_1TX,
    BIT_EF_READY,
    DIS_DPD_RATE_ALL,
    DIS_DPD_RATE_NONE,
    EEPROM_BOARD_OPTION_BT_COMBO,
    EEPROM_DEFAULT_BOARD_OPTION,
    EEPROM_DEFAULT_CRYSTAL_CAP_110_8822C,
    EEPROM_DEFAULT_CRYSTAL_CAP_B9,
    EEPROM_ID,
    EEPROM_MAC_ADDR,
    EEPROM_RF_ANTENNA_OPT_8822C,
    EEPROM_RF_BOARD_OPTION_8822C,
    EEPROM_RFE_OPTION,
    EEPROM_SIZE,
    EEPROM_TRX_PATH_BMP_VALID,
    EEPROM_TX_PWR_CALIBRATE_RATE_8822C,
    EEPROM_XTAL_110_8822C,
    EEPROM_XTAL_111_8822C,
    EEPROM_XTAL_B9_8822C,
    EFUSE_ADDR_MASK,
    EFUSE_PROTECTED_SIZE,
    EFUSE_SIZE,
    HALMAC_RF_1T1R,
    HALMAC_RF_1T2R,
    HALMAC_RF_2T2R,
    HALMAC_RF_MAX_TYPE,
    HAL_SPEC_MAX_TX_CNT,
    HAL_SPEC_RF_REG_PATH_NUM,
    HAL_SPEC_RF_REG_TRX_PATH_BMP,
    HAL_SPEC_TXGI_MAX,
    MAC_HIDDEN_RPT_1ANT_TRX_PATH_BMP,
    MAC_HIDDEN_RPT_HW_STYPE_1TX,
    REG_ANAPARLDO_POW_MAC,
    REG_EFUSE_CTRL,
    REG_LDO_EFUSE_CTRL,
    REG_SYS_EEPROM_CTRL,
    RF_PATH_MAX,
    TXPWR_PG_WITH_PWR_IDX,
    TXPWR_PG_WITH_TSSI_OFFSET,
    XCAP_VALUE_MASK,
)


logger = logging.getLogger(__name__)

def pg_txpwr_msb_diff_to_s8bit(pg_v: int) -> int:
    """Sign extend the high PG diff nibble. [SRC hal/hal_com_phycfg.c:26,28]"""
    value = (pg_v & 0xF0) >> 4
    return value - 16 if value & 0x8 else value


def pg_txpwr_lsb_diff_to_s8bit(pg_v: int) -> int:
    """Sign extend the low PG diff nibble. [SRC hal/hal_com_phycfg.c:27,29]"""
    value = pg_v & 0x0F
    return value - 16 if value & 0x8 else value


def is_pg_txpwr_base_invalid(base: int) -> bool:
    """[SRC hal/hal_com_phycfg.c:30]"""
    return base > HAL_SPEC_TXGI_MAX


def is_pg_txpwr_diff_invalid(diff: int) -> bool:
    """A 0xF nibble sign extends to -1, which is in range: only bases reject 0xFF.
    [SRC hal/hal_com_phycfg.c:31]"""
    return diff > 7 or diff < -8


def is_xcap_110_invalid(value: int) -> bool:
    """EFUSE_XCAP_VALID_CHK_110/111_8822C, both on an already masked value.
    [SRC hal/rtl8822c/rtl8822c_ops.c:369-372]"""
    return value in (EEPROM_DEFAULT_CRYSTAL_CAP_110_8822C, XCAP_VALUE_MASK)


@dataclass(frozen=True)
class EfuseInfo:
    autoload_ok: bool
    map_valid: bool
    logical_map: bytes
    physical_map: bytes

    @property
    def mac_address(self) -> bytes:
        return self.logical_map[EEPROM_MAC_ADDR:EEPROM_MAC_ADDR + 6]

    @property
    def rfe_type(self) -> int:
        """RTL8822C RF front-end type programmed by the board vendor. ``constants.RFE_TYPE``
        overrides the EFUSE and is checked first, the precedence the vendor gives its
        ``rtw_RFE_type`` module parameter. Read through the module so setting it at runtime works
        as well as editing the file. [SRC Hal_ReadRFEType rtl8822c_ops.c:677-699]"""
        if constants.RFE_TYPE is not None:
            return constants.RFE_TYPE                                # [SRC :678-681]
        value = self.logical_map[EEPROM_RFE_OPTION]
        if self.map_valid and value != 0xFF:                         # [SRC :683-687]
            return value
        # The one deliberate raise: guessing "may cause card drop. it's DIFFICULT do debug
        # especially on COB project" [SRC :693-694]. constants.RFE_TYPE is the escape hatch.
        raise RuntimeError("RTL8822CU EFUSE has no RFE type at 0xCA")

    @property
    def tpt_mode(self) -> int:
        """EFUSE 0xC8[7:4]. Read raw, without the map_valid screen the other fields apply.
        [SRC hal/rtl8822c/rtl8822c_ops.c:251]"""
        return (self.logical_map[EEPROM_TX_PWR_CALIBRATE_RATE_8822C] & 0xF0) >> 4

    @property
    def txpwr_pg_mode(self) -> int:
        """Whether the PG bytes at 0x10..0x63 are power indices (thermal) or TSSI offsets. Both
        branches are compiled for 8822C [SRC include/hal_ic_cfg.h:462-467] and the
        rtw_powertracking_type override defaults to 64, out of range, so EFUSE alone decides.
        An unburned 0xC8 reads 0xFF (nibble 15): the vendor RTW_ERRs and aborts the whole probe
        [SRC rtl8822c_ops.c:273-274], but wifit3 keeps an unburned adapter for RX, so resolve to
        PWR_IDX and continue. That default is the vendor's own post abort state: txpwr_pg_mode is
        enum value 0 [SRC include/hal_com_phycfg.h:36-38] in a zvmalloc'd hal_data
        [SRC rtw_hal_data_init hal/hal_intf.c:147-148].
        [SRC Hal_EfuseParseTxPowerInfo hal/rtl8822c/rtl8822c_ops.c:259-273, os_intfs.c:709]"""
        mode = self.tpt_mode
        if mode <= 3:
            return TXPWR_PG_WITH_PWR_IDX
        if mode <= 7:
            return TXPWR_PG_WITH_TSSI_OFFSET
        logger.error("RTL8822CU EFUSE 0xC8=0x%02x unsupported tpt_mode=%u, assuming PWR_IDX",
                     self.logical_map[EEPROM_TX_PWR_CALIBRATE_RATE_8822C], mode)
        return TXPWR_PG_WITH_PWR_IDX

    @property
    def eeprom_regulatory(self) -> int:
        """0xC1[1:0], else the default 0. An unknown tpt_mode no longer skips this: the vendor left
        it at the zvmalloc'd 0, which is what a default/invalid map yields here anyway.
        [SRC hal/rtl8822c/rtl8822c_ops.c:280-283]"""
        value = self.logical_map[EEPROM_RF_BOARD_OPTION_8822C]
        if self.map_valid and value != 0xFF:
            return value & 0x3
        return EEPROM_DEFAULT_BOARD_OPTION & 0x3

    @property
    def dis_dpd_rate(self) -> int:
        """dm->dis_dpd_rate: ODM_CMNINFO_DIS_DPD is true exactly in PWR_IDX mode, and
        config_phydm_parameter_init_8822c turns that into 0x3FF (all ten flagged rates) or zero.
        config_phydm_switch_channel_8822c rewrites it per channel, forcing 0 on channel 1, but only
        under IOT patch 011f0500, and phydm_iot_patch_id_update has no caller in the vendor tree, so
        iot_table stays zero initialised. [SRC hal/rtl8822c/rtl8822c_phy.c:455-456,
        phydm_hal_api8822c.c:2222-2225, phydm_hal_api8822c.c:1760-1765, phydm.c:339,366]"""
        if self.txpwr_pg_mode == TXPWR_PG_WITH_PWR_IDX:
            return DIS_DPD_RATE_ALL
        return DIS_DPD_RATE_NONE

    @property
    def eeprom_trx_path_bmp(self) -> int:
        """[7:4] TX paths, [3:0] RX paths; 0 means unspecified, which an unrecognised 0xC9 leaves.
        [SRC Hal_EfuseParsePathSelection hal/rtl8822c/rtl8822c_ops.c:520-542]"""
        if not self.map_valid:
            return 0x00
        value = self.logical_map[EEPROM_RF_ANTENNA_OPT_8822C]
        return value if value in EEPROM_TRX_PATH_BMP_VALID else 0x00

    @property
    def eeprom_max_tx_cnt(self) -> int:
        """0 means unspecified. [SRC hal/rtl8822c/rtl8822c_ops.c:302-308]"""
        value = self.logical_map[EEPROM_RF_BOARD_OPTION_8822C]
        return 1 if self.map_valid and (value & BIT_BOARD_OPTION_1TX) else 0

    @property
    def bluetooth_coexist(self) -> bool:
        """EFUSE 0xC1[7:5] == 1, a combo module. Ported only because it selects which crystal cap
        policy runs; BT coexistence itself is out of wifit3's scope.
        [SRC Hal_EfuseParseBTCoexistInfo hal/rtl8822c/rtl8822c_ops.c:323-330]"""
        value = self.logical_map[EEPROM_RF_BOARD_OPTION_8822C]
        if not self.map_valid or value == 0xFF:
            return False
        return ((value & 0xE0) >> 5) == EEPROM_BOARD_OPTION_BT_COMBO

    def _search_xtal_cap(self) -> int | None:
        """hal_efuse_search_xtal_cap, reduced to its one live candidate. The two the vendor tries
        first both come from the CONFIG_EFUSE_CONFIG_FILE map, which wifit3 never loads, so they
        arrive all ones and fail their validity checks. [SRC hal/rtl8822c/rtl8822c_ops.c:390-424]"""
        b9 = self.logical_map[EEPROM_XTAL_B9_8822C] & XCAP_VALUE_MASK  # [SRC :401-402]
        return None if b9 == XCAP_VALUE_MASK else b9

    def _crystal_cap_new_policy(self) -> int:
        """hal_efuse_parse_xtal_cap_new with rtw_8822c_xcap_overwrite at its default 1
        [SRC os_dep/linux/os_intfs.c:1034]. The vendor burns the found value back into EFUSE 0x110
        [SRC rtl8822c_ops.c:449-453]; wifit3 never programs EFUSE cells, so only the resolved value
        is kept. Only a valid map reaches here, so the :441-444 arm is dead.
        [SRC hal/rtl8822c/rtl8822c_ops.c:446-463]"""
        found = self._search_xtal_cap()
        if found is not None:
            return found                                             # [SRC :455]
        low = self.logical_map[EEPROM_XTAL_110_8822C] & XCAP_VALUE_MASK
        high = self.logical_map[EEPROM_XTAL_111_8822C] & XCAP_VALUE_MASK
        if is_xcap_110_invalid(low) or is_xcap_110_invalid(high):    # [SRC :458-461]
            return EEPROM_DEFAULT_CRYSTAL_CAP_110_8822C
        return low

    @property
    def crystal_cap(self) -> int:
        """RTL8822C crystal cap. A combo module takes the newer 0xB9 then 0x110 policy, every other
        board Hal_EfuseParseXtal's raw 0xB9 with the B9 default 0x3F. Both feed the same
        ``phydm_set_crystal_cap_reg`` write, which masks to 7 bits.
        [SRC branch rtl8822c_ops.c:841-845, Hal_EfuseParseXtal :485-493]"""
        if self.bluetooth_coexist:
            return self._crystal_cap_new_policy()
        value = self.logical_map[EEPROM_XTAL_B9_8822C]
        if not self.map_valid or value == 0xFF:
            return EEPROM_DEFAULT_CRYSTAL_CAP_B9
        return value


# The (tx_num, rx_num) rows of _trx_num_to_rf_type an 8822C bmp can reach [SRC core/rtw_rf.c:2098-2099]
_HALMAC_RF_TYPE = {(1, 1): HALMAC_RF_1T1R, (1, 2): HALMAC_RF_1T2R, (2, 2): HALMAC_RF_2T2R}


@dataclass(frozen=True)
class RfPath:
    """hal_data->trx_path_bmp ([7:4] TX, [3:0] RX) and max_tx_cnt."""
    trx_path_bmp: int
    max_tx_cnt: int

    @property
    def tx_path(self) -> int:
        """GET_HAL_TX_PATH_BMP [SRC include/hal_data.h:883]"""
        return (self.trx_path_bmp & 0xF0) >> 4

    @property
    def rx_path(self) -> int:
        """GET_HAL_RX_PATH_BMP [SRC include/hal_data.h:884]"""
        return self.trx_path_bmp & 0x0F

    @property
    def halmac_rf_type(self) -> int:
        """general_info.rf_type: trx_bmp_to_rf_type [SRC core/rtw_rf.c:2111-2125] then
        _rf_type_drv2halmac [SRC hal/hal_halmac.c:2996-3033]. hal_spec's bmp is 0x33 and
        Hal_EfuseParsePathSelection accepts only 0x33/0x13/0x23/0x11/0x22, so the AND leaves no
        other path count pair; an unreachable one takes the C's default [SRC hal_halmac.c:3026]."""
        counts = (bin(self.tx_path).count("1"), bin(self.rx_path).count("1"))
        return _HALMAC_RF_TYPE.get(counts, HALMAC_RF_MAX_TYPE)


def _path_bmp_limit_from_higher(bmp: int, bit_cnt: int, bit_cnt_lmt: int) -> tuple[int, int]:
    """[SRC core/rtw_rf.c:2132-2142]"""
    for i in range(RF_PATH_MAX - 1, -1, -1):
        if bit_cnt <= bit_cnt_lmt:
            break
        if bmp & (1 << i):
            bmp &= ~(1 << i)
            bit_cnt -= 1
    return bmp, bit_cnt


def restrict_trx_path_bmp_by_trx_num_lmt(trx_path_bmp: int, tx_num_lmt: int,
                                         rx_num_lmt: int) -> tuple[int, int, int]:
    """Returns (bmp, tx_num, rx_num); a limit of 0 means unlimited and a returned bmp of 0 is the
    C's failure. _trx_num_to_rf_type is populated for every 1..RF_PATH_MAX pair, so the rf_type
    search only rejects a zero TX or RX count. [SRC core/rtw_rf.c:2097-2108,2145-2181]"""
    bmp_tx = (trx_path_bmp & 0xF0) >> 4
    bmp_rx = trx_path_bmp & 0x0F
    tx_num = bin(bmp_tx).count("1")
    rx_num = bin(bmp_rx).count("1")
    if tx_num_lmt:
        bmp_tx, tx_num = _path_bmp_limit_from_higher(bmp_tx, tx_num, tx_num_lmt)
    if rx_num_lmt:
        bmp_rx, rx_num = _path_bmp_limit_from_higher(bmp_rx, rx_num, rx_num_lmt)
    if not tx_num or not rx_num:
        return 0x00, 0, 0
    return (bmp_tx << 4) | bmp_rx, tx_num, rx_num


def _rfpath_without_efuse_narrowing(trx_path_bmp: int, max_tx_cnt: int) -> RfPath:
    """The continue value for both C probe aborts. The EFUSE bmp is a restriction layered on top of
    the already report edited hal_spec [SRC hal/hal_intf.c:351-358], so dropping only that layer
    keeps the C2H MAC hidden report's own restriction [SRC hal/hal_com.c:1475-1478]. Returning the
    unrestricted 0x33 / 2 instead would discard a live hardware report: the PG walk would read the
    2T diff bytes and phy_get_pg_txpwr_idx would add *_Diff[path][1]
    [SRC hal/hal_com_phycfg.c:2490-2491] on a part with one TX path."""
    tx_num = bin((trx_path_bmp & 0xF0) >> 4).count("1")
    return RfPath(trx_path_bmp, min(max_tx_cnt, tx_num))


def hal_rfpath_init(efuse: EfuseInfo, *, ant_num: int, hw_stype: int, rf_2t2r: bool) -> RfPath:
    """hal_data->trx_path_bmp and max_tx_cnt, the TX index bound the PG walk reads. The C2H MAC
    hidden report edits hal_spec first: it is read at rtl8822c_ops.c:867, before rtw_hal_rfpath_init
    runs from rtw_drv_init (os_dep/linux/os_intfs.c:2919). Both registry path limits are 0.
    [SRC c2h_mac_hidden_rpt_hdl hal/hal_com.c:1472-1511, rtw_hal_rfpath_init hal/hal_intf.c:344-376]"""
    trx_path_bmp = HAL_SPEC_RF_REG_TRX_PATH_BMP
    max_tx_cnt = HAL_SPEC_MAX_TX_CNT
    if ant_num == 1:
        trx_path_bmp = MAC_HIDDEN_RPT_1ANT_TRX_PATH_BMP                      # [SRC hal_com.c:1475-1476]
    if hw_stype == MAC_HIDDEN_RPT_HW_STYPE_1TX:
        max_tx_cnt = min(max_tx_cnt, 1)                                      # [SRC hal_com.c:1477-1478]
    # rf_type comes from version_id RF_TYPE, the REG_SYS_CFG1 bit chipid.py reads. 8822C sets that
    # field to RF_TYPE_2T2R or RF_TYPE_1T1R and nothing else [SRC hal/rtl8822c/rtl8822c_ops.c:197],
    # so the asymmetric IS_1T2R / IS_2T3R branches never fire and tx_num == rx_num.
    # [SRC rtw_chip_rftype_to_hal_rftype hal/hal_intf.c:217-231]
    chip_path_num = 2 if rf_2t2r else 1
    avail_num = min(HAL_SPEC_RF_REG_PATH_NUM, ant_num)                       # [SRC hal_com.c:1493]
    hidden_rpt_bmp = trx_path_bmp
    trx_path_bmp, tx_path_num, _rx_path_num = restrict_trx_path_bmp_by_trx_num_lmt(
        trx_path_bmp, min(chip_path_num, avail_num), min(chip_path_num, avail_num))  # :1494-1497
    if not trx_path_bmp:
        # Unreachable: with 0x33 or 0x22 and a limit of 0 meaning unlimited, the restrict cannot
        # empty a nibble. The C aborts the probe [SRC hal_intf.c:364-368]; wifit3 continues.
        logger.error("RTL8822CU MAC hidden report (ant_num=%u, hw_stype=0x%x) leaves no RF path, "
                     "assuming 0x%02x", ant_num, hw_stype, hidden_rpt_bmp)
        return _rfpath_without_efuse_narrowing(hidden_rpt_bmp, max_tx_cnt)
    max_tx_cnt = min(max_tx_cnt, tx_path_num)                                # [SRC hal_com.c:1510]

    pre_and_bmp = trx_path_bmp
    if efuse.eeprom_trx_path_bmp:
        trx_path_bmp &= efuse.eeprom_trx_path_bmp                            # [SRC hal_intf.c:351-358]
    trx_path_bmp, tx_path_num, _rx_path_num = restrict_trx_path_bmp_by_trx_num_lmt(trx_path_bmp, 0, 0)
    # Two distinct C exits with the same outcome: an empty AND (:354-358), which 0xC9=0x11 with a
    # 1 antenna report hits (0x22 & 0x11 = 0x00), or a survivor whose TX or RX nibble is empty
    # (:364-368), which 0xC9=0x13 hits (0x22 & 0x13 = 0x02). The C returns _FAIL and aborts;
    # wifit3 keeps the adapter for RX with the pre AND state.
    if not trx_path_bmp:
        logger.error("RTL8822CU EFUSE 0xC9=0x%02x does not intersect the RF paths, assuming 0x%02x",
                     efuse.eeprom_trx_path_bmp, pre_and_bmp)
        return _rfpath_without_efuse_narrowing(pre_and_bmp, max_tx_cnt)
    max_tx_cnt = min(max_tx_cnt, tx_path_num)                                # [SRC hal_intf.c:373-374]
    if efuse.eeprom_max_tx_cnt:
        max_tx_cnt = min(max_tx_cnt, efuse.eeprom_max_tx_cnt)                # [SRC hal_intf.c:375-376]
    return RfPath(trx_path_bmp, max_tx_cnt)


def read_physical_map(transport) -> bytes:
    """Read the WIFI physical EFUSE map through the indirect controller.

    Mirrors HALMAC ``dump_efuse_map_88xx`` -> ``switch_efuse_bank_88xx`` (WIFI bank is
    already selected, so a bare read) -> ``read_hw_efuse_88xx`` (drop the 2.5 V LDO, then
    the REG_EFUSE_CTRL byte loop) [SRC hal/halmac/halmac_88xx/halmac_efuse_88xx.c:168,1042-1067].
    """
    transport.read8(REG_LDO_EFUSE_CTRL + 1)                  # switch_efuse_bank: WIFI bank
    ldo25 = transport.read8(REG_ANAPARLDO_POW_MAC)           # read efuse needs no 2.5 V LDO
    transport.write8(REG_ANAPARLDO_POW_MAC, ldo25 & ~1)
    current = transport.read32(REG_EFUSE_CTRL)
    out = bytearray(EFUSE_SIZE)
    for addr in range(EFUSE_SIZE):
        request = current & ~(0xFF | (EFUSE_ADDR_MASK << 8) | BIT_EF_READY)
        transport.write32(REG_EFUSE_CTRL, request | ((addr & EFUSE_ADDR_MASK) << 8))
        for _ in range(1000):
            current = transport.read32(REG_EFUSE_CTRL)
            if current & BIT_EF_READY:
                out[addr] = current & 0xFF
                break
        else:
            raise RuntimeError(f"RTL8822CU EFUSE read timed out at 0x{addr:03x}")
    return bytes(out)


def one_byte_read(transport, physical_map: bytes, address: int) -> int:
    """``efuse_OneByteRead`` -> ``rtw_halmac_read_physical_efuse``: halmac answers from the map
    it dumped at bring-up, so only the bank-switch check reaches the wire.
    [SRC core/efuse/rtw_efuse.c:2226, halmac_efuse_88xx.c:168]"""
    transport.read8(REG_LDO_EFUSE_CTRL + 1)
    return physical_map[address]


def decode_logical_map(physical_map: bytes) -> bytes:
    """Decode Realtek's word-enabled physical EFUSE stream into its shadow map."""
    if len(physical_map) != EFUSE_SIZE:
        raise ValueError(f"RTL8822CU physical EFUSE must be {EFUSE_SIZE} bytes")
    logical = bytearray(b"\xff" * EEPROM_SIZE)
    end = EFUSE_SIZE - EFUSE_PROTECTED_SIZE
    idx = 0
    while idx < end:
        header = physical_map[idx]
        idx += 1
        if header == 0xFF:
            break
        if (header & 0x1F) == 0x0F:
            if idx >= end or physical_map[idx] == 0xFF:
                break
            header2 = physical_map[idx]
            idx += 1
            block = ((header2 & 0xF0) >> 1) | ((header >> 5) & 0x07)
            word_enable = header2 & 0x0F
        else:
            block = header >> 4
            word_enable = header & 0x0F
        for word in range(4):
            if word_enable & (1 << word):
                continue
            target = block * 8 + word * 2
            if idx + 1 >= end or target + 1 >= EEPROM_SIZE:
                raise RuntimeError("RTL8822CU EFUSE map is malformed")
            logical[target:target + 2] = physical_map[idx:idx + 2]
            idx += 2
    return bytes(logical)


def read_efuse(transport) -> EfuseInfo:
    # rtl8822c_read_efuse: bautoload_fail_flag = (val8 & BIT_AUTOLOAD_SUS) ? _FALSE : _TRUE,
    # so autoload is OK (the inverse of the fail flag) exactly when the bit is set.
    # [SRC hal/rtl8822c/rtl8822c_ops.c:794]
    autoload_ok = bool(transport.read8(REG_SYS_EEPROM_CTRL) & BIT_AUTOLOAD_SUS)
    physical_map = read_physical_map(transport)
    logical_map = decode_logical_map(physical_map)
    map_valid = int.from_bytes(logical_map[:2], "little") == EEPROM_ID
    # The vendor aborts read_efuse when tpt_mode is out of range (rtl8822c_ops.c:834-835), because
    # it owns the whole device. wifit3 does not: an unburned adapter still RXes, so no probe screen
    # here. txpwr_pg_mode resolves the unknown to PWR_IDX and logs.
    return EfuseInfo(autoload_ok, map_valid, logical_map, physical_map)
