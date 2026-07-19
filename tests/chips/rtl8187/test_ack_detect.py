"""rtl8187 TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, plus the inject path's HW ACK-retry + software seq wiring. No hardware (synthetic frames).

The 8187L admits ACK control frames in monitor by default (RX_CONF_CTRL is set in
mac.configure_filter, = FIF_CONTROL), so ``_enable_rx_acks`` is a documented no-op. The tally and
arming live on the ``Driver`` base (``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
import struct
from unittest.mock import MagicMock

from wifit3.chips.rtl8187.driver import RTL8187Driver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN URB with one 8187L frame: a 14-B on-wire ACK to ``ra`` (10-B MPDU + 4-B
    FCS, which parse_rx_urb strips) followed by the 16-B trailing rtl8187_rx_hdr. The
    trailer flags[0:11] hold the FCS-inclusive length (14) with the CRC-error bit clear."""
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    frame = bytes(mpdu) + b"\x00\x00\x00\x00"  # 10-B MPDU + 4-B on-air FCS
    # rx_hdr: <I flags, B noise, B signal, B agc, B reserved, Q mac_time>
    trailer = struct.pack("<IBBBBQ", 14, 0, 0, 0, 0, 0)
    return frame + trailer


def _mgmt_frame(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 24-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl=0."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00")


def _driver() -> RTL8187Driver:
    d = RTL8187Driver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


async def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()                    # arms the base tally (clears _our_tx_macs)
    d._our_tx_macs.add(ra)
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 1
    assert d._parsed == []                      # an ACK is never handed to the frame parser


async def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    await d.enable_rx_acks()
    d._rx_dispatch(_ack_buf(ra))                # armed, but ra is not one of ours
    assert d.acks_seen(ra) == 0


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._rx_dispatch(_ack_buf(ra))                # never enabled -> _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_disable_rx_acks_stops_the_tally():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    await d.enable_rx_acks()
    d._our_tx_macs.add(ra)
    await d.disable_rx_acks()
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_advances_and_writes_seqctl():
    d = _driver()
    out = d._stamp_tx_seq(_mgmt_frame())
    assert (out[22] | (out[23] << 8)) == 0x10   # first frame -> seq 1 (bits [4:15]), frag 0
    assert d._tx_seqno == 0x10
    out2 = d._stamp_tx_seq(_mgmt_frame())
    assert (out2[22] | (out2[23] << 8)) == 0x20  # next frame advances one sequence


async def test_inject_wires_hw_retry_limit():
    d = _driver()
    sent: list[bytes] = []
    d.dev.write = lambda ep, payload, timeout: (sent.append(bytes(payload)), len(payload))[1]
    frame = _mgmt_frame()
    assert await d.inject_frame(frame) is True   # base entry -> _stamp_tx_seq -> _inject_frame
    assert len(sent) == 1
    retry = int.from_bytes(sent[0][8:12], "little")            # tx_hdr __le32 retry = (n-1)<<8
    assert ((retry >> 8) & 0xFF) == 7 - 1                      # RETRY_COUNT=7 -> tx_hdr (n-1)
