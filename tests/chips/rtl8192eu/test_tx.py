"""TX path: txdesc40 queue routing, XOR-16 checksum, frame builders, bulk-OUT."""
from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest

from wifit3.chips.rtl8192eu.constants import (
    REASON_CODE_CLASS3_FRAME,
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_BEACON,
    TXDESC_QUEUE_BK,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_SHIFT,
    TXDESC_QUEUE_VI,
    TXDESC_QUEUE_VO,
)
from wifit3.chips.rtl8192eu.tx import (
    TXDESC40_RETRY_LIMIT_MGNT,
    TX_DESC_SZ_40,
    build_deauth,
    build_tx_desc,
    calc_tx_desc_csum,
    queue_for_frame,
    send_frame,
)

BSSID = bytes.fromhex("c006c3c9ec0b")
CLIENT = bytes.fromhex("d03f27223eef")


def word(desc: bytes, index: int) -> int:
    return struct.unpack_from("<I", desc, index * 4)[0]


def deauth() -> bytes:
    return build_deauth(BSSID, CLIENT)


def beacon(size: int = 80) -> bytes:
    """A Beacon: MGMT type 0, subtype 8 (`fc[0] = 0x80`)."""
    frame = bytearray(size)
    frame[0] = 0x80
    frame[4:10] = b"\xff" * 6
    frame[10:16] = BSSID
    frame[16:22] = BSSID
    return bytes(frame)


def data_frame(
    *, qos_tid: int | None = None, to_ds: bool = True, from_ds: bool = False,
    seq: int = 0, addr4: bool = False,
) -> bytes:
    """A DATA frame (type 2): 24 B, +2 for QoS, +6 when addr4 is present."""
    header = bytearray(24 + (6 if addr4 else 0) + (2 if qos_tid is not None else 0))
    header[0] = 0x08 if qos_tid is None else 0x88     # subtype 0 = Data, 8 = QoS Data
    header[1] = (1 if to_ds else 0) | (2 if from_ds else 0)
    header[4:10] = BSSID
    header[10:16] = CLIENT
    header[16:22] = BSSID
    struct.pack_into("<H", header, 22, seq << 4)
    if addr4:
        header[24:30] = BSSID
    if qos_tid is not None:
        header[-2] = qos_tid
    return bytes(header)


# ---- queue selection (`rtl8xxxu_queue_select`) --------------------------


def test_mgmt_frames_route_to_the_mgmt_queue():
    assert queue_for_frame(deauth()) == TXDESC_QUEUE_MGNT
    # Probe Request: MGMT type 0, subtype 4 (`fc[0] = 0x40`) — not a beacon.
    assert queue_for_frame(b"\x40\x00" + b"\x00" * 22) == TXDESC_QUEUE_MGNT


def test_a_beacon_routes_to_the_beacon_queue():
    assert queue_for_frame(beacon()) == TXDESC_QUEUE_BEACON


def test_non_qos_data_routes_to_be():
    assert queue_for_frame(data_frame()) == TXDESC_QUEUE_BE


@pytest.mark.parametrize(
    ("tid", "queue"),
    [
        (0, TXDESC_QUEUE_BE),
        (1, TXDESC_QUEUE_BK),
        (2, TXDESC_QUEUE_BK),
        (3, TXDESC_QUEUE_BE),
        (4, TXDESC_QUEUE_VI),
        (5, TXDESC_QUEUE_VI),
        (6, TXDESC_QUEUE_VO),
        (7, TXDESC_QUEUE_VO),
        (8, TXDESC_QUEUE_BE),   # mac80211 masks the TID with 7 -> 0 -> BE
        (15, TXDESC_QUEUE_VO),  # ... -> 7 -> VO
    ],
)
def test_qos_data_maps_its_tid_to_an_ac(tid, queue):
    assert queue_for_frame(data_frame(qos_tid=tid)) == queue


def test_qos_control_is_read_after_addr4_on_a_wds_frame():
    assert queue_for_frame(data_frame(qos_tid=6)) == TXDESC_QUEUE_VO
    assert queue_for_frame(data_frame(qos_tid=6, to_ds=True, from_ds=True, addr4=True)) == (
        TXDESC_QUEUE_VO
    )
    # With addr4 present, byte 24 is addr4[0] (= BSSID[0] = 0xc0 → TID 0 → BE if the
    # offset were hard-coded); the real QoS byte sits at 30.
    assert queue_for_frame(data_frame(qos_tid=1, to_ds=True, from_ds=True, addr4=True)) == (
        TXDESC_QUEUE_BK
    )


def test_queue_for_frame_rejects_a_frame_without_frame_control():
    with pytest.raises(ValueError, match="too short"):
        queue_for_frame(b"\x08")


def test_control_frames_take_the_data_default():
    # RTS: type 1. The kernel's `else` branch is the data path (`core.c:5026-5027`).
    assert queue_for_frame(b"\xb4\x00" + b"\x00" * 14) == TXDESC_QUEUE_BE


# ---- descriptor ---------------------------------------------------------


def test_descriptor_is_40_bytes_with_offset_40():
    desc = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False)
    assert len(desc) == TX_DESC_SZ_40 == 40
    assert struct.unpack_from("<H", desc, 0)[0] == 26   # pkt_size
    assert desc[2] == 40                                # pkt_offset
    assert desc[3] == 0x8C                              # OWN | FIRST | LAST


def test_broadcast_sets_bmc_in_txdw0():
    unicast = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False)
    broadcast = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=True)
    assert unicast[3] == 0x8C
    assert broadcast[3] == 0x8D


def test_txdw1_routes_the_mgmt_queue():
    desc = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False)
    assert word(desc, 1) == TXDESC_QUEUE_MGNT << TXDESC_QUEUE_SHIFT
    assert word(desc, 1) == 0x1200


def test_txdw1_routes_the_beacon_queue():
    frame = beacon()
    desc = build_tx_desc(frame, queue_for_frame(frame), is_broadcast=True)
    assert word(desc, 1) == TXDESC_QUEUE_BEACON << TXDESC_QUEUE_SHIFT == 0x1000


def test_txdw2_is_agg_break():
    assert word(build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False), 2) == 1 << 16


def test_txdw3_uses_driver_rate_for_mgmt_only():
    assert word(build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False), 3) == 1 << 8
    assert word(build_tx_desc(beacon(), TXDESC_QUEUE_BEACON, is_broadcast=True), 3) == 1 << 8
    assert word(build_tx_desc(data_frame(), TXDESC_QUEUE_BE, is_broadcast=False), 3) == 0


def test_txdw4_carries_the_retry_limit_and_enable_for_mgmt():
    desc = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False)
    expected = (TXDESC40_RETRY_LIMIT_MGNT << 18) | (1 << 17)
    assert word(desc, 4) == expected == 0x001A0000

    custom = build_tx_desc(
        deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False, retry_limit=10
    )
    assert word(custom, 4) == (10 << 18) | (1 << 17)


def test_txdw4_carries_the_data_rate_fallback_for_data():
    desc = build_tx_desc(data_frame(), TXDESC_QUEUE_BE, is_broadcast=False)
    assert word(desc, 4) == 0x1F << 8 == 0x1F00


def test_txdw3_and_txdw4_stay_zero_for_control_frames():
    # `fill_txdesc_v2` sets DATA_RATE_FB only for `ieee80211_is_data` and USE_DRIVER_RATE
    # only for `ieee80211_is_mgmt`, so a control frame gets neither (`core.c:5283-5301`).
    rts = b"\xb4\x00" + b"\x00" * 14
    desc = build_tx_desc(rts, queue_for_frame(rts), is_broadcast=False)
    assert word(desc, 3) == 0
    assert word(desc, 4) == 0


def test_txdw5_through_txdw8_are_zero_on_the_raw_inject_path():
    desc = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False)
    for index in range(5, 9):
        assert word(desc, index) == 0, f"txdw{index} should be 0"


def test_txdw9_carries_the_frames_sequence_number():
    assert word(build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False), 9) == 0
    sequenced = build_tx_desc(data_frame(seq=5), TXDESC_QUEUE_BE, is_broadcast=False)
    assert word(sequenced, 9) == 5 << 12


def test_txdw9_stays_zero_when_the_frame_cannot_carry_seq_ctrl():
    assert word(build_tx_desc(b"\x08\x00", TXDESC_QUEUE_BE, is_broadcast=False), 9) == 0


# ---- checksum -----------------------------------------------------------


def test_checksum_is_xor16_over_the_first_32_bytes():
    desc = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False)
    expected = 0
    for i in range(0, 32, 2):
        expected ^= int.from_bytes(desc[i : i + 2], "little")

    calc_tx_desc_csum(desc)
    assert struct.unpack_from("<H", desc, 28)[0] == expected & 0xFFFF


def test_checksum_ignores_bytes_after_the_first_32():
    desc = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=True)
    calc_tx_desc_csum(desc)
    before = bytes(desc)

    desc[32:40] = b"\xFF" * 8
    calc_tx_desc_csum(desc)
    assert desc[28:30] == before[28:30]


def test_checksum_zeroes_a_stale_csum_field_first():
    desc = build_tx_desc(deauth(), TXDESC_QUEUE_MGNT, is_broadcast=False)
    desc[28:30] = b"\xDE\xAD"
    calc_tx_desc_csum(desc)
    assert desc[28:30] != b"\xDE\xAD"


# ---- deauth builder -----------------------------------------------------


def test_build_deauth_layout():
    frame = build_deauth(BSSID, CLIENT, reason=REASON_CODE_CLASS3_FRAME)
    assert len(frame) == 26
    assert frame[0] == 0xC0                   # MGMT | deauth
    assert frame[1] == 0x00                   # no ToDS/FromDS/flags
    assert frame[2:4] == b"\x3A\x01"           # duration 0x013A
    assert frame[4:10] == CLIENT               # addr1 = destination
    assert frame[10:16] == BSSID               # addr2 = source (spoofed AP)
    assert frame[16:22] == BSSID               # addr3 = BSSID
    assert frame[22:24] == b"\x00\x00"         # seq_ctrl
    assert frame[24:26] == b"\x07\x00"         # reason 7, little-endian


def test_build_deauth_rejects_bad_addresses():
    with pytest.raises(ValueError, match="bssid"):
        build_deauth(b"\x00" * 5, CLIENT)
    with pytest.raises(ValueError, match="client"):
        build_deauth(BSSID, b"\x00" * 7)


# ---- bulk-OUT -----------------------------------------------------------


def test_send_frame_prefixes_the_40_byte_descriptor():
    dev = MagicMock()
    dev.write.side_effect = lambda ep, data, timeout: len(data)
    frame = build_deauth(BSSID, CLIENT)

    written = send_frame(dev, 0x02, frame, queue=TXDESC_QUEUE_MGNT)

    assert written == 66
    ep, urb, _timeout = dev.write.call_args.args
    assert ep == 0x02
    assert len(urb) == 40 + 26
    assert urb[40:] == frame
    assert urb[2] == 40
    assert struct.unpack_from("<I", urb, 4)[0] == TXDESC_QUEUE_MGNT << TXDESC_QUEUE_SHIFT


def test_send_frame_routes_the_queue_it_is_given():
    dev = MagicMock()
    dev.write.side_effect = lambda ep, data, timeout: len(data)
    frame = data_frame()

    send_frame(dev, 0x08, frame, queue=TXDESC_QUEUE_BE)

    _ep, urb, _timeout = dev.write.call_args.args
    assert struct.unpack_from("<I", urb, 4)[0] == 0
    assert struct.unpack_from("<I", urb, 12)[0] == 0          # no USE_DRIVER_RATE
    assert struct.unpack_from("<I", urb, 16)[0] == 0x1F00     # data rate fallback


def test_send_frame_raises_on_short_write():
    dev = MagicMock()
    dev.write.return_value = 10
    with pytest.raises(IOError, match="short write"):
        send_frame(dev, 0x02, build_deauth(BSSID, CLIENT), queue=TXDESC_QUEUE_MGNT)
