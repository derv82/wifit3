"""rtl8814au_dkms TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a
MAC we inject as, plus the inject descriptor's HW ACK-retry wiring. No hardware (synthetic frames).

The 8814au decodes on the reader thread, so the tap lives in ``_read_once`` (raw frames), not
``_dispatch`` (parsed dicts): the parser drops control frames like the ACK before _dispatch. The
monitor default accept-alls RXFLTMAP1, so ``_enable_rx_acks`` is a documented no-op; the tally and
arming live on the ``Driver`` base (``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
import struct
from unittest.mock import MagicMock

from wifit3.chips.rtl8814au_dkms import tx
from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN buffer with one NORMAL_RX packet: a 14-B on-wire ACK to ``ra`` (10-B MPDU
    + 4-B HW FCS, which iter_frames strips). 24-B RX desc: rxdw0 pkt_len=14, no drvinfo/shift."""
    desc = bytearray(24)
    struct.pack_into("<I", desc, 0, 14)         # rxdw0: pkt_len=14, all flags clear
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    return bytes(desc) + bytes(mpdu) + b"\x00\x00\x00\x00"


def _deauth(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 26-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl, reason."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00" + b"\x07\x00")


def _driver() -> Rtl8814auDkmsDriver:
    return Rtl8814auDkmsDriver(MagicMock())


async def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()                    # arms the base tally (clears _our_tx_macs)
    d._our_tx_macs.add(ra)
    d.transport.bulk_in.return_value = _ack_buf(ra)
    assert d._read_once() is None       # the ACK is tapped, never yielded as a parsed frame
    assert d.acks_seen(ra) == 1


async def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    await d.enable_rx_acks()
    d.transport.bulk_in.return_value = _ack_buf(ra)
    assert d._read_once() is None
    assert d.acks_seen(ra) == 0         # armed, but not one of ours


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d.transport.bulk_in.return_value = _ack_buf(ra)   # _ack_detect_on stays False
    assert d._read_once() is None       # ACK is a control frame -> the parser drops it anyway
    assert d.acks_seen(ra) == 0


async def test_disable_rx_acks_stops_the_tally():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()
    d._our_tx_macs.add(ra)
    await d.disable_rx_acks()
    d.transport.bulk_in.return_value = _ack_buf(ra)
    assert d._read_once() is None
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = _deauth()
    assert d._stamp_tx_seq(frame) is frame      # Realtek HW-stamps; frame goes out unchanged


async def test_inject_builds_descriptor_with_hw_retry_limit():
    d = _driver()
    frame = _deauth()
    assert await d.inject_frame(frame) is True   # base entry -> _stamp_tx_seq -> _inject_frame
    sent = d.transport.bulk_out.call_args.args[0]
    rty = (int.from_bytes(sent[0x10:0x14], "little") >> 18) & 0x3F   # DATA_RETRY_LIMIT
    assert rty == d.DEFAULT_HW_ACK_RETRIES
    assert sent[tx.TXDESC_SIZE:] == frame        # HW-stamp: payload byte-for-byte unchanged
