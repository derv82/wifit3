"""rtw88_8814au TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, plus the inject descriptor's HW ACK-retry wiring. No hardware (synthetic frames).

mac_init_for_rx leaves RXFLTMAP1=0x0400 (ACK bit13 clear), so ``_enable_rx_acks`` is a real
register write (admit ctrl subtype 13); the tap lives in ``_rx_dispatch`` (the parser drops the
ACK before it reaches the callback). The tally and arming live on the ``Driver`` base
(``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
import struct
from unittest.mock import MagicMock

from wifit3.chips.rtw88_8814au.constants import TX_PKT_DESC_SZ
from wifit3.chips.rtw88_8814au.driver import RTL8814AUDriver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN buffer with one frame: a 14-B on-wire ACK to ``ra`` (10-B MPDU + 4-B HW FCS,
    which iter_bulk_frames strips). 24-B rx_pkt_desc: rxdw0 pkt_len=14, no drvinfo/shift."""
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


def _driver() -> RTL8814AUDriver:
    d = RTL8814AUDriver(MagicMock())
    # admit_ack_frames/drop_ack_frames read-modify-write RXFLTMAP1; stub the transport regs
    # (0x0400 = the ACK-bit-clear default) so _enable_rx_acks runs without real USB.
    d.transport.read16 = lambda addr: 0x0400
    d.transport.write16 = lambda addr, val: None
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


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = _mgmt_frame()
    assert d._stamp_tx_seq(frame) is frame      # Realtek HW-stamps; frame goes out unchanged


async def test_inject_builds_descriptor_with_hw_retry_limit():
    d = _driver()
    d._bulk_out_eps = [0x02]                     # HIGH lane -> out_ep[0]
    sent: list[bytes] = []

    def _write(ep, payload, timeout):
        sent.append(bytes(payload))
        return len(payload)

    d.dev.write = _write
    frame = _mgmt_frame()
    assert await d.inject_frame(frame) is True   # base entry -> _stamp_tx_seq -> _inject_frame
    assert len(sent) == 1
    pkt = sent[0]
    w4 = int.from_bytes(pkt[0x10:0x14], "little")
    assert (w4 >> 17) & 1 == 0                              # RTY_LMT_EN clear -> HW global retry
    assert pkt[TX_PKT_DESC_SZ:] == frame                    # HW-stamp: payload byte-for-byte
