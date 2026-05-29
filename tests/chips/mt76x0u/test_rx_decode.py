"""mt76x0u decode_rx_packet byte-level decoding. Guards the order-of-operations
that keeps EAPOL key_data intact: remove the L2 pad BEFORE trimming to MPDU_LEN
(mirrors mt76x02_remove_hdr_pad → pskb_trim), so the body tail isn't clipped."""
import struct

import wifit3.chips.mt76x0u.rx as rx


def _packet(frame_region: bytes, *, mpdu_len: int, rxinfo: int = 0,
            rssi0: int = 0xC8) -> bytes:
    """Assemble a bulk-IN packet: 4B DMA hdr + 32B RXWI + frame region + 4B FCE
    trailer, zero-padded so the post-DMA-header length is 4-byte aligned (the
    dma_len self-consistency check requires it). MPDU_LEN @ ctl bits 29:16."""
    rxwi = (
        struct.pack("<I", rxinfo)            # rxinfo
        + struct.pack("<I", mpdu_len << 16)  # ctl: MPDU_LEN @ bits 29:16
        + b"\x00\x00"                        # tid_sn
        + b"\x00\x00"                        # rate
        + bytes([rssi0, 0, 0, 0])            # rssi[4]
        + bytes(16)                          # bbp_rxinfo[4]
    )
    rest = rxwi + frame_region + b"\x00\x00\x00\x00"   # ... + FCE info trailer
    while len(rest) % 4:                               # 4-byte align
        rest += b"\x00"
    dma_hdr = struct.pack("<H", len(rest)) + b"\x00\x00"
    return dma_hdr + rest


# 26-byte QoS-Data header (fc0=0x88 DATA/QoS, fc1=0x01 to_ds): not 4-byte aligned
# → hardware inserts the 2-byte L2 pad after it. This is what EAPOL rides on.
_QOS_HDR = bytes([0x88, 0x01]) + bytes(24)
_BODY = bytes(range(20)) + b"\xde\xad\xbe\xef"     # 24 B; last 4 = tail at risk
_MPDU_LEN = len(_QOS_HDR) + len(_BODY)             # 50: de-padded MPDU, no FCS


def test_l2pad_frame_keeps_body_tail():
    # [hdr][pad(2)][body] with L2PAD set: de-pad must precede the MPDU_LEN trim,
    # or the last 2 body bytes fall outside the window — exactly the EAPOL clip.
    region = _QOS_HDR + b"\x00\x00" + _BODY
    decoded = rx.decode_rx_packet(_packet(region, mpdu_len=_MPDU_LEN,
                                          rxinfo=rx.MT_RXINFO_L2PAD))
    assert decoded is not None
    assert decoded.frame == _QOS_HDR + _BODY        # pad removed, body whole
    assert decoded.frame[-4:] == b"\xde\xad\xbe\xef"


def test_no_l2pad_passthrough():
    region = _QOS_HDR + _BODY
    decoded = rx.decode_rx_packet(_packet(region, mpdu_len=_MPDU_LEN, rxinfo=0))
    assert decoded is not None
    assert decoded.frame == _QOS_HDR + _BODY


def test_crc_error_is_dropped():
    region = _QOS_HDR + _BODY
    assert rx.decode_rx_packet(
        _packet(region, mpdu_len=_MPDU_LEN, rxinfo=rx.MT_RXINFO_CRCERR)) is None
