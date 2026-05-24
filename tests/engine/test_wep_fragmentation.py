"""Offline tests for the WEP fragmentation daemon's oracle + seed/handoff.

The send loop needs hardware, but the parts that must be CORRECT are pure: the
oracle that recognizes the AP's relay (pinned from the real probe pcap), seed
selection, and the immediate handoff. Those we test with crafted frames + fakes.
"""
from __future__ import annotations

from types import SimpleNamespace

from wifit3.engine.attacks.wep.fragmentation import WepFragmentation
from wifit3.engine.attacks.wep.wep_crypto import icv, wep_encrypt


OUR = bytes.fromhex("02e34bbe8366")
BSSID = "aa:bb:cc:dd:ee:06"
BSSID_B = bytes.fromhex("aa:bb:cc:dd:ee:06")
SEED_IV = bytes([0xf8, 0x48, 0xbd])


def _frame(fc1: int, da: bytes, a2: bytes, a3: bytes, iv: bytes,
           fc0: int = 0x08, body_extra: bytes = b"\x00" * 40) -> bytes:
    """Craft a 3-address frame: FC + dur + a1 + a2 + a3 + seq + IV+KeyID+body."""
    return (bytes([fc0, fc1]) + b"\x00\x00" + da + a2 + a3 + b"\x00\x00"
            + iv + b"\x00" + body_extra)


def _daemon(**kw):
    calls = []
    iface = SimpleNamespace(
        register_rx_callback=lambda cb: None,
        unregister_rx_callback=lambda cb: None,
        supports_sw_seq=True,
    )
    d = WepFragmentation(
        iface, SimpleNamespace(bssid=BSSID), store=SimpleNamespace(),
        source_mac=OUR, on_forged_arp=lambda f: calls.append(f), **kw,
    )
    d._active = True
    d._seed_iv = SEED_IV
    return d, calls


def _relay():
    """The pinned signature: Data + FromDS + Protected + DA=bcast + SA=us +
    fresh IV (≠ seed)."""
    return _frame(0x42, b"\xff" * 6, BSSID_B, OUR, bytes([0xb2, 0x4c, 0xbd]))


# ---- oracle: accepts only the real relay -----------------------------------

def test_oracle_fires_on_pinned_relay():
    d, _ = _daemon()
    d._rx_cb(_relay(), -40, 0.0)
    assert d._relay_seen
    assert d._relay_frame == _relay()


def test_oracle_ignores_our_own_tods_injection():
    d, _ = _daemon()
    # Our fragments go out ToDS (0x41) sourced from us — must NOT self-trigger.
    d._rx_cb(_frame(0x41, b"\xff" * 6, BSSID_B, OUR, bytes([1, 2, 3])), -40, 0.0)
    assert not d._relay_seen


def test_oracle_ignores_other_stations_relay():
    d, _ = _daemon()
    other = bytes.fromhex("82843d915514")
    d._rx_cb(_frame(0x42, b"\xff" * 6, BSSID_B, other, bytes([9, 9, 9])), -40, 0.0)
    assert not d._relay_seen


def test_oracle_ignores_unicast_da():
    d, _ = _daemon()
    d._rx_cb(_frame(0x42, OUR, BSSID_B, OUR, bytes([7, 7, 7])), -40, 0.0)
    assert not d._relay_seen


def test_oracle_ignores_unprotected_and_mgmt():
    d, _ = _daemon()
    d._rx_cb(_frame(0x02, b"\xff" * 6, BSSID_B, OUR, bytes([7, 7, 7])), -40, 0.0)
    assert not d._relay_seen          # FromDS but not Protected
    d._rx_cb(_frame(0x42, b"\xff" * 6, BSSID_B, OUR, bytes([7, 7, 7]),
                    fc0=0x80), -40, 0.0)
    assert not d._relay_seen          # beacon-ish (type=mgmt)


def test_oracle_ignores_stale_seed_iv():
    """A rebroadcast still carrying our SEED's IV isn't proof of reassembly."""
    d, _ = _daemon()
    d._rx_cb(_frame(0x42, b"\xff" * 6, BSSID_B, OUR, SEED_IV), -40, 0.0)
    assert not d._relay_seen


def test_oracle_matches_sibling_bssid():
    """The box relays onto its sibling BSS too — match on SA, not BSSID."""
    d, _ = _daemon()
    sibling = bytes.fromhex("4c60de4114f9")
    d._rx_cb(_frame(0x42, b"\xff" * 6, sibling, OUR, bytes([0x42, 0xff, 0xaf])),
             -40, 0.0)
    assert d._relay_seen


# ---- seed selection + handoff ----------------------------------------------

def test_pick_seed_builds_fragments_from_a_broadcast_arp():
    d, _ = _daemon()
    d._seed_iv = None
    # A plausible captured broadcast WEP ARP: 24B hdr + IV+KeyID + 40B body.
    arp = _frame(0x42, b"\xff" * 6, BSSID_B, bytes.fromhex("001122334455"),
                 bytes([0xAA, 0xBB, 0xCC]))
    d.store = SimpleNamespace(arp_candidates=lambda b: [arp])
    assert d._pick_seed() is True
    assert d._seed_iv == bytes([0xAA, 0xBB, 0xCC])
    assert 1 <= len(d._frags) <= 16
    # Fragments are ToDS+Protected and carry the seed IV.
    assert d._frags[0][1] & 0x41 == 0x41
    assert d._frags[0][24:27] == bytes([0xAA, 0xBB, 0xCC])


def test_success_hands_relay_to_campaign_and_stops():
    d, calls = _daemon()
    d._relay_seen = True
    d._relay_frame = _relay()
    d._succeed()
    assert calls == [_relay()]        # handed off
    assert d.is_active is False        # stopped injecting (immediate handoff)
    assert d.state == "success"


def test_reassembled_relay_decrypts_to_an_arp_under_known_keystream():
    """Sanity that our crafted 'relay' shape is a real WEP body: encrypt an ARP
    under a keystream, and the daemon's oracle accepts the framed result."""
    ks = bytes((i * 7 + 1) & 0xFF for i in range(60))
    arp_pt = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06]) + bytes(28)
    body = wep_encrypt(ks, arp_pt)
    # The trailing 4 bytes are a valid ICV over the 36-byte ARP plaintext.
    dec = bytes(b ^ k for b, k in zip(body, ks))
    assert dec[-4:] == icv(dec[:-4])
    frame = (b"\x08\x42\x00\x00" + b"\xff" * 6 + BSSID_B + OUR + b"\x00\x00"
             + bytes([0x11, 0x22, 0x33]) + b"\x00" + body)
    d, _ = _daemon()
    d._rx_cb(frame, -40, 0.0)
    assert d._relay_seen
    assert len(frame) == 24 + 4 + len(body)
