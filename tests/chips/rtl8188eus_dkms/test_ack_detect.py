"""rtl8188eus_dkms TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a
MAC we inject as, plus the inject descriptor's HW ACK-retry wiring. No hardware (synthetic frames).

``_enable_rx_acks`` opens RXFLTMAP1 bit13 (the 8188e defaults to filtering ACKs); the tap lives
in ``_dispatch`` (raw frames), before the parser drops the ACK control frame. The tally and
arming live on the ``Driver`` base (``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
import struct
from unittest.mock import MagicMock

from wifit3.chips.rtl8188eus_dkms import tx
from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN buffer with one NORMAL_RX packet: a 14-B on-wire ACK to ``ra`` (10-B MPDU
    + 4-B HW FCS, which iter_frames strips). RX desc rxdw0 = pkt_len 14, no drvinfo/shift."""
    desc = bytearray(24)
    struct.pack_into("<I", desc, 0, 14)         # rxdw0: pkt_len=14, all flags clear
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    return bytes(desc) + bytes(mpdu) + b"\x00\x00\x00\x00"


def _mgmt_frame(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 24-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00")


def _driver() -> Rtl8188eusDkmsDriver:
    d = Rtl8188eusDkmsDriver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


async def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()                    # arms the base tally (clears _our_tx_macs)
    d._our_tx_macs.add(ra)
    d._dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 1
    assert d._parsed == []                      # an ACK is never handed to the frame parser


async def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    await d.enable_rx_acks()
    d._dispatch(_ack_buf(ra))                   # armed, but ra is not one of ours
    assert d.acks_seen(ra) == 0


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._dispatch(_ack_buf(ra))                   # never enabled -> _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_disable_rx_acks_stops_the_tally():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()
    d._our_tx_macs.add(ra)
    await d.disable_rx_acks()
    d._dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = _mgmt_frame()
    assert d._stamp_tx_seq(frame) is frame      # Realtek HW-stamps; frame goes out unchanged


async def test_inject_builds_descriptor_with_hw_retry_limit():
    d = _driver()
    sent: list[bytes] = []
    d.transport.bulk_out = lambda pkt: sent.append(pkt)
    frame = _mgmt_frame()
    assert await d.inject_frame(frame) is True   # base entry -> _stamp_tx_seq -> _inject_frame
    assert len(sent) == 1
    pkt = sent[0]
    rty = (int.from_bytes(pkt[20:24], "little") >> 18) & 0x3F    # txdw5 DATA_RETRY_LIMIT
    assert rty == d.DEFAULT_HW_ACK_RETRIES
    assert pkt[tx.TXDESC_SIZE:] == frame         # HW-stamp: payload byte-for-byte unchanged
