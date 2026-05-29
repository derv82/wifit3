"""mt76x2u decode_urb byte-level decoding — the RXWI prefix strip, L2-pad
removal, and MPDU_LEN trim. Guards the order-of-operations that keeps EAPOL
key_data intact (remove pad BEFORE trimming to MPDU_LEN — see rx.decode_urb)."""
import struct

import wifit3.chips.mt76x2u.rx as rx

_RXINFO_L2PAD = 1 << 14
_RXINFO_CRCERR = 1 << 8


def _urb(frame_region: bytes, *, mpdu_len: int, rxinfo: int = 0,
         rssi0: int = 0xC8) -> bytes:
    """Assemble a bulk-IN URB: 4B rxfce + 32B RXWI + the 802.11 frame region.
    MPDU_LEN lives in ctl bits 29:16; rssi[0] is the first RXWI rssi byte."""
    rxfce = struct.pack("<I", 0)
    rxwi = (
        struct.pack("<I", rxinfo)            # rxinfo
        + struct.pack("<I", mpdu_len << 16)  # ctl: MPDU_LEN @ bits 29:16
        + b"\x00\x00"                        # tid_sn
        + b"\x00\x00"                        # rate
        + bytes([rssi0, 0, 0, 0])            # rssi[4]
        + bytes(16)                          # bbp_rxinfo[4]
    )
    assert len(rxfce) + len(rxwi) == 36
    return rxfce + rxwi + frame_region


# A 26-byte QoS-Data header (fc0=0x88 → DATA/QoS, fc1=0x01 → to_ds). This length
# isn't 4-byte aligned, so the hardware inserts the 2-byte L2 pad after it — the
# case that was clipping EAPOL.
_QOS_HDR = bytes([0x88, 0x01]) + bytes(24)
_BODY = bytes(range(20)) + b"\xde\xad\xbe\xef"   # 24 B; last 4 = the tail at risk
_MPDU_LEN = len(_QOS_HDR) + len(_BODY)           # 50: de-padded MPDU, no FCS


def test_l2pad_frame_keeps_body_tail():
    # [hdr][pad(2)][body][fcs(4)] with L2PAD set. The de-pad must happen before
    # the MPDU_LEN trim, or the last 2 body bytes (here the tail of \xde\xad\xbe
    # \xef) fall outside the window and are lost — exactly the EAPOL clip.
    region = _QOS_HDR + b"\x00\x00" + _BODY + b"\xff\xff\xff\xff"
    decoded = rx.decode_urb(_urb(region, mpdu_len=_MPDU_LEN, rxinfo=_RXINFO_L2PAD))
    assert decoded is not None
    frame = decoded["frame_bytes"]
    assert frame == _QOS_HDR + _BODY            # pad removed, FCS trimmed, body whole
    assert frame[-4:] == b"\xde\xad\xbe\xef"    # the tail the old order dropped


def test_no_l2pad_trims_fcs_only():
    # No pad: [hdr][body][fcs]. Trimming to MPDU_LEN drops the FCS; body intact.
    region = _QOS_HDR + _BODY + b"\xff\xff\xff\xff"
    decoded = rx.decode_urb(_urb(region, mpdu_len=_MPDU_LEN, rxinfo=0))
    assert decoded is not None
    assert decoded["frame_bytes"] == _QOS_HDR + _BODY


def test_crc_error_is_dropped():
    region = _QOS_HDR + _BODY
    assert rx.decode_urb(_urb(region, mpdu_len=_MPDU_LEN, rxinfo=_RXINFO_CRCERR)) is None


def test_rssi_is_signed():
    region = _QOS_HDR + _BODY
    decoded = rx.decode_urb(_urb(region, mpdu_len=_MPDU_LEN, rssi0=0xC8))
    assert decoded["rssi"] == -56               # 0xC8 as int8
