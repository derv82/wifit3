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


def test_agc_to_rssi_subtracts_offset_and_lna():
    # raw byte 40 on path0 only; base -12, offset0 2, lna 8 -> -12-2-8-40.
    assert rx._agc_to_rssi(40, rx.RssiCal(offset0=2, lna_gain=8)) == -62
    # zero cal reproduces the old pre-EEPROM -12 - raw ballpark.
    assert rx._agc_to_rssi(40, rx.RssiCal()) == -52


def test_rssi_cal_for_channel_selects_band_and_lna():
    from wifit3.chips.rt2800usb.eeprom import EepromValues
    ee = EepromValues(
        mac_address=b"\x00" * 6, nic_conf0=0, nic_conf1=0, freq_offset=0,
        lna_gain_bg=8, lna_gain_a=5, rssi_bg_offset0=1, rssi_bg_offset1=2,
        rssi_bg_offset2=3, rssi_a_offset0=4, rssi_a_offset1=5, rssi_a_offset2=6,
        lna_gain_a1=7, lna_gain_a2=9,
    )
    assert rx.rssi_cal_for_channel(ee, 1) == rx.RssiCal(1, 2, 3, 8)   # 2.4: BG + LNA_BG
    assert rx.rssi_cal_for_channel(ee, 36).lna_gain == 5              # LNA_A0
    assert rx.rssi_cal_for_channel(ee, 100).lna_gain == 7            # LNA_A1
    c149 = rx.rssi_cal_for_channel(ee, 149)                          # LNA_A2 + A offsets
    assert (c149.lna_gain, c149.offset0, c149.offset1, c149.offset2) == (9, 4, 5, 6)


def test_eeprom_rssi_offset_and_lna_sanitized():
    from wifit3.chips.rt2800usb.eeprom import _sanitize_lna, _sanitize_rssi_offset
    assert _sanitize_rssi_offset(8) == 8
    assert _sanitize_rssi_offset(200) == 0     # abs > 10 -> junk -> 0
    assert _sanitize_lna(0x00, 5) == 5         # unburned -> default (LNA_A0)
    assert _sanitize_lna(0xFF, 5) == 5
    assert _sanitize_lna(3, 5) == 3
