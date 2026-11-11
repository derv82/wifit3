from __future__ import annotations

import errno
import logging
from dataclasses import dataclass

from . import constants as C
from .priv import RtlPriv, RtlStats
from .stats import rtl_evm_db_to_percentage, rtl_process_phyinfo, rtl_query_rxpwrpercentage, rtl_signal_scale_mapping

logger = logging.getLogger(__name__)


def CHIP_BONDING_IDENTIFIER(value: int) -> int:
    return (value >> 22) & 0x3


def IS_NORMAL_CHIP(version: int) -> bool:
    return bool(version & C.NORMAL_CHIP)


def IS_2T2R(version: int) -> bool:
    return (version & C.RF_TYPE_MASK) == C.CHIP_92C_BITMASK


def IS_92C_SERIAL(version: int) -> bool:
    """fw_common.h:34; false for a 1T2R version (RF type bits 0b11)."""
    return IS_2T2R(version)


def IS_92C_1T2R(version: int) -> bool:
    return (version & C.CHIP_92C_1T2R) == C.CHIP_92C_1T2R


def IS_VENDOR_UMC(version: int) -> bool:
    return bool(version & C.CHIP_VENDOR_UMC)


def IS_VENDOR_UMC_A_CUT(version: int) -> bool:
    return IS_VENDOR_UMC(version) and not (version & C.CUT_VERSION_MASK)


def IS_81XXC_VENDOR_UMC_B_CUT(version: int) -> bool:
    return IS_VENDOR_UMC(version) and (version & C.CUT_VERSION_MASK) == C.CHIP_VENDOR_UMC_B_CUT


def rtl92c_read_chip_version(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:27."""
    rtlhal = rtlpriv.rtlhal
    rtlphy = rtlpriv.phy
    value32 = rtlpriv.io.read32(C.REG_SYS_CFG)
    if value32 & C.TRP_VAUX_EN:
        chip_version = C.VERSION_TEST_CHIP_92C if value32 & C.TYPE_ID else C.VERSION_TEST_CHIP_88C
    else:
        chip_version = C.NORMAL_CHIP
        chip_version |= C.CHIP_92C if value32 & C.TYPE_ID else 0
        chip_version |= C.CHIP_VENDOR_UMC if value32 & C.VENDOR_ID else 0
        if IS_VENDOR_UMC(chip_version):
            chip_version |= C.CHIP_VENDOR_UMC_B_CUT if value32 & C.CHIP_VER_RTL_MASK else 0
        if IS_92C_SERIAL(chip_version):
            value32 = rtlpriv.io.read32(C.REG_HPON_FSM)
            chip_version |= (C.CHIP_92C_1T2R
                             if CHIP_BONDING_IDENTIFIER(value32) == C.CHIP_BONDING_92C_1T2R else 0)
    rtlhal.version = chip_version

    if IS_92C_SERIAL(rtlhal.version):
        rtlphy.rf_type = C.RF_1T2R if IS_92C_1T2R(rtlhal.version) else C.RF_2T2R
    else:
        rtlphy.rf_type = C.RF_1T1R
    if rtlphy.rf_type == C.RF_1T1R:
        rtlpriv.dm.rfpath_rxenable[0] = True
    else:
        rtlpriv.dm.rfpath_rxenable[0] = rtlpriv.dm.rfpath_rxenable[1] = True


def rtl92c_llt_write(rtlpriv: RtlPriv, address: int, data: int) -> bool:
    """rtl8192cu/mac.c:124."""
    io = rtlpriv.io
    status = True
    count = 0
    value = C._LLT_INIT_ADDR(address) | C._LLT_INIT_DATA(data) | C._LLT_OP(C._LLT_WRITE_ACCESS)
    io.write32(C.REG_LLT_INIT, value)
    while True:
        value = io.read32(C.REG_LLT_INIT)
        if C._LLT_OP_VALUE(value) == C._LLT_NO_ACTIVE:
            break
        if count > C.POLLING_LLT_THRESHOLD:
            logger.error("Failed to polling write LLT done at address %d! _LLT_OP_VALUE(%x)",
                         address, C._LLT_OP_VALUE(value))
            status = False
            break
        count += 1
    return status


def rtl92c_init_llt_table(rtlpriv: RtlPriv, boundary: int) -> bool:
    """rtl8192cu/mac.c:154."""
    for i in range(boundary - 1):
        if not rtl92c_llt_write(rtlpriv, i, i + 1):
            logger.error("===> rtl92c_init_llt_table #1 fail")
            return False
    if not rtl92c_llt_write(rtlpriv, boundary - 1, 0xFF):
        logger.error("===> rtl92c_init_llt_table #2 fail")
        return False
    for i in range(boundary, C.LLT_LAST_ENTRY_OF_TX_PKT_BUFFER):
        if not rtl92c_llt_write(rtlpriv, i, i + 1):
            logger.error("===> rtl92c_init_llt_table #3 fail")
            return False
    if not rtl92c_llt_write(rtlpriv, C.LLT_LAST_ENTRY_OF_TX_PKT_BUFFER, boundary):
        logger.error("===> rtl92c_init_llt_table #4 fail")
        return False
    return True


def rtl92c_enable_interrupt(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:331; the 8192CE branch reads rtl_pci, which a USB device never has."""
    io = rtlpriv.io
    rtlusb = rtlpriv.usb
    io.write32(C.REG_HIMR, rtlusb.irq_mask[0] & 0xFFFFFFFF)
    io.write32(C.REG_HIMRE, rtlusb.irq_mask[1] & 0xFFFFFFFF)


def rtl92c_init_interrupt(rtlpriv: RtlPriv) -> None:
    rtl92c_enable_interrupt(rtlpriv)


def rtl92c_disable_interrupt(rtlpriv: RtlPriv) -> None:
    rtlpriv.io.write32(C.REG_HIMR, C.IMR8190_DISABLED)
    rtlpriv.io.write32(C.REG_HIMRE, C.IMR8190_DISABLED)


def rtl92c_init_driver_info_size(rtlpriv: RtlPriv, size: int) -> None:
    rtlpriv.io.write8(C.REG_RX_DRVINFO_SZ, size)


def rtl92c_set_network_type(rtlpriv: RtlPriv, type: int) -> int:
    """rtl8192cu/mac.c:378."""
    if type == C.NL80211_IFTYPE_UNSPECIFIED:
        value = C.NT_NO_LINK
    elif type == C.NL80211_IFTYPE_ADHOC:
        value = C.NT_LINK_AD_HOC
    elif type == C.NL80211_IFTYPE_STATION:
        value = C.NT_LINK_AP
    elif type == C.NL80211_IFTYPE_AP:
        value = C.NT_AS_AP
    else:
        return -errno.EOPNOTSUPP
    rtlpriv.io.write8(C.MSR, value)
    return 0


def rtl92c_init_network_type(rtlpriv: RtlPriv) -> None:
    rtl92c_set_network_type(rtlpriv, C.NL80211_IFTYPE_UNSPECIFIED)


def rtl92c_init_adaptive_ctrl(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:418."""
    io = rtlpriv.io
    value32 = io.read32(C.REG_RRSR)
    value32 &= ~C.RATE_BITMAP_ALL
    value32 |= C.RATE_RRSR_CCK_ONLY_1M
    io.write32(C.REG_RRSR, value32)
    value16 = C._SPEC_SIFS_CCK(0x10) | C._SPEC_SIFS_OFDM(0x10)
    io.write16(C.REG_SPEC_SIFS, value16)
    value16 = C._LRL(0x30) | C._SRL(0x30)
    io.write32(C.REG_RL, value16)


def rtl92c_init_rate_fallback(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:437."""
    io = rtlpriv.io
    io.write32(C.REG_DARFRC, 0x00000000)
    io.write32(C.REG_DARFRC + 4, 0x10080404)
    io.write32(C.REG_RARFRC, 0x04030201)
    io.write32(C.REG_RARFRC + 4, 0x08070605)


def rtl92c_set_cck_sifs(rtlpriv: RtlPriv, trx_sifs: int, ctx_sifs: int) -> None:
    rtlpriv.io.write8(C.REG_SIFS_CCK, trx_sifs)
    rtlpriv.io.write8(C.REG_SIFS_CCK + 1, ctx_sifs)


def rtl92c_set_ofdm_sifs(rtlpriv: RtlPriv, trx_sifs: int, ctx_sifs: int) -> None:
    rtlpriv.io.write8(C.REG_SIFS_OFDM, trx_sifs)
    rtlpriv.io.write8(C.REG_SIFS_OFDM + 1, ctx_sifs)


def rtl92c_init_edca(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:483."""
    io = rtlpriv.io
    value16 = io.read16(C.REG_RD_CTRL)
    value16 |= C.DIS_EDCA_CNT_DWN
    io.write16(C.REG_RD_CTRL, value16)
    rtl92c_set_cck_sifs(rtlpriv, 0xa, 0xa)
    rtl92c_set_ofdm_sifs(rtlpriv, 0xe, 0xe)
    io.write16(C.REG_SIFS_CCK, 0x0a0a)
    io.write16(C.REG_SIFS_OFDM, 0x1010)
    io.write16(C.REG_PROT_MODE_CTRL, 0x0204)
    io.write32(C.REG_BAR_MODE_CTRL, 0x014004)
    io.write32(C.REG_EDCA_BE_PARAM, 0x005EA42B)
    io.write32(C.REG_EDCA_BK_PARAM, 0x0000A44F)
    io.write32(C.REG_EDCA_VI_PARAM, 0x005EA324)
    io.write32(C.REG_EDCA_VO_PARAM, 0x002FA226)
    io.write8(C.REG_PIFS, 0x1C)
    io.write8(C.REG_AGGR_BREAK_TIME, 0x16)
    io.write16(C.REG_NAV_PROT_LEN, 0x0040)
    io.write8(C.REG_BCNDMATIM, 0x02)
    io.write8(C.REG_ATIMWND, 0x02)


def rtl92c_init_ampdu_aggregation(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:515; 0x4CA is unnamed in the source."""
    io = rtlpriv.io
    io.write32(C.REG_AGGLEN_LMT, 0x99997631)
    io.write8(C.REG_AGGR_BREAK_TIME, 0x16)
    io.write16(0x4CA, 0x0708)


def rtl92c_init_beacon_max_error(rtlpriv: RtlPriv) -> None:
    rtlpriv.io.write8(C.REG_BCN_MAX_ERR, 0xFF)


def rtl92c_init_retry_function(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:541."""
    io = rtlpriv.io
    value8 = io.read8(C.REG_FWHW_TXQ_CTRL)
    value8 |= C.EN_AMPDU_RTY_NEW
    io.write8(C.REG_FWHW_TXQ_CTRL, value8)
    io.write8(C.REG_ACKTO, 0x40)


def rtl92c_set_min_space(rtlpriv: RtlPriv, is2T: bool) -> None:
    value = C.MAX_MSS_DENSITY_2T if is2T else C.MAX_MSS_DENSITY_1T
    rtlpriv.io.write8(C.REG_AMPDU_MIN_SPACE, value)


def _s8(value: int) -> int:
    return value - 0x100 if value & 0x80 else value


@dataclass(frozen=True)
class RxFwinfo92c:
    """struct rx_fwinfo_92c (mac.h:50), the PHY status after the RX descriptor; packed, 28 bytes."""
    gain_trsw: tuple[int, int, int, int]
    pwdb_all: int
    cfosho: tuple[int, int, int, int]
    cfotail: tuple[int, int, int, int]
    rxevm: tuple[int, int]
    rxsnr: tuple[int, int, int, int]

    @classmethod
    def from_bytes(cls, data: bytes) -> "RxFwinfo92c":
        data = bytes(data[:28]).ljust(28, b"\x00")
        return cls(gain_trsw=(data[0], data[1], data[2], data[3]), pwdb_all=data[4],
                   cfosho=(data[5], data[6], data[7], data[8]), cfotail=(data[9], data[10], data[11], data[12]),
                   rxevm=(_s8(data[13]), _s8(data[14])),
                   rxsnr=(_s8(data[15]), _s8(data[16]), _s8(data[17]), _s8(data[18])))


@dataclass(frozen=True)
class PhyStsCck8192sT:
    """struct phy_sts_cck_8192s_t (rtl8192ce/def.h:101), the same bytes read as a CCK report."""
    adc_pwdb_X: tuple[int, int, int, int]
    sq_rpt: int
    cck_agc_rpt: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "PhyStsCck8192sT":
        data = bytes(data[:6]).ljust(6, b"\x00")
        return cls(adc_pwdb_X=(data[0], data[1], data[2], data[3]), sq_rpt=data[4], cck_agc_rpt=data[5])


def RX_HAL_IS_CCK_RATE(rxmcs: int) -> bool:
    """wifi.h:367."""
    return rxmcs in (C.DESC_RATE1M, C.DESC_RATE2M, C.DESC_RATE5_5M, C.DESC_RATE11M)


def _rtl92c_query_rxphystatus(rtlpriv: RtlPriv, pstats: RtlStats, rxmcs: int, drvinfo: bytes,
                              packet_match_bssid: bool, packet_toself: bool, packet_beacon: bool) -> None:
    """mac.c:570. ``rxmcs`` is the descriptor's rx_desc_92c.rxmcs (word 3 [5:0])."""
    rtlphy = rtlpriv.phy
    p_drvinfo = RxFwinfo92c.from_bytes(drvinfo)
    in_powersavemode = False
    rx_pwr_all = 0
    rx_pwr = [0, 0, 0, 0]
    rf_rx_num = 0
    total_rssi = 0
    pwdb_all = 0
    is_cck_rate = RX_HAL_IS_CCK_RATE(rxmcs)
    pstats.packet_matchbssid = packet_match_bssid
    pstats.packet_toself = packet_toself
    pstats.packet_beacon = packet_beacon
    pstats.is_cck = is_cck_rate
    pstats.rx_mimo_sig_qual[0] = -1
    pstats.rx_mimo_sig_qual[1] = -1
    if is_cck_rate:
        cck_buf = PhyStsCck8192sT.from_bytes(drvinfo)
        cck_highpwr = rtlphy.cck_high_power if not in_powersavemode else False
        cck_agc_rpt = cck_buf.cck_agc_rpt
        if not cck_highpwr:
            report = (cck_buf.cck_agc_rpt & 0xc0) >> 6
            if report == 0x3:
                rx_pwr_all = -46 - (cck_agc_rpt & 0x3e)
            elif report == 0x2:
                rx_pwr_all = -26 - (cck_agc_rpt & 0x3e)
            elif report == 0x1:
                rx_pwr_all = -12 - (cck_agc_rpt & 0x3e)
            else:
                rx_pwr_all = 16 - (cck_agc_rpt & 0x3e)
        else:
            report = (p_drvinfo.cfosho[0] & 0x60) >> 5
            if report == 0x3:
                rx_pwr_all = -46 - ((cck_agc_rpt & 0x1f) << 1)
            elif report == 0x2:
                rx_pwr_all = -26 - ((cck_agc_rpt & 0x1f) << 1)
            elif report == 0x1:
                rx_pwr_all = -12 - ((cck_agc_rpt & 0x1f) << 1)
            else:
                rx_pwr_all = 16 - ((cck_agc_rpt & 0x1f) << 1)
        pwdb_all = rtl_query_rxpwrpercentage(rx_pwr_all)
        pstats.rx_pwdb_all = pwdb_all
        pstats.recvsignalpower = rx_pwr_all
        if packet_match_bssid:
            if pstats.rx_pwdb_all > 40:
                sq = 100
            else:
                sq = cck_buf.sq_rpt
                if sq > 64:
                    sq = 0
                elif sq < 20:
                    sq = 100
                else:
                    sq = ((64 - sq) * 100) // 44
            pstats.signalquality = sq
            pstats.rx_mimo_sig_qual[0] = sq
            pstats.rx_mimo_sig_qual[1] = -1
    else:
        rtlpriv.dm.rfpath_rxenable[0] = rtlpriv.dm.rfpath_rxenable[1] = True
        for i in range(C.RF90_PATH_A, C.RF90_PATH_MAX):
            if rtlpriv.dm.rfpath_rxenable[i]:
                rf_rx_num += 1
            rx_pwr[i] = ((p_drvinfo.gain_trsw[i] & 0x3f) * 2) - 110
            rssi = rtl_query_rxpwrpercentage(rx_pwr[i])
            total_rssi += rssi
            rtlpriv.stats.rx_snr_db[i] = int(p_drvinfo.rxsnr[i] / 2)
            if packet_match_bssid:
                pstats.rx_mimo_signalstrength[i] = rssi
        rx_pwr_all = ((p_drvinfo.pwdb_all >> 1) & 0x7f) - 110
        pwdb_all = rtl_query_rxpwrpercentage(rx_pwr_all)
        pstats.rx_pwdb_all = pwdb_all
        pstats.rxpower = rx_pwr_all
        pstats.recvsignalpower = rx_pwr_all
        if rxmcs and C.DESC_RATEMCS8 <= rxmcs <= C.DESC_RATEMCS15:
            max_spatial_stream = 2
        else:
            max_spatial_stream = 1
        for i in range(max_spatial_stream):
            evm = rtl_evm_db_to_percentage(p_drvinfo.rxevm[i])
            if packet_match_bssid:
                if i == 0:
                    pstats.signalquality = evm & 0xff
                pstats.rx_mimo_sig_qual[i] = evm & 0xff
    if is_cck_rate:
        pstats.signalstrength = rtl_signal_scale_mapping(pwdb_all) & 0xFF
    elif rf_rx_num != 0:
        total_rssi //= rf_rx_num
        pstats.signalstrength = rtl_signal_scale_mapping(total_rssi) & 0xFF


def rtl92c_translate_rx_signal_stuff(rtlpriv: RtlPriv, skb: bytes, pstats: RtlStats, rxmcs: int,
                                     drvinfo: bytes) -> None:
    """mac.c:710. ``skb`` starts after the RX descriptor, as in the source."""
    mac = rtlpriv.mac80211
    rtlefuse = rtlpriv.efuse
    tmp_buf = skb[pstats.rx_drvinfo_size + pstats.rx_bufshift:]
    cpu_fc = int.from_bytes(tmp_buf[0:2], "little")
    type = cpu_fc & C.IEEE80211_FCTL_FTYPE
    addr1, addr2, addr3 = bytes(tmp_buf[4:10]), bytes(tmp_buf[10:16]), bytes(tmp_buf[16:22])
    if cpu_fc & C.IEEE80211_FCTL_TODS:
        bssid_addr = addr1
    elif cpu_fc & C.IEEE80211_FCTL_FROMDS:
        bssid_addr = addr2
    else:
        bssid_addr = addr3
    packet_matchbssid = (C.IEEE80211_FTYPE_CTL != type and mac.bssid == bssid_addr
                         and not pstats.hwerror and not pstats.crc and not pstats.icv)
    packet_toself = packet_matchbssid and addr1 == rtlefuse.dev_addr
    packet_beacon = (cpu_fc & (C.IEEE80211_FCTL_FTYPE | C.IEEE80211_FCTL_STYPE)) == (
        C.IEEE80211_FTYPE_MGMT | C.IEEE80211_STYPE_BEACON)
    _rtl92c_query_rxphystatus(rtlpriv, pstats, rxmcs, drvinfo, packet_matchbssid, packet_toself, packet_beacon)
    rtl_process_phyinfo(rtlpriv, pstats)
