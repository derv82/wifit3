from __future__ import annotations

import errno

from . import constants as C
from .base import (
    ieee80211_get_DA,
    ieee80211_is_beacon,
    ieee80211_is_data_qos,
    ieee80211_is_mgmt,
    is_broadcast_ether_addr,
    is_multicast_ether_addr,
    rtl_get_tcb_desc,
    rtlwifi_rate_mapping,
)
from .mac import IS_NORMAL_CHIP, rtl92c_translate_rx_signal_stuff
from .priv import Ieee80211RxStatus, Ieee80211TxInfo, RtlPriv, RtlStats, RtlTcbDesc, RtlUsb


def configvertoutep(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:17, test-chip endpoint config."""
    rtlusb = rtlpriv.usb
    ep_nums = 0
    rtlusb.out_queue_sel = 0
    ep_cfg = rtlpriv.io.read8(C.REG_TEST_SIE_OPTIONAL)
    ep_cfg = (ep_cfg & C.USB_TEST_EP_MASK) >> C.USB_TEST_EP_SHIFT
    if ep_cfg in (0, 3):
        rtlusb.out_queue_sel = C.TX_SELE_HQ | C.TX_SELE_LQ
        ep_nums = 2
    elif ep_cfg in (1, 2):
        txqsele = rtlpriv.io.read8(C.REG_TEST_USB_TXQS)
        if txqsele & 0x0F:
            rtlusb.out_queue_sel = C.TX_SELE_HQ
        elif txqsele & 0xF0:
            rtlusb.out_queue_sel = C.TX_SELE_LQ
        ep_nums = 1
    return 0 if rtlusb.out_ep_nums == ep_nums else -errno.EINVAL


def configvernoutep(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:50, normal-chip endpoint config."""
    rtlusb = rtlpriv.usb
    ep_nums = 0
    rtlusb.out_queue_sel = 0
    ep_cfg = rtlpriv.io.read8(C.REG_NORMAL_SIE_EP + 1)
    if ep_cfg & C.USB_NORMAL_SIE_EP_MASK:
        rtlusb.out_queue_sel |= C.TX_SELE_HQ
        ep_nums += 1
    if (ep_cfg >> C.USB_NORMAL_SIE_EP_SHIFT) & C.USB_NORMAL_SIE_EP_MASK:
        rtlusb.out_queue_sel |= C.TX_SELE_NQ
        ep_nums += 1
    ep_cfg = rtlpriv.io.read8(C.REG_NORMAL_SIE_EP + 2)
    if ep_cfg & C.USB_NORMAL_SIE_EP_MASK:
        rtlusb.out_queue_sel |= C.TX_SELE_LQ
        ep_nums += 1
    return 0 if rtlusb.out_ep_nums == ep_nums else -errno.EINVAL


def twooutepmapping(rtlusb: RtlUsb, is_chip8: bool, bwificfg: bool) -> None:
    """rtl8192cu/trx.c:79."""
    ep_map = rtlusb.ep_mapping
    if bwificfg:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]
    else:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]


def threeoutepmapping(rtlusb: RtlUsb, bwificfg: bool) -> None:
    """rtl8192cu/trx.c:109."""
    ep_map = rtlusb.ep_mapping
    if bwificfg:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[2]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]
    else:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[2]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[2]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]


def oneoutepmapping(rtlusb: RtlUsb) -> None:
    """rtl8192cu/trx.c:139."""
    for queue in (C.RTL_TXQ_BE, C.RTL_TXQ_BK, C.RTL_TXQ_VI, C.RTL_TXQ_VO,
                  C.RTL_TXQ_MGT, C.RTL_TXQ_BCN, C.RTL_TXQ_HI):
        rtlusb.ep_mapping[queue] = rtlusb.out_eps[0]


def _out_ep_mapping(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:153. The 3-EP case passes ischipn as bwificfg, as the source does."""
    rtlusb = rtlpriv.usb
    bwificfg = False
    ischipn = IS_NORMAL_CHIP(rtlpriv.rtlhal.version)
    if rtlusb.out_ep_nums == 2:
        twooutepmapping(rtlusb, ischipn, bwificfg)
    elif rtlusb.out_ep_nums == 3:
        if not ischipn:
            return -errno.EINVAL
        threeoutepmapping(rtlusb, ischipn)
    elif rtlusb.out_ep_nums == 1:
        oneoutepmapping(rtlusb)
    else:
        return -errno.EINVAL
    return 0


def rtl8192cu_endpoint_mapping(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:188."""
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        error = configvernoutep(rtlpriv)
    else:
        error = configvertoutep(rtlpriv)
    if error:
        return error
    return _out_ep_mapping(rtlpriv)


def rtl8192cu_mq_to_hwq(fc: int, mac80211_queue_index: int) -> int:
    """trx.c:206."""
    if ieee80211_is_beacon(fc):
        return C.RTL_TXQ_BCN
    if ieee80211_is_mgmt(fc):
        return C.RTL_TXQ_MGT
    if mac80211_queue_index == 0:
        return C.RTL_TXQ_VO
    if mac80211_queue_index == 1:
        return C.RTL_TXQ_VI
    if mac80211_queue_index == 2:
        return C.RTL_TXQ_BE
    if mac80211_queue_index == 3:
        return C.RTL_TXQ_BK
    return C.RTL_TXQ_BE


def _rtl8192cu_mq_to_descq(fc: int, mac80211_queue_index: int) -> int:
    """trx.c:241."""
    if ieee80211_is_beacon(fc):
        return C.QSLT_BEACON
    if ieee80211_is_mgmt(fc):
        return C.QSLT_MGNT
    if mac80211_queue_index == 0:
        return C.QSLT_VO
    if mac80211_queue_index == 1:
        return C.QSLT_VI
    if mac80211_queue_index == 3:
        return C.QSLT_BK
    return C.QSLT_BE


def _le32_get_bits(desc: bytes, word: int, mask: int) -> int:
    value = int.from_bytes(desc[word * 4:word * 4 + 4], "little")
    return (value & mask) >> ((mask & -mask).bit_length() - 1)


def get_rx_desc_pkt_len(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 0, C.GENMASK(13, 0))


def get_rx_desc_crc32(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 0, C.BIT(14))


def get_rx_desc_icv(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 0, C.BIT(15))


def get_rx_desc_drvinfo_size(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 0, C.GENMASK(19, 16))


def get_rx_desc_shift(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 0, C.GENMASK(25, 24))


def get_rx_desc_phy_status(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 0, C.BIT(26))


def get_rx_desc_swdec(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 0, C.BIT(27))


def get_rx_desc_paggr(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 1, C.BIT(14))


def get_rx_desc_faggr(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 1, C.BIT(15))


def get_rx_desc_rx_mcs(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 3, C.GENMASK(5, 0))


def get_rx_desc_rx_ht(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 3, C.BIT(6))


def get_rx_desc_splcp(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 3, C.BIT(8))


def get_rx_desc_bw(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 3, C.BIT(9))


def get_rx_desc_tsfl(pdesc: bytes) -> int:
    return _le32_get_bits(pdesc, 5, 0xFFFFFFFF)


def rtl92cu_rx_query_desc(rtlpriv: RtlPriv, stats: RtlStats, rx_status: Ieee80211RxStatus,
                          pdesc: bytes, skb: bytes) -> bool:
    """trx.c:289. ``skb`` starts after the RX descriptor (usb.c pulls RTL_RX_DESC_SIZE first)."""
    phystatus = get_rx_desc_phy_status(pdesc)
    stats.length = get_rx_desc_pkt_len(pdesc)
    stats.rx_drvinfo_size = get_rx_desc_drvinfo_size(pdesc) * C.RX_DRV_INFO_SIZE_UNIT
    stats.rx_bufshift = get_rx_desc_shift(pdesc) & 0x03
    stats.icv = get_rx_desc_icv(pdesc)
    stats.crc = get_rx_desc_crc32(pdesc)
    stats.hwerror = stats.crc | stats.icv
    stats.decrypted = not get_rx_desc_swdec(pdesc)
    stats.rate = get_rx_desc_rx_mcs(pdesc)
    stats.shortpreamble = get_rx_desc_splcp(pdesc)
    stats.isampdu = get_rx_desc_paggr(pdesc) == 1
    stats.isfirst_ampdu = get_rx_desc_paggr(pdesc) == 1 and get_rx_desc_faggr(pdesc) == 1
    stats.timestamp_low = get_rx_desc_tsfl(pdesc)
    stats.rx_is40mhzpacket = bool(get_rx_desc_bw(pdesc))
    stats.is_ht = bool(get_rx_desc_rx_ht(pdesc))
    rx_status.freq = rtlpriv.conf_center_freq
    if get_rx_desc_crc32(pdesc):
        rx_status.failed_fcs_crc = True
    if not get_rx_desc_swdec(pdesc):
        rx_status.decrypted = True
    if get_rx_desc_bw(pdesc):
        rx_status.bw = C.RATE_INFO_BW_40
    if get_rx_desc_rx_ht(pdesc):
        rx_status.encoding = C.RX_ENC_HT
    if stats.decrypted:
        rx_status.decrypted = True
    rx_status.rate_idx = rtlwifi_rate_mapping(stats.is_ht, stats.rate)
    rx_status.mactime = get_rx_desc_tsfl(pdesc)
    if phystatus:
        p_drvinfo = skb[stats.rx_bufshift:]
        rtl92c_translate_rx_signal_stuff(rtlpriv, skb, stats, get_rx_desc_rx_mcs(pdesc), p_drvinfo)
    rx_status.signal = stats.recvsignalpower + 10
    return True


def _le32p_replace_bits(desc: bytearray, word: int, value: int, mask: int) -> None:
    offset = word * 4
    current = int.from_bytes(desc[offset:offset + 4], "little")
    shift = (mask & -mask).bit_length() - 1
    current = (current & ~mask) | ((value << shift) & mask)
    desc[offset:offset + 4] = current.to_bytes(4, "little")


def set_tx_desc_pkt_size(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.GENMASK(15, 0))


def set_tx_desc_offset(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.GENMASK(23, 16))


def set_tx_desc_bmc(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.BIT(24))


def set_tx_desc_htc(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.BIT(25))


def set_tx_desc_last_seg(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.BIT(26))


def set_tx_desc_first_seg(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.BIT(27))


def set_tx_desc_linip(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.BIT(28))


def set_tx_desc_own(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 0, value, C.BIT(31))


def set_tx_desc_macid(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.GENMASK(4, 0))


def set_tx_desc_agg_enable(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.BIT(5))


def set_tx_desc_agg_break(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.BIT(6))


def set_tx_desc_rdg_enable(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.BIT(7))


def set_tx_desc_queue_sel(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.GENMASK(12, 8))


def set_tx_desc_rate_id(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.GENMASK(19, 16))


def set_tx_desc_sec_type(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.GENMASK(23, 22))


def set_tx_desc_pkt_offset(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 1, value, C.GENMASK(30, 26))


def set_tx_desc_more_frag(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 2, value, C.BIT(17))


def set_tx_desc_seq(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 3, value, C.GENMASK(27, 16))


def set_tx_desc_pkt_id(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 3, value, C.GENMASK(31, 28))


def set_tx_desc_rts_rate(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.GENMASK(4, 0))


def set_tx_desc_hwseq_en(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(7))


def set_tx_desc_use_rate(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(8))


def set_tx_desc_disable_fb(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(10))


def set_tx_desc_cts2self(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(11))


def set_tx_desc_rts_enable(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(12))


def set_tx_desc_hw_rts_enable(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(13))


def set_tx_desc_data_sc(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.GENMASK(21, 20))


def set_tx_desc_data_bw(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(25))


def set_tx_desc_rts_short(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(26))


def set_tx_desc_rts_bw(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.BIT(27))


def set_tx_desc_rts_sc(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.GENMASK(29, 28))


def set_tx_desc_rts_stbc(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 4, value, C.GENMASK(31, 30))


def set_tx_desc_tx_rate(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 5, value, C.GENMASK(5, 0))


def set_tx_desc_data_shortgi(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 5, value, C.BIT(6))


def set_tx_desc_data_rate_fb_limit(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 5, value, C.GENMASK(12, 8))


def set_tx_desc_rts_rate_fb_limit(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 5, value, C.GENMASK(16, 13))


def set_tx_desc_tx_desc_checksum(txdesc: bytearray, value: int) -> None:
    _le32p_replace_bits(txdesc, 7, value, C.GENMASK(15, 0))


def _rtl_fill_usb_tx_desc(txdesc: bytearray) -> None:
    """trx.c:451."""
    set_tx_desc_own(txdesc, 1)
    set_tx_desc_last_seg(txdesc, 1)
    set_tx_desc_first_seg(txdesc, 1)


def _rtl_tx_desc_checksum(txdesc: bytearray) -> None:
    """trx.c:461: XOR of the first 16 little-endian u16s, with the checksum field cleared first."""
    set_tx_desc_tx_desc_checksum(txdesc, 0)
    checksum = 0
    for index in range(16):
        checksum ^= int.from_bytes(txdesc[index * 2:index * 2 + 2], "little")
    set_tx_desc_tx_desc_checksum(txdesc, checksum)


def rtl92cu_tx_fill_desc(rtlpriv: RtlPriv, skb: bytes, info: Ieee80211TxInfo, tcb_desc: RtlTcbDesc) -> bytes:
    """trx.c:474 with no STA, so A-MPDU aggregation stays off (agg_state RTL_AGG_STOP), and no
    hw_key (sec_type 0). Returns the skb after skb_push: descriptor, then the frame."""
    mac = rtlpriv.mac80211
    ppsc = rtlpriv.psc
    hdr = skb
    fc = int.from_bytes(hdr[0:2], "little")
    rate_flag = info.rate_flags
    pktlen = len(skb)
    fw_qsel = _rtl8192cu_mq_to_descq(fc, info.queue_mapping)
    seq_number = (int.from_bytes(hdr[22:24], "little") & C.IEEE80211_SCTL_SEQ) >> 4
    rtl_get_tcb_desc(rtlpriv, info, hdr, tcb_desc)
    txdesc = bytearray(C.RTL_TX_HEADER_SIZE)
    set_tx_desc_pkt_size(txdesc, pktlen)
    set_tx_desc_linip(txdesc, 0)
    set_tx_desc_pkt_offset(txdesc, C.RTL_DUMMY_OFFSET)
    set_tx_desc_offset(txdesc, C.RTL_TX_HEADER_SIZE)
    set_tx_desc_tx_rate(txdesc, tcb_desc.hw_rate)
    if tcb_desc.use_shortgi or tcb_desc.use_shortpreamble:
        set_tx_desc_data_shortgi(txdesc, 1)
    set_tx_desc_agg_break(txdesc, 1)
    set_tx_desc_seq(txdesc, seq_number)
    set_tx_desc_rts_enable(txdesc, 1 if tcb_desc.rts_enable and not tcb_desc.cts_enable else 0)
    set_tx_desc_hw_rts_enable(txdesc, 1 if tcb_desc.rts_enable or tcb_desc.cts_enable else 0)
    set_tx_desc_cts2self(txdesc, 1 if tcb_desc.cts_enable else 0)
    set_tx_desc_rts_stbc(txdesc, 1 if tcb_desc.rts_stbc else 0)
    set_tx_desc_rts_rate(txdesc, tcb_desc.rts_rate)
    set_tx_desc_rts_bw(txdesc, 0)
    set_tx_desc_rts_sc(txdesc, tcb_desc.rts_sc)
    if tcb_desc.rts_rate <= C.DESC_RATE54M:
        set_tx_desc_rts_short(txdesc, 1 if tcb_desc.rts_use_shortpreamble else 0)
    else:
        set_tx_desc_rts_short(txdesc, 1 if tcb_desc.rts_use_shortgi else 0)
    if mac.bw_40:
        if rate_flag & C.IEEE80211_TX_RC_DUP_DATA:
            set_tx_desc_data_bw(txdesc, 1)
            set_tx_desc_data_sc(txdesc, 3)
        elif rate_flag & C.IEEE80211_TX_RC_40_MHZ_WIDTH:
            set_tx_desc_data_bw(txdesc, 1)
            set_tx_desc_data_sc(txdesc, mac.cur_40_prime_sc)
        else:
            set_tx_desc_data_bw(txdesc, 0)
            set_tx_desc_data_sc(txdesc, 0)
    else:
        set_tx_desc_data_bw(txdesc, 0)
        set_tx_desc_data_sc(txdesc, 0)
    set_tx_desc_pkt_id(txdesc, 0)
    set_tx_desc_queue_sel(txdesc, fw_qsel)
    set_tx_desc_data_rate_fb_limit(txdesc, 0x1F)
    set_tx_desc_rts_rate_fb_limit(txdesc, 0xF)
    set_tx_desc_disable_fb(txdesc, 0)
    set_tx_desc_use_rate(txdesc, 1 if tcb_desc.use_driver_rate else 0)
    if ieee80211_is_data_qos(fc) and mac.rdg_en:
        set_tx_desc_rdg_enable(txdesc, 1)
        set_tx_desc_htc(txdesc, 1)
    if rtlpriv.dm.useramask:
        set_tx_desc_rate_id(txdesc, tcb_desc.ratr_index)
        set_tx_desc_macid(txdesc, tcb_desc.mac_id)
    else:
        set_tx_desc_rate_id(txdesc, 0xC + tcb_desc.ratr_index)
        set_tx_desc_macid(txdesc, tcb_desc.ratr_index)
    if not ieee80211_is_data_qos(fc) and ppsc.leisure_ps and ppsc.fwctrl_lps:
        set_tx_desc_hwseq_en(txdesc, 1)
        set_tx_desc_pkt_id(txdesc, 8)
    if fc & C.IEEE80211_FCTL_MOREFRAGS:
        set_tx_desc_more_frag(txdesc, 1)
    da = ieee80211_get_DA(hdr)
    if is_multicast_ether_addr(da) or is_broadcast_ether_addr(da):
        set_tx_desc_bmc(txdesc, 1)
    _rtl_fill_usb_tx_desc(txdesc)
    _rtl_tx_desc_checksum(txdesc)
    return bytes(txdesc) + bytes(skb)
