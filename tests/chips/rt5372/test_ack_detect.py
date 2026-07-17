"""rt5372 TX-ACK detection on the redesigned Driver base: the RX tap that counts the AP's
link-layer ACKs to a MAC we inject as, the software seq-stamp, and the ACK-bit inject wiring.
No hardware — synthetic frames. The Ralink monitor RX filter (RX_FILTER_CFG=0x93) already admits
ACKs, so ``_enable_rx_acks`` is a documented no-op; the tally + arming live on the ``Driver`` base
(``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
from unittest.mock import MagicMock

import wifit3.chips.rt5372.driver as drv


def _ack_mpdu(ra: bytes) -> bytes:
    """A 10-byte on-wire ACK to ``ra``: FC=0xD4 (control/ACK) + duration + addr1/RA."""
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    return bytes(mpdu)


def _mgmt_frame(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 24-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00")


def _driver(monkeypatch, ra: bytes) -> drv.RT5372Driver:
    d = drv.RT5372Driver(MagicMock())
    monkeypatch.setattr(drv, "iter_frames", lambda buf, ev, lna: [(_ack_mpdu(ra), -40)])
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


async def test_tap_counts_ack_to_our_mac(monkeypatch):
    ra = bytes.fromhex("020000000001")
    d = _driver(monkeypatch, ra)
    await d.enable_rx_acks()                    # arms the base tally (clears _our_tx_macs)
    d._our_tx_macs.add(ra)
    d._dispatch(b"BULK")
    assert d.acks_seen(ra) == 1
    assert d._parsed == []          # an ACK is never handed to the frame parser


async def test_tap_ignores_ack_to_foreign_mac(monkeypatch):
    ra = bytes.fromhex("aabbccddeeff")
    d = _driver(monkeypatch, ra)
    await d.enable_rx_acks()
    d._dispatch(b"BULK")            # armed, but ra is not one of ours
    assert d.acks_seen(ra) == 0


def test_tap_off_by_default(monkeypatch):
    ra = bytes.fromhex("020000000001")
    d = _driver(monkeypatch, ra)
    d._our_tx_macs.add(ra)
    d._dispatch(b"BULK")            # never enabled -> _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_disable_rx_acks_stops_the_tally(monkeypatch):
    ra = bytes.fromhex("020000000001")
    d = _driver(monkeypatch, ra)
    await d.enable_rx_acks()
    d._our_tx_macs.add(ra)
    await d.disable_rx_acks()
    d._dispatch(b"BULK")
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_increments_and_copies():
    d = drv.RT5372Driver(MagicMock())
    frame = _mgmt_frame()
    first = d._stamp_tx_seq(frame)
    second = d._stamp_tx_seq(frame)
    assert first is not frame                   # returns a copy; caller's bytes untouched
    assert frame[22:24] == b"\x00\x00"
    assert first[22:24] == b"\x00\x00"          # seq 0 -> seqctl 0x0000
    assert second[22:24] == b"\x10\x00"         # seq 1 -> (1 << 4) little-endian


async def test_inject_sets_ack_bit_and_stamps_seq(monkeypatch):
    d = drv.RT5372Driver(MagicMock())
    d._bulk_out_ep = 0x01
    sent: list = []
    monkeypatch.setattr(
        drv.tx, "send_frame",
        lambda dev, ep, frame, *, use_no_ack=True, timeout_ms=1000: sent.append((frame, use_no_ack)),
    )
    assert await d.inject_frame(_mgmt_frame()) is True   # base -> _stamp_tx_seq -> _inject_frame
    assert len(sent) == 1
    frame, use_no_ack = sent[0]
    assert use_no_ack is False                  # TXWI ACK bit ON (frame requests the AP's ACK)
    assert frame[22:24] == b"\x00\x00"          # base stamped the seq before the send
