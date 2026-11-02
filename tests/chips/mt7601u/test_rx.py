"""RX descriptor decode tests for the MT7601U port.

Payloads here are taken from the monitor-mode capture, so the byte layouts are the ones
the silicon actually produced. Wire-order behaviour of the whole bring-up is checked by
scripts/chips/mt7601u/verify_mt7601u.py; this pins the decode.
"""
from __future__ import annotations

import pytest


from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.rx import (
    MIN_SEGMENT_LEN,
    RXWI_LEN,
    decode_segment,
    get_rssi,
    iter_frames,
    next_segment_len,
)

# capture-3-monitor-chan, first bulk-IN packet of the dongle (148f:7601): the 32-byte
# DMA header plus rxwi exactly as the silicon wrote them, and the first 14 payload bytes.
CAPTURED_HEADER = bytes.fromhex(
    "e000080060200000ff00c100805f00002e0000005241d8000000000000000000"
)
CAPTURED_BSSID = bytes.fromhex("b8d4bc2e4e68")
CAPTURED_MPDU_LEN = 193
CAPTURED_PAYLOAD_LEN = 196
CAPTURED_SEG = (
    CAPTURED_HEADER
    + bytes.fromhex("80000000ffffffffffff") + CAPTURED_BSSID
    + bytes(CAPTURED_PAYLOAD_LEN - 16)
    + b"\x00\x00\x00\x00"                            # FCE
)


def build_segment(*, mpdu_len: int, fce_type: int = 0, rxinfo: int = 0,
                  payload: bytes | None = None) -> bytes:
    """Assemble one segment: DMA header, rxwi, payload, FCE."""
    body = b"\x80\x00\x00\x00\xff\xff\xff\xff\xff\xff" + bytes(
        max(mpdu_len - 10, 0)) if payload is None else payload
    # The hardware's length word spans the segment minus the 8-byte double header,
    # so it counts the rxwi and the payload; the trailing FCE rides inside that span.
    dma_len = len(body) + RXWI_LEN
    return (
        dma_len.to_bytes(2, "little") + b"\x08\x00"
        + rxinfo.to_bytes(4, "little")
        + (mpdu_len << 16).to_bytes(4, "little")   # ctl: MPDU_LEN in bits 27..16
        + b"\x80\x5f"                                # frag_sn
        + b"\x5f\x00"                                # rate
        + b"\x2e\x00\x00\x00"                        # unknown + zero[3]
        + bytes([0x52, 0x41, 0xD8, 0x00])            # snr, ant, gain, freq_off
        + b"\x00" * 8                                # resv2 + expert_ant
        + body
        + (fce_type << 30).to_bytes(4, "little")
    )


class TestSegmentLength:
    def test_captured_segment_spans_the_whole_buffer(self) -> None:
        assert next_segment_len(CAPTURED_SEG) == len(CAPTURED_SEG)

    def test_builtin_segment_length_is_the_dma_header_plus_dma_len(self) -> None:
        assert next_segment_len(build_segment(mpdu_len=32)) == 4 + 32 + RXWI_LEN + 4

    def test_short_buffer_yields_nothing(self) -> None:
        assert next_segment_len(b"\x00" * (MIN_SEGMENT_LEN - 1)) == 0

    def test_zero_dma_len_yields_nothing(self) -> None:
        assert next_segment_len(b"\x00\x00\x08\x00" + b"\x00" * 64) == 0

    def test_unaligned_dma_len_yields_nothing(self) -> None:
        seg = bytearray(build_segment(mpdu_len=32))
        seg[0:2] = (60 + 1).to_bytes(2, "little")   # dma_len 61 is not 4-byte aligned
        assert next_segment_len(bytes(seg)) == 0

    def test_dma_len_past_the_buffer_yields_nothing(self) -> None:
        seg = bytearray(build_segment(mpdu_len=32))
        seg[0:2] = (4096).to_bytes(2, "little")
        assert next_segment_len(bytes(seg)) == 0


class TestDecodeSegment:
    def test_frame_length_comes_from_the_ctl_field(self) -> None:
        seg = build_segment(mpdu_len=193)
        frame = decode_segment(seg)
        assert frame is not None
        assert len(frame.frame) == 193

    def test_captured_assoc_response_decodes(self) -> None:
        frame = decode_segment(CAPTURED_SEG)
        assert frame.frame[:2] == b"\x80\x00"          # FC: association response
        assert frame.frame[4:10] == b"\xff" * 6       # RA: broadcast
        assert frame.frame[10:16] == CAPTURED_BSSID   # TA
        assert len(frame.frame) == 193

    def test_rf_fields_land_on_their_rxwi_offsets(self) -> None:
        frame = decode_segment(CAPTURED_SEG)
        assert (frame.snr, frame.ant, frame.gain, frame.freq_off) == (0x52, 0x41, 0xD8, 0x00)
        assert frame.rate == 0x0000

    def test_non_packet_fce_type_is_dropped(self) -> None:
        assert decode_segment(build_segment(mpdu_len=64, fce_type=1)) is None

    def test_zero_mpdu_len_is_dropped(self) -> None:
        assert decode_segment(build_segment(mpdu_len=0, payload=b"\x80" * 16)) is None

    def test_mpdu_len_beyond_the_payload_is_dropped(self) -> None:
        assert decode_segment(build_segment(mpdu_len=32, payload=b"\x80" * 4)) is None

    def test_mpdu_len_under_ten_is_dropped(self) -> None:
        assert decode_segment(build_segment(mpdu_len=9)) is None

    def test_crc_error_frame_is_dropped(self) -> None:
        """A frame the MAC flagged FCS-failed is noise wearing a plausible header."""
        assert decode_segment(build_segment(mpdu_len=64)) is not None
        assert decode_segment(build_segment(mpdu_len=64,
                                           rxinfo=C.MT_RXINFO_CRCERR)) is None

    def test_l2pad_splits_header_from_body(self) -> None:
        # FC 0x0084 is a control frame, so the header is 16 bytes and the two pad
        # bytes the hardware inserted sit at offset 16. Reading the header as 10
        # would splice them out of the middle of the frame instead.
        payload = b"\x84\x00" + b"\xaa" * 14 + b"\xbb\xbb" + b"\xcc" * 20
        seg = build_segment(mpdu_len=len(payload), rxinfo=C.MT_RXINFO_L2PAD, payload=payload)
        frame = decode_segment(seg)
        assert frame.frame == payload[:16] + payload[18:]


class TestIterFrames:
    def test_walks_every_chained_segment(self) -> None:
        two = build_segment(mpdu_len=64) + build_segment(mpdu_len=96)
        frames = list(iter_frames(two))
        assert [len(f.frame) for f in frames] == [64, 96]

    def test_stops_at_a_short_remainder(self) -> None:
        two = build_segment(mpdu_len=64) + b"\x01\x00\x08\x00"
        assert len(list(iter_frames(two))) == 1

    def test_skips_a_dropped_segment_and_keeps_going(self) -> None:
        three = (build_segment(mpdu_len=64)
                 + build_segment(mpdu_len=0, payload=b"\x80" * 16)
                 + build_segment(mpdu_len=32))
        assert [len(f.frame) for f in iter_frames(three)] == [64, 32]


# net/wireless/util.c:455 ieee80211_hdrlen, transcribed independently so the port is
# checked against the algorithm rather than against itself. Frame type is bits 2-3,
# subtype is bits 4-7, To DS / From DS are bits 8 and 9, Order is bit 15.
def _kernel_hdrlen(fc: int) -> int:
    hdrlen = 24
    if (fc & 0x000C) == 0x000C:                                   # ieee80211_is_ext
        return 4
    if (fc & 0x000C) == 0x0008:                                   # ieee80211_is_data
        if (fc & 0x0300) == 0x0300:                               # ieee80211_has_a4
            hdrlen = 30
        if (fc & 0x008C) == 0x0088:                               # ieee80211_is_data_qos
            hdrlen += 2                                            # IEEE80211_QOS_CTL_LEN
            if fc & 0x8000:                                        # ieee80211_has_order
                hdrlen += 4                                        # IEEE80211_HT_CTL_LEN
        return hdrlen
    if (fc & 0x000C) == 0x0000:                                   # ieee80211_is_mgmt
        return 28 if fc & 0x8000 else 24
    if (fc & 0x00E0) == 0x00C0:                                   # ACK or CTS, else 16
        return 10
    return 16


class TestHdrlen:
    """Every frame control the port injects must produce the length the MAC header
    actually occupies. Get it wrong and the header pad splices into the middle of a
    frame, which no descriptor-length check can see."""

    # Real 802.11 frame controls, not masks: type in bits 2-3, subtype in bits 4-7.
    FRAME_CONTROLS = [
        0x0000,   # beacon
        0x0040,   # probe request
        0x00A0,   # disassociation
        0x00C0,   # deauthentication
        0x0008,   # data
        0x0108,   # data, To DS
        0x0208,   # data, From DS
        0x0308,   # data, both DS bits: four addresses
        0x0088,   # QoS data
        0x0188,   # QoS data, To DS
        0x0388,   # QoS data, four addresses
        0x8088,   # QoS data with the Order bit
        0x80C0,   # deauthentication with the Order bit
        0x0B4,    # control, RTS: a receiver address and a duration
        0x0C4,    # control, ACK: no addresses at all
        0x0D4,    # control, CTS
        0x000C,   # extension frame
    ]

    @pytest.mark.parametrize("fc", FRAME_CONTROLS)
    def test_matches_the_kernel_algorithm(self, fc: int) -> None:
        from wifit3.chips.mt7601u.rx import _hdrlen
        assert _hdrlen(fc) == _kernel_hdrlen(fc)

    def test_a_management_frame_is_twenty_four_bytes(self) -> None:
        """The bug this pins. Reading only the DS bits put a deauth at 10 bytes, so
        the two-byte pad landed inside Addr2 and every injected deauth left the
        dongle corrupted. Nothing upstream could see it: the transfer length and
        the descriptor byte count were both unchanged."""
        from wifit3.chips.mt7601u.rx import _hdrlen
        assert _hdrlen(0x00C0) == 24

    def test_a_data_frame_is_never_ten_bytes(self) -> None:
        """Ten bytes is a bare ACK or CTS. A data frame is at least 24."""
        from wifit3.chips.mt7601u.rx import _hdrlen
        for fc in (0x0008, 0x0108, 0x0208, 0x0308):
            assert _hdrlen(fc) >= 24


class TestRssi:
    def test_matches_the_kernel_table(self) -> None:
        # Captured descriptor: bw20 (rate bit7 clear), main LNA (ant bit7 clear),
        # LNA id 3 -> index 2, gain field 0xd8 -> 24.
        assert get_rssi(0x005F, 0x41, 0xD8, 0, 0) == 8 - 33 - 24

    def test_lna_gain_lowers_rssi(self) -> None:
        assert get_rssi(0x005F, 0x41, 0xD8, 2, 0) == get_rssi(0x005F, 0x41, 0xD8, 0, 0) - 2

    def test_rssi_offset_lowers_rssi(self) -> None:
        assert get_rssi(0x005F, 0x41, 0xD8, 0, 7) == get_rssi(0x005F, 0x41, 0xD8, 0, 0) - 7

    def test_bw40_uses_the_bw40_lna_row(self) -> None:
        assert get_rssi(0x0080, 0x41, 0xD8, 0, 0) == 8 - 34 - 24

    def test_aux_lna_bw40_differs_from_main(self) -> None:
        assert get_rssi(0x0080, 0xC1, 0xD8, 0, 0) == 8 - 34 - 24

    def test_lna_id_zero_is_used_as_index_zero(self) -> None:
        assert get_rssi(0x0000, 0x00, 0x00, 0, 0) == 8 + 2
