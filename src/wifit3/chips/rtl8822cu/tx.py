"""RTL8822CU management-frame TX over the rtw88 48-byte descriptor."""
from __future__ import annotations

import struct
import usb.core

from wifit3.chips.rtw88_base.registers import (
    DESC_RATE1M, DESC_RATE6M, RTW_DMA_MAPPING_HIGH, RTW_DMA_MAPPING_NORMAL,
    TX_DESC_QSEL_BEACON, TX_DESC_QSEL_H2C, TX_DESC_QSEL_HIGH, TX_DESC_QSEL_MGMT,
)
from wifit3.chips.rtw88_base.tx_common import fill_txdesc_checksum, pick_bulk_out_ep as _pick

TX_PKT_DESC_SZ = 48
RTW_RATEID_B_20M = 8
RTW_RATEID_G = 7

# Inject-frame pattrib defaults from update_monitor_frame_attrib [SRC core/rtw_mlme_ext.c:6883-6898].
INJECT_MACID = 1                # RTW_DEFAULT_MGMT_MACID [SRC include/drv_types.h:939]
INJECT_RATE_ID = 9             # RATEID_IDX_VHT_2SS [SRC include/ieee80211.h:249]


def _fill_txdesc_checksum48(desc: bytearray) -> None:
    # W7[15:0] = XOR of all 24 u16 halfwords over the full 48-byte descriptor, field zeroed
    # first [SRC hal/halmac/halmac_88xx/halmac_8822c/halmac_common_8822c.c:184-196].
    struct.pack_into("<H", desc, 7 * 4, 0)
    chksum = 0
    for i in range(TX_PKT_DESC_SZ // 2):
        chksum ^= struct.unpack_from("<H", desc, i * 2)[0]
    struct.pack_into("<H", desc, 7 * 4, chksum & 0xFFFF)


def build_tx_desc_inject(mpdu: bytes, *, band_is_2g: bool = True) -> bytes:
    """Inject-branch (pattrib->inject == 0xa5) TX descriptor for a raw 802.11 frame
    [SRC hal/rtl8822c/usb/rtl8822cu_xmit.c:117-145]. The chip does not re-stamp the sequence."""
    if len(mpdu) < 10:
        raise ValueError(f"MPDU too short ({len(mpdu)} bytes)")
    bmc = bool(mpdu[4] & 1)
    # DATARATE = MRateToHwRate(pattrib->rate) [SRC rtl8822cu_xmit.c:139]. For a raw monitor
    # inject rtw_monitor_xmit_entry defaults fixed_rate = MGN_1M and only a radiotap
    # RATE/MCS/VHT field overrides it, then copies it into pattrib->rate
    # [SRC core/rtw_xmit.c:4876,4931-4988,5014]. wifit3 injects the bare MPDU with no radiotap,
    # so fixed_rate stays MGN_1M -> MRateToHwRate(MGN_1M) = DESC_RATE1M = 0
    # [SRC hal/hal_com.c:447,533-538]. The vendor default is unconditional across bands (it
    # emits CCK 1M on 5 GHz too); band_is_2g is therefore unused here on purpose. Whether wifit3
    # should instead inject a 5 GHz-valid basic rate is a derv decision: see NEEDS-DERV INJ-1.
    rate = DESC_RATE1M
    # retry_ctrl = (txflags & 0x08 NOACK) ? _FALSE : _TRUE [SRC core/rtw_xmit.c:5019]. wifit3 has
    # no radiotap TX_FLAGS so txflags = 0 -> retry_ctrl = _TRUE -> RTS_DATA_RTY_LMT = 6
    # [SRC rtl8822cu_xmit.c:122-128].
    rts_data_rty_lmt = 6
    # w0: TXPKTSIZE, OFFSET=48, BMC, LS; OWN/DISQSELSEQ stay clear [SRC rtl8822cu_xmit.c:66-81].
    w0 = len(mpdu) | (TX_PKT_DESC_SZ << 16) | (int(bmc) << 24) | (1 << 26)
    # w1: MACID / QSEL / RATE_ID from the inject pattrib [SRC rtl8822cu_xmit.c:97-100].
    w1 = INJECT_MACID | (TX_DESC_QSEL_MGMT << 8) | (INJECT_RATE_ID << 16)
    # w3: USE_RATE + DISRTSFB + DISDATAFB [SRC rtl8822cu_xmit.c:135-138].
    w3 = (1 << 8) | (1 << 9) | (1 << 10)
    # w4: DATARATE [+0x10 b0..6] + RTY_LMT_EN [b17] + RTS_DATA_RTY_LMT [b18..23]
    # [SRC hal/halmac/halmac_tx_desc_nic.h:597-617; rtl8822cu_xmit.c:122-139].
    w4 = (rate & 0x7F) | (1 << 17) | ((rts_data_rty_lmt & 0x3F) << 18)
    # w8 EN_HWSEQ stays 0; w9 SW_SEQ is the frame's own seq_ctl [SRC rtl8822cu_xmit.c:119-120].
    seqnum = (struct.unpack_from("<H", mpdu, 22)[0] >> 4) & 0xFFF if len(mpdu) >= 24 else 0
    w9 = seqnum << 12
    desc = bytearray(struct.pack("<12I", w0, w1, 0, w3, w4, 0, 0, 0, 0, w9, 0, 0))
    _fill_txdesc_checksum48(desc)
    return bytes(desc)


def build_tx_desc_mgmt(mpdu: bytes, *, band_is_2g: bool = True,
                       retry_limit: int | None = None) -> bytes:
    if len(mpdu) < 10:
        raise ValueError(f"MPDU too short ({len(mpdu)} bytes)")
    bmc = bool(mpdu[4] & 1)
    rate = DESC_RATE1M if band_is_2g else DESC_RATE6M
    rate_id = RTW_RATEID_B_20M if band_is_2g else RTW_RATEID_G
    w0 = len(mpdu) | (TX_PKT_DESC_SZ << 16) | (int(bmc) << 24) | (1 << 26) | (1 << 31)
    w1 = (TX_DESC_QSEL_MGMT << 8) | (rate_id << 16)
    w3 = (1 << 8) | (1 << 10)
    w4 = rate & 0x7F
    if retry_limit is not None:
        w4 |= (1 << 17) | ((retry_limit & 0x3F) << 18)
    desc = bytearray(struct.pack("<12I", w0, w1, 0, w3, w4, 0, 0, 0, 1 << 15, 0, 0, 0))
    fill_txdesc_checksum(desc)
    return bytes(desc)

def pick_bulk_out_ep(out_ep_addrs: list[int], queue: int = TX_DESC_QSEL_MGMT) -> int:
    dma = RTW_DMA_MAPPING_HIGH if queue in (TX_DESC_QSEL_BEACON, TX_DESC_QSEL_HIGH,
                                             TX_DESC_QSEL_MGMT, TX_DESC_QSEL_H2C) else RTW_DMA_MAPPING_NORMAL
    return _pick(out_ep_addrs, dma)

def write_bulk(dev: usb.core.Device, ep: int, payload: bytes, *, timeout_ms: int = 200) -> int:
    return int(dev.write(ep, payload, timeout_ms))
