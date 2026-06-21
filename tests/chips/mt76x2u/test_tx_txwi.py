"""mt76x2u TXWI build — must match the inject frames seen on the wire.

Ground truth: capture-1 frame 32207 (aireplay-ng directed deauth) decodes
to rate=0x0000 (CCK 1 Mbps), wcid=0xff, txstream=0x13 (2x2, rev>=E4),
pktid=0x00. The struct is 20 bytes, little-endian, field order:
flags(le16), rate(le16), ack_ctl(u8), wcid(u8), len_ctl(le16),
iv(le32), eiv(le32), aid(u8), txstream(u8), ctl2(u8), pktid(u8).
"""
import struct

from wifit3.chips.mt76x2u import tx


def _decode(txwi: bytes) -> dict:
    (flags, rate, ack_ctl, wcid, len_ctl, iv, eiv,
     aid, txstream, ctl2, pktid) = struct.unpack("<HH BB H II BBBB", txwi)
    return {
        "flags": flags, "rate": rate, "ack_ctl": ack_ctl, "wcid": wcid,
        "len_ctl": len_ctl, "iv": iv, "eiv": eiv, "aid": aid,
        "txstream": txstream, "ctl2": ctl2, "pktid": pktid,
    }


def test_txwi_is_20_bytes():
    assert len(tx.build_txwi(26)) == 20


def test_txwi_matches_wire_inject_defaults():
    """Default (broadcast, no-ack) inject frame == frame 32207's txwi."""
    d = _decode(tx.build_txwi(26))
    assert d["rate"] == 0x0000      # CCK 1 Mbps
    assert d["wcid"] == 0xFF        # no-station / inject
    assert d["txstream"] == 0x13    # 2x2 MIMO, rev >= E4
    assert d["pktid"] == 0x00       # MT_PACKET_ID_NO_ACK
    assert d["len_ctl"] == 26


def test_txwi_pktid_stays_zero_even_when_ack_requested():
    """Inject is always wcid=0xff, so the chip assigns MT_PACKET_ID_NO_ACK
    regardless of the ACK request — pktid must stay 0 (matches the wire)."""
    d = _decode(tx.build_txwi(26, ack=True))
    assert d["pktid"] == 0x00
    # ack still flips the ack_ctl REQ bit.
    assert d["ack_ctl"] == tx._TXWI_ACK_CTL_REQ


def test_rate_for_channel_band_split():
    """2.4 GHz → CCK 1 Mbps; 5 GHz (ch >= 36) → OFDM 6 Mbps. CCK is invalid on
    5 GHz, so the band split is mandatory, not cosmetic."""
    assert tx._txwi_rate_for_channel(1) == 0x0000
    assert tx._txwi_rate_for_channel(11) == 0x0000
    assert tx._txwi_rate_for_channel(36) == 0x2000
    assert tx._txwi_rate_for_channel(149) == 0x2000


def test_assembled_txwi_rate_follows_band():
    """The rate threads all the way into the assembled bulk-OUT TXWI: CCK by
    default (2.4 GHz wire match) and OFDM 6 Mbps when a 5 GHz rate is passed."""
    frame = b"\xc0\x00" + b"\x00" * 24      # 26-byte deauth, 24B hdr (no pad)
    rate_24 = _decode(tx.assemble_tx_frame(frame)[4:24])["rate"]
    rate_5 = _decode(
        tx.assemble_tx_frame(frame, rate=tx._txwi_rate_for_channel(36))[4:24]
    )["rate"]
    assert rate_24 == 0x0000
    assert rate_5 == 0x2000
