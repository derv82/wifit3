"""rtl8822bu TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, plus the inject descriptor's HW ACK-retry wiring. No hardware — synthetic frames.

The tap lives in _rx_dispatch (the parser drops control frames like the ACK before they reach the
callback). ``_enable_rx_acks`` opens RXFLTMAP1 bit13; the tally and arming live on the ``Driver``
base (``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
import struct
from unittest.mock import MagicMock

import wifit3.chips.rtl8822bu.driver as drv
from wifit3.chips.rtl8822bu.driver import RTL8822BUDriver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN buffer with one frame: a 14-B on-wire ACK to ``ra`` (10-B MPDU + 4-B HW FCS,
    which iter_bulk_frames strips). 24-B rx_pkt_desc: rxdw0 pkt_len=14, no drvinfo/shift."""
    desc = bytearray(24)
    struct.pack_into("<I", desc, 0, 14)         # rxdw0: pkt_len=14, all flags clear
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    return bytes(desc) + bytes(mpdu) + b"\x00\x00\x00\x00"


def _driver() -> RTL8822BUDriver:
    d = RTL8822BUDriver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True         # arm the base tally (bypasses the RXFLTMAP1 register write)
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 1
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 0     # armed, but not one of ours


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._rx_dispatch(_ack_buf(ra))    # _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = b"\xc0\x00\x00\x00" + b"\x11" * 6 + b"\x22" * 6 + b"\x33" * 6 + b"\x00\x00"
    assert d._stamp_tx_seq(frame) is frame      # Realtek HW-stamps; frame goes out unchanged


async def test_inject_builds_descriptor_with_hw_retry_limit(monkeypatch):
    d = _driver()
    d._bulk_out_eps = [0x05, 0x06, 0x08]
    sent: list[bytes] = []

    def _fake_write(dev, ep, payload, timeout_ms=200):
        sent.append(bytes(payload))
        return len(payload)

    monkeypatch.setattr(drv, "write_bulk", _fake_write)   # module-level ref used by _inject_frame
    frame = b"\xc0\x00\x00\x00" + b"\x11" * 6 + b"\x22" * 6 + b"\x33" * 6 + b"\x00\x00"
    assert await d.inject_frame(frame) is True
    assert len(sent) == 1
    desc = sent[0][:48]
    assert (int.from_bytes(desc[0x10:0x14], "little") >> 17) & 1 == 0   # RTY_LMT_EN clear -> HW global retry
    assert sent[0][48:] == frame                # HW-stamp: 48-B descriptor then frame unchanged
