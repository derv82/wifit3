"""rt2800usb parse_rx_urb byte-level decode: the RXINFO/RXWI prefix strip,
L2-pad removal, and the MPDU_TOTAL_BYTE_COUNT trim. Guards the
order-of-operations that keeps EAPOL key_data intact -- remove the L2 pad
BEFORE trimming to mpdu_len (see rx.parse_rx_urb)."""
import struct

import wifit3.chips.rt2800usb.rx as rx
from wifit3.chips.rt2800usb.constants import RXD_W0_CRC_ERROR, RXD_W0_L2PAD


def _urb(frame_region: bytes, *, mpdu_len: int, rxd_w0: int = 0) -> bytes:
    """Assemble a bulk-IN URB: 4B RXINFO + 16B RXWI + frame region + 4B RXD.
    MPDU_TOTAL_BYTE_COUNT sits in RXWI_W0 bits 27:16; RXINFO_W0 low bits carry
    rx_pkt_len (RXWI + frame region); RXD_W0 trails after rx_pkt_len bytes."""
    rxwi = struct.pack("<I", mpdu_len << 16) + bytes(12)   # W0 + W1/W2/W3
    rx_pkt_len = len(rxwi) + len(frame_region)
    return (
        struct.pack("<I", rx_pkt_len)        # RXINFO_W0
        + rxwi
        + frame_region
        + struct.pack("<I", rxd_w0)          # RXD_W0
    )


# 26-byte QoS-Data header (fc0=0x88 -> DATA/QoS, fc1=0x01 -> to_ds): not
# 4-aligned, so the hw inserts the 2-byte L2 pad after it -- the EAPOL case.
_QOS_HDR = bytes([0x88, 0x01]) + bytes(24)
_BODY = bytes(range(20)) + b"\xde\xad\xbe\xef"   # 24 B; last 4 = the tail at risk
_MPDU_LEN = len(_QOS_HDR) + len(_BODY)           # 50: de-padded MPDU, no FCS


def test_l2pad_frame_keeps_body_tail():
    # [hdr][pad(2)][body][trailing] with L2PAD set. The de-pad must precede the
    # mpdu_len trim, or the last 2 body bytes (here the tail of de:ad:be:ef)
    # fall outside the window and are lost -- exactly the EAPOL clip.
    region = _QOS_HDR + b"\x00\x00" + _BODY + b"\xff\xff"
    frame = rx.parse_rx_urb(_urb(region, mpdu_len=_MPDU_LEN, rxd_w0=RXD_W0_L2PAD))
    assert frame is not None
    assert frame.mpdu == _QOS_HDR + _BODY            # pad removed, body whole
    assert frame.mpdu[-4:] == b"\xde\xad\xbe\xef"    # the tail the bug dropped


def test_no_l2pad_trims_to_mpdu_len():
    # No pad, flag clear: [hdr][body][trailing]. Trim to mpdu_len; body intact.
    region = _QOS_HDR + _BODY + b"\xff\xff"
    frame = rx.parse_rx_urb(_urb(region, mpdu_len=_MPDU_LEN, rxd_w0=0))
    assert frame is not None
    assert frame.mpdu == _QOS_HDR + _BODY


def test_crc_error_flag_surfaced():
    # rt2800usb surfaces CRC via has_fcs_error (it does not drop the frame).
    region = _QOS_HDR + _BODY
    frame = rx.parse_rx_urb(_urb(region, mpdu_len=_MPDU_LEN, rxd_w0=RXD_W0_CRC_ERROR))
    assert frame is not None
    assert frame.has_fcs_error is True
