"""RTL8812AU (DKMS) TX descriptor: the monitor-injection path.

Ports `update_txdesc` [SRC] hal/rtl8812a/usb/rtl8812au_xmit.c:42 as `rtw_monitor_xmit_entry`
(core/rtw_xmit.c:4871) drives it: every injected frame is an alloc_mgtxmitframe frame
(MGNT_FRAMETAG, rtw_mlme_ext.c:7635), non-QoS, unencrypted.

Field bit positions [SRC] include/rtl8812a_xmit.h SET_TX_DESC_*_8812; queue/rate constants
[SRC] include/hal_com.h, include/ieee80211.h.
"""
from __future__ import annotations

import struct

from ..rtl88xxau_base.registers import TXDESC_SIZE

RTW_DEFAULT_MGMT_MACID = 1      # include/drv_types.h:886
QSLT_MGNT = 0x12
RATEID_IDX_G = 7
RATEID_IDX_B = 8
RATEID_IDX_VHT_2SS = 9          # the 8812's rf_type is RF_2T2R: rf_reg_trx_path_bmp 0x33 (rtl8812a_hal_init.c:3500)
DESC_RATE1M = 0x00
DESC_RATE6M = 0x04
DESC_RATE24M = 0x08
DESC_RATEVHTSS1MCS9 = 0x35
PACKET_OFFSET_SZ = 8            # include/rtw_xmit.h:239
# TODO: verify, untested here, needs a USB 3 host: IS_SUPER_SPEED_USB takes USB_SUPER_SPEED_BULK_SIZE 1024.
USB_HIGH_SPEED_BULK_SIZE = 512  # include/usb_ops.h:136
# rtw_monitor_xmit_entry sets retry_ctrl = _FALSE (rtw_xmit.c:4938): rtl8812au_xmit.c:302-306.
MGNT_DATA_RETRY_LIMIT = 12
TXBF_G_ID_BCMC = 63             # the bcmc stainfo's bf_info.g_id (rtw_sta_mgt.c:1012)


def txdesc_checksum(desc: bytes | bytearray) -> int:
    """[SRC] rtl8812a_cal_txdesc_chksum: XOR of the first 16 LE u16 words, its own field zeroed."""
    chk = 0
    for word in struct.unpack_from("<16H", desc, 0):
        chk ^= word
    return chk


def update_txdesc(frame_bytes: bytes, *, rate_id: int, hw_rate: int) -> bytes:
    """The bulk-OUT payload for one injected frame: the descriptor and the frame, as
    rtw_dump_xframe writes them (rtl8812au_xmit.c:481-491)."""
    pkt_len = len(frame_bytes)
    if pkt_len > 0xFFFF:
        raise ValueError("pkt_len must fit the 16-bit TX descriptor PKT_SIZE field")
    # rtl8812au_xmit.c:65-71, 97-102: off a bulk-size boundary the descriptor is pulled over the
    # packet offset, and pkt_offset (1, rtw_init_xmitframe rtw_xmit.c:3897) drops to 0.
    pull = (TXDESC_SIZE + pkt_len) % USB_HIGH_SPEED_BULK_SIZE != 0
    if pull:
        pkt_offset = 0
    else:
        pkt_offset = 1
    bmcst = frame_bytes[4] & 0x01 == 0x01   # IS_MCAST(pattrib->ra); ra = addr1 (rtw_mlme_ext.c:7840)
    # CONFIG_BEAMFORMING: txbf_g_id from rtw_get_stainfo(ra), only the bcmc stainfo exists.
    if bmcst:
        txbf_g_id = TXBF_G_ID_BCMC
    else:
        txbf_g_id = 0

    dw0 = pkt_len | (TXDESC_SIZE << 16) | (1 << 26) | (1 << 27) | (1 << 31)   # LAST_SEG, FIRST_SEG, OWN
    if bmcst:
        dw0 |= 1 << 24                                  # BMC
    dw1 = RTW_DEFAULT_MGMT_MACID | (QSLT_MGNT << 8) | ((rate_id & 0x1F) << 16) | (pkt_offset << 24)
    dw2 = txbf_g_id << 24                               # GID; PAID 0
    dw3 = 1 << 8                                        # USE_RATE
    dw4 = (hw_rate & 0x7F) | (1 << 17) | (MGNT_DATA_RETRY_LIMIT << 18)   # TX_RATE, RETRY_LIMIT_ENABLE
    dw8 = 1 << 15                                       # HWSEQ_EN (qos_en is _FALSE)
    desc = bytearray(struct.pack("<10I", dw0, dw1, dw2, dw3, dw4, 0, 0, 0, dw8, 0))
    struct.pack_into("<H", desc, 28, txdesc_checksum(desc))
    if pull:
        return bytes(desc) + frame_bytes
    return bytes(desc) + bytes(PACKET_OFFSET_SZ) + frame_bytes
