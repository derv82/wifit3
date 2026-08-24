"""The session-wide 802.11 picture (``WlanSink``): the AP/client registry, WEP capture, packet
stats and forged-MAC bookkeeping, built from parsed RX frames that a ``WlanArray`` has already
deduplicated across cards. One sink per session, fed by every card; it holds no hardware or
event-loop state, so it is unit-testable on ``Packet`` inputs alone.

Moved here (largely verbatim) from ``WlanInterface``, which becomes a pure per-card radio. The one
addition for multicard is ``card_id``: RSSI is tracked per receiving card in ``signal_by_card`` so
the Power reading can pick the strongest antenna, while every other field (beacons, IEs, clients,
handshakes) is updated once, on the deduplicated (novel) copy only."""
import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional, Set

from wifit3.chips.log_trace import TRACE   # registers Logger.trace + the level name
from wifit3.models import AccessPoint, Client, Handshake, HandshakeMessage
from wifit3.dot11.mac import mac_to_str
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.packet import (
    Packet, BeaconPacket, EapolPacket, WepDataPacket, AssocRequestPacket,
)
from wifit3.wlan.packet_stats import PacketStats
from wifit3.wlan.wep_store import WepCaptureStore

logger = logging.getLogger(__name__)


def _set_result(fut, value) -> None:
    if not fut.done():
        fut.set_result(value)


def _enc_rank(label: str) -> int:
    """Rank an encryption label by evidence strength: OPEN(0) < WEP(1) < WPA*(2)."""
    if label == "OPEN":
        return 0
    if label == "WEP":
        return 1
    return 2  # any IE-derived label


def _bssid_bit_diff(a: str, b: str) -> int:
    """Hamming distance between two ``aa:bb:…``-formatted BSSIDs."""
    pa = a.lower().split(":")
    pb = b.lower().split(":")
    if len(pa) != 6 or len(pb) != 6:
        return 48
    try:
        return sum(
            bin(int(x, 16) ^ int(y, 16)).count("1") for x, y in zip(pa, pb)
        )
    except ValueError:
        return 48


def _bssid_byte_diff(a: str, b: str) -> int:
    """Count differing bytes between two ``aa:bb:…``-formatted BSSIDs."""
    pa = a.lower().split(":")
    pb = b.lower().split(":")
    if len(pa) != 6 or len(pb) != 6:
        return 6
    return sum(1 for x, y in zip(pa, pb) if x != y)


def _fmt_frame(tag: str, ftype: str, src, dest, bssid) -> str:
    """One consistent line for a captured 802.11 frame."""
    return f"[{tag}] {ftype:<9} {src} → {dest}  (bssid {bssid})"


class WlanSink:
    """The deduplicated 802.11 picture across all cards in a session."""

    SIBLING_BIT_DIFF_MAX = 4

    def __init__(self):
        self.access_points: Dict[str, AccessPoint] = {}
        self.clients: Dict[str, Client] = {}
        self.wep_store = WepCaptureStore()  # WEP IV tallying
        self.packet_stats = PacketStats()   # Packet dashboard source
        self.own_macs: Set[str] = set()     # MACs we transmit as; dropped at ingest, never a client
        self._waiters: list = []            # (match, future, loop) for the next_frame await-API
        self._waiters_lock = threading.Lock()

    # ----- signal (per-card) -------------------------------------------------

    @staticmethod
    def _smooth(prev: Optional[int], rssi: int) -> int:
        """Running two-sample average; the first sample is taken as-is."""
        return rssi if prev is None else (prev + rssi) // 2

    def _record_ap_signal(self, ap: AccessPoint, card_id: str, rssi: int) -> None:
        ap.signal_by_card[card_id] = self._smooth(ap.signal_by_card.get(card_id), rssi)

    def _record_client_signal(self, client: Client, card_id: str, rssi: int) -> None:
        client.signal_by_card[card_id] = self._smooth(client.signal_by_card.get(card_id), rssi)

    # ----- ingest ------------------------------------------------------------

    def update(self, pkt: Packet, card_id: str, channel_hint: int = 1) -> None:
        """Fold one deduplicated (novel) frame into the picture. ``card_id`` names the receiving
        card (for per-card RSSI); ``channel_hint`` is that card's current channel, used only when a
        beacon carries no channel of its own."""
        frame_type = pkt.type
        bssid = pkt.bssid

        if (
            frame_type in ("data", "eapol", "wep_data", "assoc_resp", "reassoc_resp",
                           "deauth", "disassoc")
            or frame_type.startswith("mgmt_")
        ) and logger.isEnabledFor(TRACE):
            logger.trace("%s", _fmt_frame("RXFRAME", frame_type, pkt.source, pkt.dest, bssid))

        if not bssid or bssid == "Unknown" or bssid == "ff:ff:ff:ff:ff:ff":
            return

        self.packet_stats.record_rx(bssid, frame_type)  # Live packet dashboard

        self._on_beacon_frame(pkt, card_id, channel_hint)
        self._on_wepdata_frame(pkt)
        self._track_client(pkt, card_id)
        self._on_eapol_frame(pkt)

    def record_signal(self, card_id: str, bssid: str, rssi: int) -> None:
        """Update per-card RSSI from a cross-card duplicate: the frame's picture was already folded
        in by the card that heard it first, but every card that also heard it contributes its own
        antenna's signal. No-op if the BSSID is not a known AP."""
        ap = self.access_points.get(bssid)
        if ap is not None:
            self._record_ap_signal(ap, card_id, rssi)

    def record_injected_eapol(self, frame: bytes) -> None:
        """Fold EvilTwin's injected M1 into the sink, bypassing the array's own-frame RX filter, so
        a client's M2 pairs with it."""
        pkt = WlanFrameParser.parse_80211_frame(frame, 0)
        if isinstance(pkt, EapolPacket):
            self._on_eapol_frame(pkt)

    # ----- typed handlers ----------------------------------------------------

    def _on_beacon_frame(self, pkt: Packet, card_id: str, channel_hint: int) -> bool:
        """Build/refresh the AP from a beacon or probe response."""
        if not isinstance(pkt, BeaconPacket):
            return False
        frame_type = pkt.type
        bssid = pkt.bssid
        rssi = pkt.rssi
        ssid = pkt.ssid
        channel = pkt.channel if pkt.channel is not None else channel_hint
        enc = pkt.encryption
        akms = pkt.akms
        akm_suites = pkt.akm_suites
        pairwise_cipher = pkt.pairwise_cipher
        wpa3 = pkt.wpa3
        transition_mode = pkt.transition_mode
        pmf_capable = pkt.pmf_capable
        pmf_required = pkt.pmf_required
        beacon_protection = pkt.beacon_protection
        wps = pkt.wps
        wps_locked = pkt.wps_locked
        wps_version = pkt.wps_version
        wps_config_methods = pkt.wps_config_methods
        wps_device_password_id = pkt.wps_device_password_id
        wps_selected_registrar = pkt.wps_selected_registrar

        if bssid not in self.access_points:
            ap = AccessPoint(
                bssid=bssid,
                ssid=ssid if self._is_real_ssid(ssid) else None,
                channel=channel,
                encryption=enc,
                akms=list(akms),
                akm_suites=list(akm_suites),
                pairwise_cipher=pairwise_cipher,
                beacons=1 if frame_type == "beacon" else 0,
                wpa3=wpa3,
                transition_mode=transition_mode,
                pmf_capable=pmf_capable,
                pmf_required=pmf_required,
                beacon_protection=beacon_protection,
                wps=wps,
                wps_locked=wps_locked,
                wps_version=wps_version,
                wps_config_methods=wps_config_methods,
                wps_device_password_id=wps_device_password_id,
                wps_selected_registrar=wps_selected_registrar,
            )
            self.access_points[bssid] = ap
            self._record_ap_signal(ap, card_id, rssi)
            self._recompute_siblings_for(bssid)
            if self._is_real_ssid(ssid):
                logger.trace('[RXFRAME] %-9s New AP on Ch %s: %s ("%s")',
                             "beacon", channel, bssid, ssid)
        else:
            ap = self.access_points[bssid]
            old_channel = ap.channel
            if frame_type == "beacon":
                ap.beacons += 1

            if self._is_real_ssid(ssid):
                self._decloak(ap, ssid, frame_type)

            self._record_ap_signal(ap, card_id, rssi)

            # Sibling links are channel-scoped, so re-evaluate on an actual channel move.
            ap.channel = channel
            if old_channel != channel:
                self._recompute_siblings_for(bssid)
            # Keep the strongest encryption evidence ever seen (see _enc_rank).
            if _enc_rank(enc) >= _enc_rank(ap.encryption):
                ap.encryption = enc
                ap.akms = list(akms)
                ap.akm_suites = list(akm_suites)
                ap.pairwise_cipher = pairwise_cipher
                ap.wpa3 = wpa3
                ap.transition_mode = transition_mode
                ap.pmf_capable = pmf_capable
                ap.pmf_required = pmf_required
                ap.beacon_protection = beacon_protection
            # Only refresh WPS when this frame carried the IE.
            if wps:
                ap.wps = True
                ap.wps_locked = wps_locked
                ap.wps_version = wps_version
                ap.wps_config_methods = wps_config_methods
                ap.wps_device_password_id = wps_device_password_id
                ap.wps_selected_registrar = wps_selected_registrar

        ap = self.access_points[bssid]
        ap.last_seen = time.time()

        # Stash the latest RSNIE
        rsn_ie = pkt.rsn_ie_raw
        if rsn_ie:
            ap.rsn_ie = rsn_ie

        # Stash the latest beacon and back-fill handshakes missing one.
        if frame_type == "beacon":
            raw_beacon = pkt.raw
            if raw_beacon:
                ap.last_beacon_frame = raw_beacon
                for hs in ap.handshakes.values():
                    if not hs.beacon_frame:
                        hs.beacon_frame = raw_beacon
                    if ap.akm_suites and not hs.akm_offered:
                        hs.akm_offered = list(ap.akm_suites)
        return True

    def _on_wepdata_frame(self, pkt: Packet) -> bool:
        """Route a WEP Data frame into the passive capture store."""
        if not isinstance(pkt, WepDataPacket):
            return False
        bssid = pkt.bssid
        ap = self.access_points.get(bssid)
        if ap is not None and (ap.encryption or "").upper() == "WEP":
            stats = self.wep_store.observe(bssid, pkt)
            if stats is not None and ap.wep is None:
                ap.wep = stats
        return True

    def _track_client(self, pkt: Packet, card_id: str) -> bool:
        """Register/refresh the client STA behind a frame (assoc, probed SSIDs, decloak)."""
        frame_type = pkt.type
        if frame_type not in (
            "probe_req", "assoc_req", "reassoc_req", "data", "wep_data", "eapol",
            "deauth", "assoc_resp",
        ):
            return False
        bssid = pkt.bssid
        rssi = pkt.rssi
        client_mac = pkt.client_mac
        if not client_mac or client_mac in self.own_macs:
            return True

        if client_mac not in self.clients:
            self.clients[client_mac] = Client(mac=client_mac)
        client = self.clients[client_mac]
        self._record_client_signal(client, card_id, rssi)
        client.packets += 1

        # The client's chosen AKM, from its (Re)Assoc Request RSN IE.
        if isinstance(pkt, AssocRequestPacket) and pkt.assoc_akm is not None:
            client.akm_selected = pkt.assoc_akm

        if frame_type in ("assoc_req", "reassoc_req", "data", "wep_data", "eapol") and bssid:
            client.bssid = bssid

        if frame_type == "probe_req" and self._is_real_ssid(pkt.ssid):
            client.probed_ssids.add(pkt.ssid)

        if frame_type in ("assoc_req", "reassoc_req"):
            ap = self.access_points.get(bssid)
            if ap is not None and self._is_real_ssid(pkt.ssid):
                self._decloak(ap, pkt.ssid, frame_type)
        return True

    def _on_eapol_frame(self, pkt: Packet) -> bool:
        """Track the 4-way handshake / PMKID for a client on a known AP."""
        if not isinstance(pkt, EapolPacket):
            return False
        bssid = pkt.bssid
        ap = self.access_points.get(bssid)
        if ap is None:
            return True
        client_mac = pkt.client_mac
        raw_frame = pkt.raw
        replay = pkt.replay_counter
        if not (client_mac and raw_frame and replay):
            return True

        hs = ap.handshakes.get(client_mac)
        if hs is None:
            hs = Handshake(
                bssid=bssid,
                client_mac=client_mac,
                beacon_frame=ap.last_beacon_frame,
                akm_offered=list(ap.akm_suites),
            )
            ap.handshakes[client_mac] = hs
        elif ap.akm_suites:
            # Refresh in case the handshake was created before the AP's RSN IE was known.
            hs.akm_offered = list(ap.akm_suites)

        # AKM this association negotiated, snapshotted now: this frame's own RSN IE
        # (M2/M3), else the client's assoc-time selection.
        akm = pkt.akm
        if akm is None:
            client_obj = self.clients.get(client_mac)
            if client_obj is not None:
                akm = client_obj.akm_selected

        # Forged MACs keep a Handshake (for PMKID) but skip the EAPOL list.
        if client_mac not in self.own_macs and not hs.has_message(raw_frame):
            eapol = HandshakeMessage(
                raw=raw_frame,
                msg_num=pkt.msg_num,
                replay_hex=replay.hex(),
                nonce=pkt.nonce or b"",
                mic=pkt.mic or b"",
                key_data_len=pkt.key_data_len,
                eapol_payload=pkt.payload,
                akm=akm,
                timestamp=time.time(),
            )
            hs.messages.append(eapol)
            msg_label = f"M{eapol.msg_num}" if eapol.msg_num else "EAPOL-?"
            logger.info(f"[{msg_label}] {bssid} <-> {client_mac} (replay {eapol.replay_hex})")

        pmkid = pkt.pmkid
        if pmkid and not hs.pmkid:
            hs.pmkid = pmkid
            hs.pmkid_akm = akm
            logger.info(f"[PMKID] {bssid} <-> {client_mac} captured {pmkid.hex()}")
        return True

    def _decloak(self, ap: AccessPoint, ssid: str, method: str) -> None:
        """Learn a hidden AP's real SSID, tag how it was revealed."""
        if not self._is_real_ssid(ap.ssid):
            ap.decloak_method = method
        ap.ssid = ssid

    @staticmethod
    def _is_real_ssid(ssid: Optional[str]) -> bool:
        """True for a usable SSID (not hidden)."""
        return bool(ssid) and ssid != "<hidden>"

    def _recompute_siblings_for(self, bssid: str) -> None:
        """Refresh sibling links for ``bssid`` against the whole registry."""
        ap = self.access_points.get(bssid)
        if not ap:
            return
        new_siblings: List[str] = []
        for other_bssid, other_ap in self.access_points.items():
            if other_bssid == bssid:
                continue
            same_channel = other_ap.channel == ap.channel
            bit_d = _bssid_bit_diff(bssid, other_bssid)
            byte_d = _bssid_byte_diff(bssid, other_bssid)
            is_sibling = (
                same_channel
                and bit_d > 0
                and (bit_d <= self.SIBLING_BIT_DIFF_MAX or byte_d == 1)
            )
            if is_sibling:
                new_siblings.append(other_bssid)
                if bssid not in other_ap.siblings:
                    other_ap.siblings.append(bssid)
            else:
                # Channel mismatch or too divergent: drop any stale link.
                if bssid in other_ap.siblings:
                    other_ap.siblings.remove(bssid)
        ap.siblings = new_siblings

    # ----- reads / forged-MAC bookkeeping / TX stats -------------------------

    def get_access_points(self) -> List[AccessPoint]:
        """A list of discovered Access Points."""
        return list(self.access_points.values())

    def dispatch_rx(self, pkt) -> None:
        """Resolve any next_frame waiters this deduped RX frame matches."""
        with self._waiters_lock:
            waiters = list(self._waiters)
        for match, fut, loop in waiters:
            if fut.done():
                continue
            try:
                hit = match(pkt)
            except Exception:
                hit = False
            if hit:
                loop.call_soon_threadsafe(_set_result, fut, pkt)

    async def next_frame(self, match, timeout: float):
        """Await the first deduped RX frame for which ``match(pkt)`` is true, else None on timeout."""
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        entry = (match, fut, loop)
        with self._waiters_lock:
            self._waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            with self._waiters_lock:
                if entry in self._waiters:
                    self._waiters.remove(entry)

    async def wait_until(self, condition, timeout: float, poll: float = 0.05) -> bool:
        """Poll ``condition()`` until it is true or ``timeout`` elapses; returns the final truth."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            await asyncio.sleep(poll)
        return bool(condition())

    def register_own_mac(self, mac) -> str:
        """Mark ``mac`` as one we transmit as: ingest drops its frames, and it never becomes a client."""
        mac_str = mac_to_str(mac) if isinstance(mac, (bytes, bytearray)) else str(mac).lower()
        self.own_macs.add(mac_str)
        self.clients.pop(mac_str, None)
        return mac_str

    def unregister_own_mac(self, mac) -> None:
        """Inverse of ``register_own_mac``."""
        mac_str = mac_to_str(mac) if isinstance(mac, (bytes, bytearray)) else str(mac).lower()
        self.own_macs.discard(mac_str)

    # Back-compat names the campaigns still call; all funnel to the single own-MAC set.
    def register_forged_mac(self, mac) -> None:
        self.register_own_mac(mac)

    def register_self_mac(self, mac, bssid=None) -> str:
        return self.register_own_mac(mac)

    def unregister_self_mac(self, mac) -> None:
        self.unregister_own_mac(mac)

    @property
    def forged_macs(self):
        """Back-compat alias the UI reads to hide our own STA from client lists."""
        return self.own_macs

    def record_tx(self, frame_bytes: bytes) -> None:
        """Classify an outgoing frame for the packet dashboard (deauth vs other). Best-effort."""
        try:
            parsed = WlanFrameParser.parse_80211_frame(frame_bytes, 0)
            if parsed is None:
                return
            if logger.isEnabledFor(TRACE):
                logger.trace("%s", _fmt_frame("TXFRAME", parsed.type,
                                              parsed.source, parsed.dest, parsed.bssid))
            bssid = parsed.bssid
            if bssid and bssid not in ("Unknown", "ff:ff:ff:ff:ff:ff"):
                self.packet_stats.record_tx(bssid, parsed.type == "deauth")
        except Exception:
            pass
