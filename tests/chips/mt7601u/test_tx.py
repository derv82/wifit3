"""TX descriptor tests for the MT7601U port.

Layout and field semantics come from tx.c (mt7601u_push_txwi) and dma.h
(struct mt76_txwi). Byte-exact verification of the resulting wire descriptor is the job of
scripts/chips/mt7601u/verify_tx.py against a recorded kernel-driver injection.
"""
from __future__ import annotations

import pytest

from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.tx import (
    DMA_INFO_LEN,
    TRAILER_LEN,
    TX_NO_STATION,
    TX_QUEUE_COUNT,
    TX_QUEUE_INBAND_CMD,
    TX_QUEUE_INJECT,
    TXWI_LEN,
    RATE_CONTROLLED,
    WCID_STORED_RATE,
    build_tx_dma,
    build_txwi,
    dma_queue_for_endpoint,
    header_pad_len,
    packet_id,
)

CONTROL_FRAME = b"\xc4\x00" + b"\x00" * 8
"""A 10-byte control frame (FC 0x00C4, an ACK): the shortest legal send, and the
only length that is 2 mod 4, so it is the one that takes the header pad."""

QOS_DATA_FRAME = b"\x08\x01" + bytes(22)
"""24 bytes, FC 0x0108: data To DS, so the header is 24 bytes and needs no pad."""


def fields(raw: bytes) -> dict[str, int]:
    """Decode a txwi into its named fields."""
    return {
        "flags": int.from_bytes(raw[0:2], "little"),
        "rate_ctl": int.from_bytes(raw[2:4], "little"),
        "ack_ctl": raw[4],
        "wcid": raw[5],
        "len_ctl": int.from_bytes(raw[6:8], "little"),
        "iv": int.from_bytes(raw[8:12], "little"),
        "eiv": int.from_bytes(raw[12:16], "little"),
        "aid": raw[16],
        "txstream": raw[17],
        "ctl": int.from_bytes(raw[18:20], "little"),
    }


class TestTxwiLayout:
    def test_is_twenty_bytes(self) -> None:
        assert TXWI_LEN == 20
        assert len(build_txwi(ack=True, wcid=TX_NO_STATION, length=10)) == 20

    def test_is_all_zero_except_the_fields_we_set(self) -> None:
        """tx.c memsets the descriptor, so anything unset must read back zero."""
        f = fields(build_txwi(ack=False, wcid=TX_NO_STATION, length=10))
        assert f["flags"] == 0
        assert f["iv"] == 0
        assert f["eiv"] == 0
        assert f["aid"] == 0
        assert f["txstream"] == 0
        assert f["ctl"] == 0


# The first 24 bytes of a real deauth injection, captured off the dongle with
# usbmon while aireplay-ng sent 7680 identical broadcast deauths. The text
# format truncates at 32 bytes, which still covers the info word and the txwi.
CAPTURED_DEAUTH_INFO = bytes.fromhex("30000805")
CAPTURED_DEAUTH_TXWI = bytes.fromhex(
    "0000000000ff1a10000000000000000000000000")

CAPTURE_6_TX_ENDPOINT = 0x07
"""Every usbmon record in capture-6-kernel-tx reads `Bo:1:015:7` for the kernel's
7680 deauths, so the trailing 7 is the OUT endpoint the kernel transmitted on."""

OUT_EP_DESCRIPTOR_ORDER = [0x08, 0x04, 0x05, 0x06, 0x07, 0x09]
"""bulk-OUT endpoints in interface-descriptor order, which is how mt7601u_assign_pipes
fills out_eps -- not ascending, so the index does not match the endpoint number."""


class TestAgainstCapturedInjection:
    """The fields a real injection used, byte for byte."""

    def _build(self, **kw):
        return build_tx_dma(CONTROL_FRAME, wcid=TX_NO_STATION, rate=0, **kw)

    def test_info_word_matches(self) -> None:
        """LEN is excluded: it is round_up(txwi + frame, 4), and the two frames differ
        in length. What it must equal is the padded txwi+body, not the USB length --
        the capture reads LEN 48 on a 56-byte transfer."""
        info = int.from_bytes(self._build(ack=False)[:4], "little")
        got = int.from_bytes(CAPTURED_DEAUTH_INFO, "little")
        for name, mask in (("D_PORT", C.MT_TXD_INFO_D_PORT),
                           ("TYPE", C.MT_TXD_INFO_TYPE), ("QSEL", C.MT_TXD_PKT_INFO_QSEL)):
            assert C._field_get(mask, info) == C._field_get(mask, got), name
        # LEN covers txwi + frame, excluding the DMA info word and the trailer.
        padded = -(-(TXWI_LEN + len(CONTROL_FRAME)) // 4) * 4
        assert C._field_get(C.MT_TXD_INFO_LEN, info) == padded
        assert bool(info & C.MT_TXD_PKT_INFO_80211) == bool(got & C.MT_TXD_PKT_INFO_80211)
        assert bool(info & C.MT_TXD_PKT_INFO_WIV) == bool(got & C.MT_TXD_PKT_INFO_WIV)

    def test_txwi_matches_except_the_frame_length(self) -> None:
        """Every txwi field the capture pinned: flags, rate, ack, wcid, packet id."""
        built = self._build(ack=False)[4:4 + TXWI_LEN]
        got = CAPTURED_DEAUTH_TXWI
        assert built[0:6] == got[0:6]          # flags, rate_ctl, ack_ctl, wcid
        assert (built[6:8] and int.from_bytes(built[6:8], "little") & 0xF000
                == int.from_bytes(got[6:8], "little") & 0xF000)   # PKTID
        assert built[16:20] == got[16:20]      # aid, txstream, ctl

    def test_capture_proves_no_ack_on_a_broadcast_deauth(self) -> None:
        """tx.c sets MT_TXWI_ACK_CTL_REQ only when IEEE80211_TX_CTL_NO_ACK is clear.
        A broadcast deauth is noack, so requesting an ACK is wrong."""
        assert CAPTURED_DEAUTH_TXWI[4] == 0x00

    def test_broadcast_injection_does_not_request_an_ack_by_default(self) -> None:
        assert fields(self._build()[4:4 + TXWI_LEN])["ack_ctl"] == 0x00

    def test_an_ack_can_still_be_requested_explicitly(self) -> None:
        assert fields(self._build(ack=True)[4:4 + TXWI_LEN])["ack_ctl"] & C.MT_TXWI_ACK_CTL_REQ

    def test_the_monitor_slot_is_confirmed_as_0xff(self) -> None:
        assert CAPTURED_DEAUTH_TXWI[5] == 0xFF

    def test_rate_zero_encodes_packet_id_one(self) -> None:
        assert int.from_bytes(CAPTURED_DEAUTH_TXWI[6:8], "little") & 0xF000 == 0x1000


class TestAckCtl:
    def test_ack_sets_the_request_bit(self) -> None:
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10))
        assert f["ack_ctl"] & C.MT_TXWI_ACK_CTL_REQ

    def test_no_ack_clears_it(self) -> None:
        f = fields(build_txwi(ack=False, wcid=TX_NO_STATION, length=10))
        assert not f["ack_ctl"] & C.MT_TXWI_ACK_CTL_REQ

    def test_the_chip_assigns_sequence_numbers(self) -> None:
        """NSEQ clear means the hardware stamps the 802.11 sequence number."""
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10))
        assert not f["ack_ctl"] & C.MT_TXWI_ACK_CTL_NSEQ

    def test_nseq_is_clear_unless_the_mac_is_asked_to_assign(self) -> None:
        """rt2800.h:3097 -- NSEQ 1 assigns a hardware sequence number, 0 does not.
        tx.c:160 sets it only for ASSIGN_SEQ, and the monitor form leaves it clear."""
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10))
        assert not f["ack_ctl"] & C.MT_TXWI_ACK_CTL_NSEQ
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10, assign_seq=True))
        assert f["ack_ctl"] & C.MT_TXWI_ACK_CTL_NSEQ


class TestWcid:
    def test_broadcast_uses_the_no_station_sentinel(self) -> None:
        """init.c:590 sets the monitor WCID's idx to 0xff; tx.c sends monitor
        injection through it. Slot 0 would be a real station."""
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10))
        assert f["wcid"] == 0xFF

    def test_a_chosen_slot_is_carried_verbatim(self) -> None:
        f = fields(build_txwi(ack=True, wcid=7, length=10))
        assert f["wcid"] == 7

    def test_out_of_range_slot_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="wcid"):
            build_txwi(ack=True, wcid=0x100, length=10)


class TestLenCtl:
    def test_byte_count_carries_the_frame_length(self) -> None:
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=193))
        assert C._field_get(C.MT_TXWI_LEN_BYTE_CNT, f["len_ctl"]) == 193

    def test_packet_id_encodes_the_rate(self) -> None:
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10, rate=0))
        assert C._field_get(C.MT_TXWI_LEN_PKTID, f["len_ctl"]) == packet_id(0, False)

    def test_rate_advances_the_packet_id(self) -> None:
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10, rate=5))
        assert C._field_get(C.MT_TXWI_LEN_PKTID, f["len_ctl"]) == packet_id(5, False)

    def test_rate_index_is_stored_in_rate_ctl(self) -> None:
        f = fields(build_txwi(ack=True, wcid=TX_NO_STATION, length=10, rate=3))
        assert C._field_get(C.MT_TXWI_RATE_MCS, f["rate_ctl"]) == 3

    def test_a_frame_longer_than_the_byte_count_field_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="length"):
            build_txwi(ack=True, wcid=TX_NO_STATION, length=0x1000)


class TestPacketIdEncoding:
    """tx.c mt7601u_tx_pktid_enc: (rate + 1) + is_probe * 8, with the MCS7-probe
    collision folded back one step because PKT_ID 0 disables status reporting."""

    @pytest.mark.parametrize("rate", range(8))
    def test_plain_rates_encode_as_rate_plus_one(self, rate: int) -> None:
        assert packet_id(rate, False) == rate + 1

    @pytest.mark.parametrize("rate", range(7))
    def test_probe_rates_add_eight(self, rate: int) -> None:
        assert packet_id(rate, True) == rate + 1 + 8

    def test_mcs7_probe_is_the_one_folded_case(self) -> None:
        """The collision the C folds away; it is the exception, not the rule."""
        assert packet_id(7, True) == 9

    def test_mcs7_probe_collides_with_mcs0_probe_and_is_folded_back(self) -> None:
        assert packet_id(7, True) == packet_id(0, True)

    def test_no_encoding_yields_the_status_reporting_killer(self) -> None:
        for rate in range(8):
            for probe in (False, True):
                assert packet_id(rate, probe) != 0


class TestHeaderPad:
    """Valid 802.11 header lengths are 10, 16, 20, 24, 26 and 30 -- only ever
    0 or 2 mod 4 -- so the two-byte pad either aligns the body or is omitted."""

    @pytest.mark.parametrize("hdr_len", [10, 26, 30])
    def test_headers_two_mod_four_take_two_bytes(self, hdr_len: int) -> None:
        assert header_pad_len(hdr_len) == 2

    @pytest.mark.parametrize("hdr_len", [16, 20, 24])
    def test_headers_zero_mod_four_take_nothing(self, hdr_len: int) -> None:
        assert header_pad_len(hdr_len) == 0


class TestDmaWrapper:
    def test_short_frame_is_rejected_not_read_past(self) -> None:
        """Review Focus: a control frame is 10 bytes; anything shorter is garbage
        and must not be packed by reading past the buffer's end."""
        with pytest.raises(ValueError, match="too short"):
            build_tx_dma(b"\x08\x01\x00", ack=True)

    def test_empty_frame_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            build_tx_dma(b"", ack=True)

    def test_total_length_is_a_multiple_of_four(self) -> None:
        """Review Focus: the DMA trailer assumes 4-byte alignment."""
        # 24 covers both header lengths: the 10-byte control header and the
        # 24-byte To DS header, so both pad paths are exercised.
        for n in (24, 25, 32, 33, 64, 193):
            frame = CONTROL_FRAME[:2] + b"\x00" * (n - 2) if n <= 30 else \
                QOS_DATA_FRAME[:2] + b"\x00" * (n - 2)
            assert len(build_tx_dma(frame, ack=True)) % 4 == 0

    def test_frame_appears_verbatim_after_the_descriptor(self) -> None:
        frame = QOS_DATA_FRAME
        out = build_tx_dma(frame, ack=True)
        assert out[4 + TXWI_LEN:4 + TXWI_LEN + len(frame)] == frame

    def test_trailing_pad_makes_an_odd_frame_align(self) -> None:
        """dma.h skb_put_padto: the whole transfer is rounded up, not just the header."""
        frame = CONTROL_FRAME + bytes(11)            # 21 bytes, header pad 2
        out = build_tx_dma(frame, ack=True)
        assert len(out) % 4 == 0
        tail = out[4 + TXWI_LEN + 2 + len(frame):-4]
        assert tail == bytes(len(tail))             # alignment pad, all zeros

    def test_trailer_is_four_zero_bytes(self) -> None:
        out = build_tx_dma(CONTROL_FRAME, ack=True)
        assert out[-4:] == b"\x00\x00\x00\x00"

    def test_info_length_field_excludes_the_info_word_and_trailer(self) -> None:
        """The capture settles this: a 56-byte transfer carries LEN 48. The field is
        round_up(txwi + frame, 4); the DMA info word and zero trailer sit outside it."""
        frame = QOS_DATA_FRAME[:2] + bytes(range(29))     # 31 bytes, odd length
        out = build_tx_dma(frame, ack=True)
        info = int.from_bytes(out[:4], "little")
        assert C._field_get(C.MT_TXD_INFO_LEN, info) == len(out) - DMA_INFO_LEN - TRAILER_LEN

    def test_destination_port_is_the_wlan_port(self) -> None:
        out = build_tx_dma(CONTROL_FRAME, ack=True)
        info = int.from_bytes(out[:4], "little")
        assert C._field_get(C.MT_TXD_INFO_D_PORT, info) == C.WLAN_PORT

    def test_type_is_a_dma_packet(self) -> None:
        out = build_tx_dma(CONTROL_FRAME, ack=True)
        info = int.from_bytes(out[:4], "little")
        assert C._field_get(C.MT_TXD_INFO_TYPE, info) == C.DMA_PACKET

    def test_80211_bit_marks_the_payload_as_a_raw_frame(self) -> None:
        """mt7601u_dma_enqueue_tx sets MT_TXD_PKT_INFO_80211: the payload is a
        complete 802.11 frame, not an Ethernet payload for the driver to convert."""
        out = build_tx_dma(CONTROL_FRAME, ack=True)
        info = int.from_bytes(out[:4], "little")
        assert info & C.MT_TXD_PKT_INFO_80211

    def test_wiv_bit_is_set_for_the_unkeyed_monitor_slot(self) -> None:
        """init.c:591 leaves the monitor WCID's hw_key_idx at -1, so dma.c adds WIV."""
        out = build_tx_dma(CONTROL_FRAME, ack=True)
        info = int.from_bytes(out[:4], "little")
        assert info & C.MT_TXD_PKT_INFO_WIV

    def test_queue_index_is_not_carried_in_qsel(self) -> None:
        """Index 2 happens to equal MT_QSEL_EDCA, so this used to pass by
        coincidence. dma.c ep2dmaq maps by rule, not by copy."""
        out = build_tx_dma(CONTROL_FRAME, ack=True, queue=2)
        info = int.from_bytes(out[:4], "little")
        assert C._field_get(C.MT_TXD_PKT_INFO_QSEL, info) == C.MT_QSEL_EDCA

    def test_the_inband_command_endpoint_is_refused(self) -> None:
        """dma.c:355 q2ep returns qid + 1, so a frame endpoint is never 0. Endpoint 0 is
        the MCU command pipe, and frame bytes on it stop the receive stream."""
        with pytest.raises(ValueError, match="inband command pipe"):
            build_tx_dma(CONTROL_FRAME, ack=True, queue=TX_QUEUE_INBAND_CMD)
        # ep2dmaq itself still has no inband case: the QSEL derivation is unchanged.
        assert dma_queue_for_endpoint(TX_QUEUE_INBAND_CMD) == C.MT_QSEL_EDCA

    def test_out_of_range_queue_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="queue"):
            build_tx_dma(CONTROL_FRAME, ack=True, queue=6)

    def test_byte_count_is_the_unpadded_frame_length(self) -> None:
        """tx.c captures pkt_len before mt76_insert_hdr_pad adds alignment."""
        frame = QOS_DATA_FRAME + bytes(8)              # 32 bytes, 24-byte header
        out = build_tx_dma(frame, ack=True)
        f = fields(out[4:4 + TXWI_LEN])
        assert C._field_get(C.MT_TXWI_LEN_BYTE_CNT, f["len_ctl"]) == len(frame)

    def test_pad_bytes_sit_between_the_header_and_the_frame_body(self) -> None:
        """CONTROL_FRAME's header is 10 (2 mod 4), so two pad bytes follow it."""
        frame = CONTROL_FRAME + bytes(12)       # 22 bytes; 10-byte header, pad 2
        out = build_tx_dma(frame, ack=True)
        body = out[4 + TXWI_LEN:-TRAILER_LEN]     # exclude the zero trailer
        assert body[:10] == frame[:10]
        assert body[10:12] == b"\x00\x00"
        assert body[12:] == frame[10:]           # header, pad, then the remainder
        assert len(body) == len(frame) + 2

    def test_a_management_frame_arrives_unshifted(self) -> None:
        """The live-TX bug. A deauth's header is 24 bytes and 0 mod 4, so no pad
        is inserted and every address stays where the frame put it. Reading the
        header as 10 bytes instead spliced two zero bytes into Addr2, which moved
        Addr3, the sequence number and the whole body along with it -- and left
        the descriptor length and the transfer size both unchanged, so nothing
        upstream could see the damage."""
        addr1, addr2, addr3 = b"\xff" * 6, b"\x0a\x73\x0a\x25\x07\xd3", b"\xff" * 6
        frame = (b"\xc0\x00" + bytes([0, 0]) + addr1 + addr2 + addr3
                 + bytes([0, 0, 0, 42]) + b"\x07\x00")
        out = build_tx_dma(frame, ack=True)
        assert len(frame) == 28
        body = out[4 + TXWI_LEN:-TRAILER_LEN]
        assert body == frame, "header pad spliced into the frame"

    def test_an_aligned_header_is_followed_by_the_frame_directly(self) -> None:
        frame = QOS_DATA_FRAME + bytes(8)
        out = build_tx_dma(frame, ack=True)
        assert out[4 + TXWI_LEN:4 + TXWI_LEN + len(frame)] == frame


class TestEndpointAndQsel:
    """Which OUT endpoint a frame goes out on, and what QSEL it carries.

    Both are load-bearing and neither is visible in the txwi: the endpoint is
    only observable in the USB transfer, and QSEL is 2 bits naming a hardware
    queue. Getting either wrong still reports TX success while nothing is
    modulated, which is how an injected frame can look successful and silent.
    """

    def test_an_injected_frame_lands_on_the_endpoint_tx_c_derives(self) -> None:
        """tx.c: an unclassified skb is mac80211 queue 0; q2hwq(0) is 0 ^ 3 = 3,
        and q2ep(3) is 3 + 1 = 4. Index 4, not the one named AC_BE."""
        assert TX_QUEUE_INJECT == 4

    def test_capture_6_confirms_that_endpoint(self) -> None:
        """capture-6-kernel-tx records the kernel sending 7680 deauths, and every
        usbmon line reads `Bo:1:015:7` -- endpoint 7. Descriptor order puts 7 at
        index 4 of out_eps, which is where the kernel's q2ep(3) sends. This is the
        measurement the fix rests on: the port used index 2 (endpoint 5) because
        usb.h labels it AC_BE, and frames there are accepted but never modulated."""
        assert OUT_EP_DESCRIPTOR_ORDER[TX_QUEUE_INJECT] == 0x07
        assert OUT_EP_DESCRIPTOR_ORDER[2] == 0x05
        assert OUT_EP_DESCRIPTOR_ORDER[2] != CAPTURE_6_TX_ENDPOINT

    def test_qsel_is_derived_from_the_endpoint_not_copied_from_it(self) -> None:
        """dma.c ep2dmaq returns MGMT for endpoint 5 and EDCA for every other,
        so the QSEL field must not carry the endpoint index verbatim."""
        assert dma_queue_for_endpoint(5) == C.MT_QSEL_MGMT
        assert dma_queue_for_endpoint(0) == C.MT_QSEL_EDCA
        assert dma_queue_for_endpoint(4) == C.MT_QSEL_EDCA

    def test_the_default_frame_reports_the_best_effort_queue(self) -> None:
        info = int.from_bytes(build_tx_dma(QOS_DATA_FRAME)[:4], "little")
        assert C._field_get(C.MT_TXD_PKT_INFO_QSEL, info) == C.MT_QSEL_EDCA

    def test_a_management_endpoint_reports_the_management_queue(self) -> None:
        """The only endpoint index that is not EDCA. QSEL is 2 bits, so an
        endpoint index written straight into it would silently truncate."""
        info = int.from_bytes(build_tx_dma(QOS_DATA_FRAME, queue=5)[:4], "little")
        assert C._field_get(C.MT_TXD_PKT_INFO_QSEL, info) == C.MT_QSEL_MGMT

    def test_the_qsel_field_holds_every_endpoint_index_without_truncation(self) -> None:
        """Index 4 in a 2-bit field would be masked off to 0 and the chip would
        be told MGMT. Deriving instead of copying is what keeps this from
        happening for the default index."""
        for endpoint in range(TX_QUEUE_INBAND_CMD + 1, TX_QUEUE_COUNT):
            info = int.from_bytes(
                build_tx_dma(QOS_DATA_FRAME, queue=endpoint)[:4], "little")
            assert C._field_get(C.MT_TXD_PKT_INFO_QSEL, info) == \
                dma_queue_for_endpoint(endpoint)

    def test_an_endpoint_outside_the_table_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside"):
            dma_queue_for_endpoint(TX_QUEUE_COUNT)

class TestAckRequestIsAlwaysOn:
    """mt76x0u:999 and mt76x2u:684 request the ACK on every injected frame on this same
    txwi, group addr1 included. The MAC's ACK-based retry is injection's only retransmission."""

    @staticmethod
    def _ack_ctl(out: bytes) -> int:
        return out[DMA_INFO_LEN + 4]           # txwi byte 4 is ack_ctl

    def test_a_broadcast_frame_still_requests_an_ack(self) -> None:
        frame = bytes([0xC0, 0x00, 0x00, 0x00]) + bytes([0xFF] * 6) + bytes(14)
        out = build_tx_dma(frame, ack=True)
        assert self._ack_ctl(out) & C.MT_TXWI_ACK_CTL_REQ

    def test_a_multicast_frame_still_requests_an_ack(self) -> None:
        frame = bytes([0xC0, 0x00, 0x00, 0x00, 0x01, 0x00, 0x5E, 0x01, 0x02, 0x03]) + bytes(14)
        out = build_tx_dma(frame, ack=True)
        assert self._ack_ctl(out) & C.MT_TXWI_ACK_CTL_REQ

    def test_the_replay_form_clears_it(self) -> None:
        """ack=False is replay-only: it is how verify_tx byte-matches the aireplay capture."""
        frame = bytes([0xC0, 0x00, 0x00, 0x00, 0x02, 0x00, 0x5E, 0x01, 0x02, 0x03]) + bytes(14)
        out = build_tx_dma(frame, ack=False)
        assert not self._ack_ctl(out) & C.MT_TXWI_ACK_CTL_REQ

    def test_a_unicast_frame_still_requests_one_when_asked(self) -> None:
        frame = bytes([0xC0, 0x00, 0x00, 0x00, 0x02, 0x00, 0x5E, 0x01, 0x02, 0x03]) + bytes(14)
        out = build_tx_dma(frame, ack=True)
        assert self._ack_ctl(out) & C.MT_TXWI_ACK_CTL_REQ


class TestRateControlledSentinel:
    def test_it_resolves_to_the_wcid_stored_rate(self) -> None:
        """tx.c:151 takes wcid->tx_rate when rate->idx < 0. Masking 0xff onto the
        7-bit MCS field instead would emit MCS 127 on the CCK PHY."""
        f = fields(build_txwi(wcid=TX_NO_STATION, length=10, rate=RATE_CONTROLLED))
        assert f["rate_ctl"] == WCID_STORED_RATE

    def test_the_packet_id_comes_from_the_resolved_rate(self) -> None:
        """tx.c:183 encodes rate_ctl & 0x7, not the caller's sentinel."""
        f = fields(build_txwi(wcid=TX_NO_STATION, length=10, rate=RATE_CONTROLLED))
        assert C._field_get(C.MT_TXWI_LEN_PKTID, f["len_ctl"]) == packet_id(0, False)

    def test_the_rate_field_is_seven_bits_wide(self) -> None:
        """MT_TXWI_RATE_MCS is GENMASK(6, 0) (mac.h); the 3-bit limit belongs to the
        pktid encoder, not to rate_ctl."""
        f = fields(build_txwi(wcid=TX_NO_STATION, length=10, rate=0x40))
        assert f["rate_ctl"] == 0x40
        with pytest.raises(ValueError, match="MT_TXWI_RATE_MCS"):
            build_txwi(wcid=TX_NO_STATION, length=10, rate=0x80)
