"""Port of the rtl8xxxu core.c RX path (v6.19.14) for the RTL8192FU: rxdesc24 + jaguar2 PHY status."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import usb.core

from . import constants as C
from .core import (
    RF_A,
    RF_B,
    Nl80211Iftype,
    Rtl8xxxuPriv,
    rtl8xxxu_gen2_h2c_cmd,
    rtl8xxxu_read32,
    rtl8xxxu_write8,
    rtl8xxxu_write32,
)

RTL8XXXU_RXDESC24_SIZE = 24  # sizeof(struct rtl8xxxu_rxdesc24), rtl8xxxu.h:275
IEEE80211_MAX_FRAME_LEN = 2352  # include/linux/ieee80211.h
RTL8XXXU_RX_URB_SIZE = IEEE80211_MAX_FRAME_LEN + RTL8XXXU_RXDESC24_SIZE  # core.c:6540, rx_buf_aggregation=0

C2H_8723B_BT_INFO = 9  # rtl8xxxu.h:1542 enum c2h_evt_8723b
C2H_8723B_RA_REPORT = 0x0C
H2C_8723B_B_TYPE_TDMA = 0x60  # rtl8xxxu.h:1394 enum h2c_cmd_8723b
BT_8723B_1ANT_STATUS_NON_CONNECTED_IDLE = 0x0  # rtl8xxxu.h:1704 enum bt_8723b_1ant_status
BT_8723B_1ANT_STATUS_CONNECTED_IDLE = 0x1
BT_8723B_1ANT_STATUS_ACL_BUSY = 0x3
BT_8723B_1ANT_STATUS_SCO_BUSY = 0x4
BT_8723B_1ANT_STATUS_MAX = 0x6


@dataclass(frozen=True)
class Rtl8xxxuRxdesc24:
    """struct rtl8xxxu_rxdesc24 (rtl8xxxu.h:275), the fields parse_rxdesc24 reads."""
    pktlen: int
    crc32: bool
    icverr: bool
    drvinfo_sz: int
    security: int
    shift: int
    phy_stats: bool
    swdec: bool
    rpt_sel: bool
    rxmcs: int
    bw: int
    tsfl: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "Rtl8xxxuRxdesc24":
        dw0, _dw1, dw2, dw3, dw4, dw5 = (
            int.from_bytes(data[i:i + 4], "little") for i in range(0, RTL8XXXU_RXDESC24_SIZE, 4))
        return cls(
            pktlen=dw0 & 0x3FFF,
            crc32=bool(dw0 & C.BIT(14)),
            icverr=bool(dw0 & C.BIT(15)),
            drvinfo_sz=(dw0 >> 16) & 0xF,
            security=(dw0 >> 20) & 0x7,
            shift=(dw0 >> 24) & 0x3,
            phy_stats=bool(dw0 & C.BIT(26)),
            swdec=bool(dw0 & C.BIT(27)),
            rpt_sel=bool(dw2 & C.BIT(28)),
            rxmcs=dw3 & 0x7F,
            bw=(dw4 >> 4) & 0x3,
            tsfl=dw5,
        )


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


def _is_ctl(frame: bytes) -> bool:
    return (frame[0] & 0x0C) == 0x04


def rtl8xxxu_is_packet_match_bssid(priv: Rtl8xxxuPriv, frame: bytes) -> bool:
    return priv.vif_type == Nl80211Iftype.NL80211_IFTYPE_STATION and priv.is_associated and \
        priv.bss_bssid == frame[10:16]


def rtl8192f_cck_rssi(priv: Rtl8xxxuPriv, phy_stats: bytes) -> int:
    """8192f.c:1967, on struct jaguar2_phy_stats_type0."""
    lna_idx = ((phy_stats[14] >> 7) << 3) | (phy_stats[13] >> 5)
    vga_idx = phy_stats[13] & 0x1F
    if lna_idx == 7:
        rx_pwr_all = -44 - (2 * vga_idx)
    elif lna_idx == 5:
        rx_pwr_all = -28 - (2 * vga_idx)
    elif lna_idx == 3:
        rx_pwr_all = -10 - (2 * vga_idx)
    elif lna_idx == 0:
        rx_pwr_all = 14 - (2 * vga_idx)
    else:
        rx_pwr_all = 0
    return _s8(rx_pwr_all)


def jaguar2_rx_parse_phystats_type0(priv: Rtl8xxxuPriv, phy_stats0: bytes) -> int:
    rx_power = _s8(phy_stats0[1] - 110)
    if not priv.cck_new_agc:
        rx_power = rtl8192f_cck_rssi(priv, phy_stats0)
    return rx_power


def jaguar2_rx_parse_phystats_type1(priv: Rtl8xxxuPriv, phy_stats1: bytes, frame: bytes,
                                    crc_icv_err: bool) -> int:
    parse_cfo = not crc_icv_err and not _is_ctl(frame) and rtl8xxxu_is_packet_match_bssid(priv, frame)
    if parse_cfo:
        priv.cfo_tracking.cfo_tail[RF_A] = _s8(phy_stats1[20 + RF_A])
        priv.cfo_tracking.cfo_tail[RF_B] = _s8(phy_stats1[20 + RF_B])
        priv.cfo_tracking.packet_count += 1

    pwdb_max = max(phy_stats1[1 + rx_path] for rx_path in range(priv.rx_paths))
    return _s8(pwdb_max - 110)


def jaguar2_rx_parse_phystats_type2(priv: Rtl8xxxuPriv, phy_stats2: bytes) -> int:
    pwdb_max = max(phy_stats2[1 + rx_path] for rx_path in range(priv.rx_paths))
    return _s8(pwdb_max - 110)


def jaguar2_rx_parse_phystats(priv: Rtl8xxxuPriv, phy_stats: bytes, frame: bytes,
                              crc_icv_err: bool) -> int:
    """rx_status->signal; 0 (the memset value) for an unknown page, as the C."""
    page_num = phy_stats[0]
    if page_num == 0:
        return jaguar2_rx_parse_phystats_type0(priv, phy_stats)
    if page_num == 1:
        return jaguar2_rx_parse_phystats_type1(priv, phy_stats, frame, crc_icv_err)
    if page_num == 2:
        return jaguar2_rx_parse_phystats_type2(priv, phy_stats)
    return 0


def rtl8723bu_handle_c2h(priv: Rtl8xxxuPriv, skb: bytes) -> None:
    priv.c2hcmd_queue.append(skb)


def rtl8xxxu_parse_rxdesc24(priv: Rtl8xxxuPriv, skb: bytes) -> Iterator[tuple[bytes, int | None]]:
    """(frame, signal dBm or None for RX_FLAG_NO_SIGNAL_VAL); C2H packets go to priv.c2hcmd_queue.

    Frames with crc32 set are not yielded: mac80211 drops RX_FLAG_FAILED_FCS_CRC on a
    monitor without MONITOR_FLAG_FCSFAIL (net/mac80211/rx.c ieee80211_rx_monitor).
    """
    urb_len = len(skb)
    if urb_len < RTL8XXXU_RXDESC24_SIZE:
        return

    offset = 0
    while True:
        rx_desc = Rtl8xxxuRxdesc24.from_bytes(skb[offset:offset + RTL8XXXU_RXDESC24_SIZE])
        pkt_len = rx_desc.pktlen
        drvinfo_sz = rx_desc.drvinfo_sz * 8
        desc_shift = rx_desc.shift
        pkt_offset = (pkt_len + drvinfo_sz + desc_shift + RTL8XXXU_RXDESC24_SIZE + 7) // 8 * 8
        has_next = urb_len >= pkt_offset + RTL8XXXU_RXDESC24_SIZE

        phy_stats_start = offset + RTL8XXXU_RXDESC24_SIZE
        frame_start = phy_stats_start + drvinfo_sz + desc_shift
        frame = skb[frame_start:frame_start + pkt_len]

        if rx_desc.rpt_sel:
            rtl8723bu_handle_c2h(priv, frame)
        elif rx_desc.phy_stats:
            signal = jaguar2_rx_parse_phystats(
                priv, skb[phy_stats_start:frame_start], frame, rx_desc.crc32 or rx_desc.icverr)
            if not rx_desc.crc32:
                yield frame, signal
        elif not rx_desc.crc32:
            yield frame, None

        if not has_next:
            return
        offset += pkt_offset
        urb_len -= pkt_offset
        if urb_len < RTL8XXXU_RXDESC24_SIZE:
            return


def rtl8723bu_set_ps_tdma(priv: Rtl8xxxuPriv, arg1: int, arg2: int, arg3: int, arg4: int, arg5: int) -> None:
    rtl8xxxu_gen2_h2c_cmd(priv, bytes((H2C_8723B_B_TYPE_TDMA, arg1, arg2, arg3, arg4, arg5)))


_COEX_TABLES = {
    0: (0x55555555, 0x55555555),
    1: (0x55555555, 0x5A5A5A5A),
    2: (0x5A5A5A5A, 0x5A5A5A5A),
    3: (0x55555555, 0x5A5A5A5A),
    4: (0x5A5A5A5A, 0xAAAA5A5A),
    5: (0x5A5A5A5A, 0xAA5A5A5A),
    6: (0x55555555, 0xAAAAAAAA),
    7: (0xAAAAAAAA, 0xAAAAAAAA),
}


def rtl8723bu_set_coex_with_type(priv: Rtl8xxxuPriv, coex_type: int) -> None:
    if coex_type not in _COEX_TABLES:
        return
    table1, table2 = _COEX_TABLES[coex_type]
    rtl8xxxu_write32(priv, C.REG_BT_COEX_TABLE1, table1)
    rtl8xxxu_write32(priv, C.REG_BT_COEX_TABLE2, table2)
    rtl8xxxu_write32(priv, C.REG_BT_COEX_TABLE3, 0x00FFFFFF)
    rtl8xxxu_write8(priv, C.REG_BT_COEX_TABLE4, 0x03)


def rtl8723bu_update_bt_link_info(priv: Rtl8xxxuPriv, bt_info: int) -> None:
    btcoex = priv.bt_coex
    btcoex.c2h_bt_inquiry = bool(bt_info & C.BT_INFO_8723B_1ANT_B_INQ_PAGE)

    if not bt_info & C.BT_INFO_8723B_1ANT_B_CONNECTION:
        btcoex.bt_status = BT_8723B_1ANT_STATUS_NON_CONNECTED_IDLE
        btcoex.has_sco = False
        btcoex.has_hid = False
        btcoex.has_pan = False
        btcoex.has_a2dp = False
    else:
        if (bt_info & 0x1F) == C.BT_INFO_8723B_1ANT_B_CONNECTION:
            btcoex.bt_status = BT_8723B_1ANT_STATUS_CONNECTED_IDLE
        elif bt_info & C.BT_INFO_8723B_1ANT_B_SCO_ESCO or bt_info & C.BT_INFO_8723B_1ANT_B_SCO_BUSY:
            btcoex.bt_status = BT_8723B_1ANT_STATUS_SCO_BUSY
        elif bt_info & C.BT_INFO_8723B_1ANT_B_ACL_BUSY:
            btcoex.bt_status = BT_8723B_1ANT_STATUS_ACL_BUSY
        else:
            btcoex.bt_status = BT_8723B_1ANT_STATUS_MAX
        btcoex.has_pan = bool(bt_info & C.BT_INFO_8723B_1ANT_B_FTP)
        btcoex.has_a2dp = bool(bt_info & C.BT_INFO_8723B_1ANT_B_A2DP)
        btcoex.has_hid = bool(bt_info & C.BT_INFO_8723B_1ANT_B_HID)
        btcoex.has_sco = bool(bt_info & C.BT_INFO_8723B_1ANT_B_SCO_ESCO)

    btcoex.hid_only = not btcoex.has_a2dp and not btcoex.has_sco and not btcoex.has_pan and btcoex.has_hid
    # sic: core.c:5974 assigns has_a2dp, not a2dp_only
    btcoex.has_a2dp = not btcoex.has_sco and not btcoex.has_pan and not btcoex.has_hid and btcoex.has_a2dp
    btcoex.bt_busy = btcoex.bt_status in (BT_8723B_1ANT_STATUS_SCO_BUSY, BT_8723B_1ANT_STATUS_ACL_BUSY)


def rtl8723bu_handle_bt_inquiry(priv: Rtl8xxxuPriv) -> None:
    btcoex = priv.bt_coex
    if not priv.is_associated:
        rtl8723bu_set_ps_tdma(priv, 0x8, 0x0, 0x0, 0x0, 0x0)
        rtl8723bu_set_coex_with_type(priv, 0)
    elif btcoex.has_sco or btcoex.has_hid or btcoex.has_a2dp:
        rtl8723bu_set_ps_tdma(priv, 0x61, 0x35, 0x3, 0x11, 0x11)
        rtl8723bu_set_coex_with_type(priv, 4)
    elif btcoex.has_pan:
        rtl8723bu_set_ps_tdma(priv, 0x61, 0x3F, 0x3, 0x11, 0x11)
        rtl8723bu_set_coex_with_type(priv, 4)
    else:
        rtl8723bu_set_ps_tdma(priv, 0x8, 0x0, 0x0, 0x0, 0x0)
        rtl8723bu_set_coex_with_type(priv, 7)


def rtl8723bu_handle_bt_info(priv: Rtl8xxxuPriv) -> None:
    btcoex = priv.bt_coex
    if not priv.is_associated:
        rtl8723bu_set_ps_tdma(priv, 0x8, 0x0, 0x0, 0x0, 0x0)
        rtl8723bu_set_coex_with_type(priv, 0)
        return

    # TODO: verify, untested here, needs an associated vif (core.c:6020); wifit3 never associates.
    val32 = rtl8xxxu_read32(priv, 0x770)  # core.c:6024, no named register
    high_prio_tx = val32 & 0x0000FFFF
    high_prio_rx = (val32 & 0xFFFF0000) >> 16

    if btcoex.bt_busy:
        if btcoex.hid_only:
            rtl8723bu_set_ps_tdma(priv, 0x61, 0x20, 0x3, 0x11, 0x11)
            rtl8723bu_set_coex_with_type(priv, 5)
        elif btcoex.a2dp_only:
            rtl8723bu_set_ps_tdma(priv, 0x61, 0x35, 0x3, 0x11, 0x11)
            rtl8723bu_set_coex_with_type(priv, 4)
        elif (btcoex.has_a2dp and btcoex.has_pan) or (btcoex.has_hid and btcoex.has_a2dp and btcoex.has_pan):
            rtl8723bu_set_ps_tdma(priv, 0x51, 0x21, 0x3, 0x10, 0x10)
            rtl8723bu_set_coex_with_type(priv, 4)
        elif btcoex.has_hid and btcoex.has_a2dp:
            rtl8723bu_set_ps_tdma(priv, 0x51, 0x21, 0x3, 0x10, 0x10)
            rtl8723bu_set_coex_with_type(priv, 3)
        else:
            rtl8723bu_set_ps_tdma(priv, 0x61, 0x35, 0x3, 0x11, 0x11)
            rtl8723bu_set_coex_with_type(priv, 4)
    else:
        rtl8723bu_set_ps_tdma(priv, 0x8, 0x0, 0x0, 0x0, 0x0)
        if high_prio_tx + high_prio_rx <= 60:
            rtl8723bu_set_coex_with_type(priv, 2)
        else:
            rtl8723bu_set_coex_with_type(priv, 7)


def rtl8xxxu_c2hcmd_callback(priv: Rtl8xxxuPriv) -> None:
    """The c2hcmd_work body; C2H_8723B_RA_REPORT only feeds sta_statistics (no register I/O)."""
    while priv.c2hcmd_queue:
        c2h = priv.c2hcmd_queue.pop(0)
        if len(c2h) > 3 and c2h[0] == C2H_8723B_BT_INFO:
            rtl8723bu_update_bt_link_info(priv, c2h[3])
            if priv.bt_coex.c2h_bt_inquiry:
                rtl8723bu_handle_bt_inquiry(priv)
            else:
                rtl8723bu_handle_bt_info(priv)


def read_burst(dev: usb.core.Device, endpoint: int, timeout_ms: int = 100) -> bytes | None:
    """One bulk-IN URB (rtl8xxxu_submit_rx_urb's skb_size); None on timeout."""
    try:
        return bytes(dev.read(endpoint, RTL8XXXU_RX_URB_SIZE, timeout_ms))
    except usb.core.USBTimeoutError:
        return None
    except usb.core.USBError as error:
        if error.errno in (110, 10060):  # ETIMEDOUT / WSAETIMEDOUT
            return None
        raise
