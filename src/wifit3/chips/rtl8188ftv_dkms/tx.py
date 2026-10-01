"""RTL8188FTV DKMS TX descriptor builder + bulk-OUT send (M8).

Ported from ``rtl8188f_fill_default_txdesc`` /
``rtl8188f_update_txdesc`` /
``rtl8188f_cal_txdesc_chksum``
(hal/rtl8188f/rtl8188f_hal_init.c) with the MGMT attrib rules from
``update_mgntframe_attrib`` (hal/hal_com.c + core/rtw_mlme_ext.c:
bcmc stainfo mac_id 1, QSLT_MGNT, 11B raid 8 at CCK 1M, HWSEQ_EN) and
the USB framing from ``update_txdesc`` /
``urb_zero_packet_chk`` /
``rtw_dump_xframe`` /
``ffaddr2pipehdl``
(hal/rtl8188f/usb/rtl8188fu_xmit.c, os_dep/linux/usb_ops_linux.c):
40B descriptor, checksum over the first 32 bytes with the checksum
field zeroed, bulk-OUT EP 0x02 for MGMT and BE, 8B pad prepended only
when ``(size + 40) % 512 == 0``. MGMT sequence numbers run the
``mgnt_seq`` counter (caller-owned ``st["mgnt_seq"]``, init 0);
retry_ctrl FALSE (limit 12) matches the monitor path and the recorded
disconnect deauth, while the assoc flow default TRUE (limit 6) is kept
for reference vectors. DATA descriptors share the builder with explicit
fields; per-link DATA rules (per-STA mac_id/raid, per-TID seq) are not
modeled, so live DATA injection stays unported.
"""
from __future__ import annotations

import struct

TXDESC_SIZE = 40
TXDESC_OFFSET = 40
PACKET_OFFSET_SZ = 8
USB_BULK_SIZE = 512
BULK_OUT_EP = 0x02

QSLT_MGNT = 0x12
QSLT_BE = 0x0
MGMT_MACID = 1
MGMT_RAID = 8
MGMT_TX_RATE = 0x00
MGMT_RETRY_LIMIT_ASSOC = 6
MGMT_RETRY_LIMIT_INJECT = 12
MGMT_MBSSID = 0

BROADCAST = bytes((0xFF,) * 6)


def _is_mcast(addr: bytes) -> bool:
    return bool(addr[0] & 0x01)


def _set(desc: bytearray, word: int, shift: int, width: int, value: int) -> None:
    raw = struct.unpack("<I", desc[word * 4:word * 4 + 4])[0]
    mask = ((1 << width) - 1) << shift
    raw = (raw & ~mask) | ((value << shift) & mask)
    desc[word * 4:word * 4 + 4] = struct.pack("<I", raw & 0xFFFFFFFF)


def txdesc_checksum(desc: bytes) -> int:
    total = 0
    for i in range(0, 32, 2):
        if i == 28:
            continue
        total ^= struct.unpack("<H", desc[i:i + 2])[0]
    return total & 0xFFFF


def build_tx_desc(*, size: int, seq: int, macid: int, qsel: int,
                  rateid: int, use_rate: bool, tx_rate: int,
                  retry_en: bool, retry_limit: int, mbssid: int = 0,
                  bmc: bool = False, hwseq_en: bool = True,
                  agg_num: int = 0, spe_rpt: bool = False,
                  agg_break: bool = False, data_short: bool = False,
                  fb_limit: int = 0) -> bytes:
    desc = bytearray(TXDESC_SIZE)
    _set(desc, 0, 0, 16, size)
    _set(desc, 0, 16, 8, TXDESC_OFFSET)
    _set(desc, 0, 24, 1, 1 if bmc else 0)
    _set(desc, 1, 0, 7, macid)
    _set(desc, 1, 8, 5, qsel)
    _set(desc, 1, 16, 5, rateid)
    _set(desc, 2, 16, 1, 1 if agg_break else 0)
    _set(desc, 2, 19, 1, 1 if spe_rpt else 0)
    _set(desc, 3, 8, 1, 1 if use_rate else 0)
    _set(desc, 4, 0, 7, tx_rate)
    _set(desc, 4, 8, 5, fb_limit)
    _set(desc, 4, 17, 1, 1 if retry_en else 0)
    _set(desc, 4, 18, 6, retry_limit)
    _set(desc, 5, 4, 1, 1 if data_short else 0)
    _set(desc, 6, 12, 4, mbssid)
    _set(desc, 7, 24, 8, agg_num)
    _set(desc, 8, 15, 1, 1 if hwseq_en else 0)
    _set(desc, 9, 12, 12, seq)
    _set(desc, 7, 0, 16, txdesc_checksum(bytes(desc)))
    return bytes(desc)


def build_mgnt_desc(*, size: int, seq: int, bmc: bool,
                    retry_limit: int = MGMT_RETRY_LIMIT_INJECT,
                    spe_rpt: bool = False) -> bytes:
    """The monitor-path template: MGMT frames and monitor-injected DATA
    share it (`update_mgntframe_attrib` / `update_monitor_frame_attrib`
    + plain `dump_mgntframe`; per-link station DATA rules are separate)."""
    return build_tx_desc(size=size, seq=seq, macid=MGMT_MACID,
                         qsel=QSLT_MGNT, rateid=MGMT_RAID, use_rate=True,
                         tx_rate=MGMT_TX_RATE, retry_en=True,
                         retry_limit=retry_limit, mbssid=MGMT_MBSSID,
                         bmc=bmc, hwseq_en=True, agg_num=0,
                         spe_rpt=spe_rpt)


def needs_zero_pad(size: int) -> bool:
    return (size + TXDESC_SIZE) % USB_BULK_SIZE == 0


def build_tx_urb(frame: bytes, desc: bytes) -> bytes:
    if needs_zero_pad(len(frame)):
        return bytes(PACKET_OFFSET_SZ) + desc + frame
    return desc + frame


def build_deauth_frame(dst: bytes, src: bytes, bssid: bytes, seq: int,
                       reason: int = 7, duration: int = 0x0000) -> bytes:
    return (struct.pack("<HH6s6s6sH", 0x00C0, duration, dst, src, bssid,
                        (seq << 4) & 0xFFF0)
            + struct.pack("<H", reason))


def frame_seqnum(frame: bytes) -> int:
    return (struct.unpack("<H", frame[22:24])[0] >> 4) & 0xFFF


def stamp_seqnum(frame: bytes, seq: int) -> bytes:
    return frame[:22] + struct.pack("<H", (seq << 4) & 0xFFF0) + frame[24:]


def inject_frame(t, st: dict, frame: bytes,
                 retry_limit: int = MGMT_RETRY_LIMIT_INJECT) -> bool:
    """Send one pre-stamped MGMT or DATA frame on the monitor path: the
    descriptor follows the shared monitor template with the frame's own
    sequence number (recorded invariant), then ``mgnt_seq`` advances.
    ``t`` needs ``bulk_out(data)`` on the MGMT pipe (EP 0x02). Control
    frames are unported."""
    if len(frame) < 24 or (frame[0] & 0x0C) not in (0x00, 0x08):
        raise ValueError("live control-frame injection untested here")
    seq = frame_seqnum(frame)
    desc = build_mgnt_desc(size=len(frame), seq=seq,
                           bmc=_is_mcast(frame[4:10]),
                           retry_limit=retry_limit)
    t.bulk_out(build_tx_urb(frame, desc))
    st["mgnt_seq"] = (seq + 1) & 0xFFF
    return True
