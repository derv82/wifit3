"""The device-agnostic 802.11 interface (``WlanInterface``): builds the AP/client registry
from parsed RX frames, drives channel hopping, and injects raw frames via a chipset driver."""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Callable, Any, Dict, Set

from wifit3.models import AccessPoint, Client, Handshake, HandshakeMessage
from wifit3.chips.driver import FakeMacSupport
from wifit3.errors import is_device_gone
from wifit3.wlan.channels import scan_hop_order
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.packet import (
    Packet, BeaconPacket, EapolPacket, WepDataPacket, AssocRequestPacket,
)
from wifit3.dot11.deauth import build_deauth, _deauth_nav_bytes
from wifit3.wlan.packet_stats import PacketStats
from wifit3.wlan.wep_store import WepCaptureStore

logger = logging.getLogger(__name__)


def _enc_rank(label: str) -> int:
    """Rank an encryption label by evidence strength: OPEN(0) < WEP(1) < WPA*(2).

    A WPA/WPA2/WPA3/OWE label means an RSN/WPA IE was parsed (a truncated beacon can't
    fabricate one); WEP/OPEN are Privacy-bit fallbacks when no IE was found. Callers keep
    the highest rank ever seen so a dropped IE can't downgrade an AP.
    """
    if label == "OPEN":
        return 0
    if label == "WEP":
        return 1
    return 2  # any IE-derived label


def _bssid_bit_diff(a: str, b: str) -> int:
    """Hamming distance between two ``aa:bb:…``-formatted BSSIDs (count of
    differing bits across all 48). Returns 48 (sentinel — fully different)
    for malformed input so callers naturally reject it from the sibling
    set."""
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
    """Count differing bytes between two ``aa:bb:…``-formatted BSSIDs.
    Complements ``_bssid_bit_diff``: vendor schemes that deliberately
    randomize multiple bits inside a single byte (e.g. ``42 / 3c`` =
    6-bits-in-1-byte) need the byte-level branch."""
    pa = a.lower().split(":")
    pb = b.lower().split(":")
    if len(pa) != 6 or len(pb) != 6:
        return 6
    return sum(1 for x, y in zip(pa, pb) if x != y)


@dataclass
class DeauthResult:
    """Per-direction TX-ACK tally for a client-directed de-auth (see ``deauth_client``).

    ``client_acks``/``client_sent`` — AP→Client frames the CLIENT ACKed (each ACK's RA is
    the AP MAC we spoofed as the sender). ``ap_acks``/``ap_sent`` — Client→AP frames the AP
    ACKed (RA = the client MAC we spoofed). ``measured`` is False when the driver has no
    TX-ACK detection: the counts are then 0 and the caller reports only frames sent, no ACK
    verdict (a broadcast de-auth is never measured — group frames aren't ACKed)."""
    client_acks: int = 0
    client_sent: int = 0
    ap_acks: int = 0
    ap_sent: int = 0
    measured: bool = False

    @property
    def total_acked(self) -> int:
        return self.client_acks + self.ap_acks

    @property
    def total_sent(self) -> int:
        return self.client_sent + self.ap_sent


def _fmt_frame(tag: str, ftype: str, src, dest, bssid) -> str:
    """One consistent line for a captured/injected 802.11 frame:
    ``[TAG] type      src → dst  (bssid …)``. Shared by [RXFRAME]/[TXFRAME] (and the
    new-AP beacon line) so every frame log reads in the same padded-type column."""
    return f"[{tag}] {ftype:<9} {src} → {dest}  (bssid {bssid})"


class WlanInterface:
    """High-level 802.11 abstraction for a hardware driver; the UI talks only to this class."""
    def __init__(self, driver_instance: Any, name: str, description: str,
                 vid: Optional[int] = None, pid: Optional[int] = None,
                 dev: Any = None):
        self.driver = driver_instance
        self.name = name
        self.description = description
        # VID:PID + raw pyusb Device, kept so the splash can probe openability and re-find
        # the card after a WinUSB install. Optional so test-built interfaces can omit them.
        self.vid = vid
        self.pid = pid
        self.dev = dev
        self.current_channel = 1

        self.access_points: Dict[str, AccessPoint] = {}
        self.clients: Dict[str, Client] = {}

        # Passive WEP IV tallying (RX-only); updates AccessPoint.wep counters.
        self.wep_store = WepCaptureStore()

        # Per-(bssid, class) frame counters for the Focus live packet dashboard.
        self.packet_stats = PacketStats()

        # MACs we forged for active attacks (e.g. PMKID harvest). Frames to these come from
        # the AP, so skip client registration + handshake EAPOL retries (PMKID still runs).
        self.forged_macs: Set[str] = set()

        # Forged STA MAC for WEP fake-auth. Unlike forged_macs these ARE registered as a
        # client, tagged is_self so the UI shows "YOU".
        self.self_macs: Set[str] = set()

        self._rx_callbacks: List[Callable[[bytes, int, float], None]] = []
        # Fired once when the adapter is lost (RX reader death, or a hopper tune that hits a
        # dead pipe). The app subscribes and raises the Quit-only fatal modal. Latched via
        # _device_lost so multiple error sources can't stack it.
        self._disconnect_callbacks: List[Callable[[Exception], None]] = []
        self._device_lost = False
        self._hopping_task: Optional[asyncio.Task] = None
        # The in-flight per-hop set_channel, tracked so stop_hopping() can drain it
        # (a tune runs in an executor thread that cancellation can't stop).
        self._tune_task: Optional[asyncio.Task] = None
        self._is_hopping = False
        # Serializes start/stop_hopping so they can't interleave — a concurrent start
        # mid-stop would orphan a hop task that ping-pongs the chip across channels.
        self._hop_lock = asyncio.Lock()

        self.driver.register_rx_callback(self._on_frame_parsed)
        # Drivers whose RX reader can report a terminal failure forward it here.
        self.driver.register_disconnect_callback(self._on_device_lost)

    def _on_frame_parsed(self, pkt: Packet):
        """Mutator callback: takes the driver's parsed frame and updates the registry."""
        frame_type = pkt.type
        bssid = pkt.bssid
        rssi = pkt.rssi

        # Fan out to raw-frame subscribers (e.g. WPA3DowngradeAttack) first, so they see
        # frames even when the state-update path below bails on bssid filtering.
        if pkt.raw is not None and self._rx_callbacks:
            self._fire_rx_callbacks(pkt.raw, rssi)

        # Debug trace of attack-relevant RX — data/EAPOL plus the AP's mgmt replies
        # (assoc-resp, auth=mgmt_11, deauth), so a whole exchange is visible. Data frames
        # stay in on purpose: seeing data addressed to MACs that aren't ours is the sign
        # monitor mode is actually promiscuous. Beacons log their own [RXFRAME] line on
        # first sighting (see _on_beacon_frame); control frames never reach here.
        if (
            frame_type in ("data", "eapol", "wep_data", "assoc_resp", "reassoc_resp",
                           "deauth", "disassoc")
            or frame_type.startswith("mgmt_")
        ) and logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s", _fmt_frame("RXFRAME", frame_type, pkt.source, pkt.dest, bssid))

        if not bssid or bssid == "Unknown" or bssid == "ff:ff:ff:ff:ff:ff":
            return

        # Live packet dashboard — tally each RX frame against its AP by class. Best-effort.
        self.packet_stats.record_rx(bssid, frame_type)

        # Each handler narrows on frame type and returns True if it claimed the frame —
        # a home for future frame types / fields. They are NOT exclusive: an eapol frame
        # updates the client registry AND the handshake.
        self._on_beacon_frame(pkt)
        self._on_wepdata_frame(pkt)
        self._track_client(pkt)
        self._on_eapol_frame(pkt)

    def _on_beacon_frame(self, pkt: Packet) -> bool:
        """Build/refresh the AP from a beacon or probe response, then (for beacons) stash the
        raw frame and back-fill handshakes that predate it. True if this was a beacon/probe."""
        if not isinstance(pkt, BeaconPacket):
            return False
        frame_type = pkt.type
        bssid = pkt.bssid
        rssi = pkt.rssi
        ssid = pkt.ssid
        channel = pkt.channel if pkt.channel is not None else self.current_channel
        enc = pkt.encryption
        akms = pkt.akms
        akm_suites = pkt.akm_suites
        pairwise_cipher = pkt.pairwise_cipher
        wpa3 = pkt.wpa3
        transition_mode = pkt.transition_mode
        pmf_capable = pkt.pmf_capable
        pmf_required = pkt.pmf_required
        wps = pkt.wps
        wps_locked = pkt.wps_locked
        wps_version = pkt.wps_version
        wps_config_methods = pkt.wps_config_methods
        wps_device_password_id = pkt.wps_device_password_id
        wps_selected_registrar = pkt.wps_selected_registrar

        if bssid not in self.access_points:
            self.access_points[bssid] = AccessPoint(
                bssid=bssid,
                ssid=ssid if self._is_real_ssid(ssid) else None,
                channel=channel,
                signal=rssi,
                encryption=enc,
                akms=list(akms),
                akm_suites=list(akm_suites),
                pairwise_cipher=pairwise_cipher,
                beacons=1 if frame_type == "beacon" else 0,
                wpa3=wpa3,
                transition_mode=transition_mode,
                pmf_capable=pmf_capable,
                pmf_required=pmf_required,
                wps=wps,
                wps_locked=wps_locked,
                wps_version=wps_version,
                wps_config_methods=wps_config_methods,
                wps_device_password_id=wps_device_password_id,
                wps_selected_registrar=wps_selected_registrar,
            )
            self._recompute_siblings_for(bssid)
            if self._is_real_ssid(ssid):
                logger.info('[RXFRAME] %-9s New AP on Ch %s: %s ("%s")',
                            "beacon", channel, bssid, ssid)
        else:
            ap = self.access_points[bssid]
            old_channel = ap.channel
            if frame_type == "beacon":
                ap.beacons += 1

            if self._is_real_ssid(ssid):
                self._decloak(ap, ssid, frame_type)

            # Smooth RSSI (running average)
            ap.signal = (ap.signal + rssi) // 2

            # Sibling links are channel-scoped, so re-evaluate on an actual channel move.
            ap.channel = channel
            if old_channel != channel:
                self._recompute_siblings_for(bssid)
            # Keep the strongest encryption evidence ever seen (see _enc_rank): apply only on
            # equal-or-higher rank, so a dropped IE can't downgrade WPA2->WEP while WPA*-to-WPA*
            # config refreshes still pass.
            if _enc_rank(enc) >= _enc_rank(ap.encryption):
                ap.encryption = enc
                ap.akms = list(akms)
                ap.akm_suites = list(akm_suites)
                ap.pairwise_cipher = pairwise_cipher
                ap.wpa3 = wpa3
                ap.transition_mode = transition_mode
                ap.pmf_capable = pmf_capable
                ap.pmf_required = pmf_required
            # Only refresh WPS when this frame carried the IE — its absence must not clear a
            # known WPS flag (but locked/state can change, so re-read if present).
            if wps:
                ap.wps = True
                ap.wps_locked = wps_locked
                ap.wps_version = wps_version
                ap.wps_config_methods = wps_config_methods
                ap.wps_device_password_id = wps_device_password_id
                ap.wps_selected_registrar = wps_selected_registrar

        ap = self.access_points[bssid]
        # Bump recency — drives stale-row dim-out and "Last Beacon: Ns ago" in Focus.
        ap.last_seen = time.time()

        # Stash the latest RSN IE — PMKID harvest echoes it into its Assoc Req.
        rsn_ie = pkt.rsn_ie_raw
        if rsn_ie:
            ap.rsn_ie = rsn_ie

        # Stash the latest beacon and back-fill handshakes missing one (EAPOL can arrive
        # before the first beacon).
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
        """Route a WEP Data frame into the passive capture store — only for APs already
        classified WEP from a beacon (guards a stray ExtIV-clear frame). True for wep_data."""
        if not isinstance(pkt, WepDataPacket):
            return False
        bssid = pkt.bssid
        ap = self.access_points.get(bssid)
        if ap is not None and (ap.encryption or "").upper() == "WEP":
            stats = self.wep_store.observe(bssid, pkt)
            if stats is not None and ap.wep is None:
                ap.wep = stats
        return True

    def _track_client(self, pkt: Packet) -> bool:
        """Register/refresh the client STA behind a frame (association, probed SSIDs, decloak
        via Assoc Req). True for a client-bearing frame type."""
        frame_type = pkt.type
        if frame_type not in (
            "probe_req", "assoc_req", "reassoc_req", "data", "wep_data", "eapol",
            "deauth", "assoc_resp",
        ):
            return False
        bssid = pkt.bssid
        rssi = pkt.rssi
        client_mac = pkt.client_mac
        if not client_mac or client_mac in self.forged_macs:
            return True

        if client_mac not in self.clients:
            self.clients[client_mac] = Client(
                mac=client_mac, signal=rssi, is_self=client_mac in self.self_macs,
            )
        client = self.clients[client_mac]
        client.signal = (client.signal + rssi) // 2
        client.packets += 1

        # The client's chosen AKM, from its (Re)Assoc Request RSN IE.
        if isinstance(pkt, AssocRequestPacket) and pkt.assoc_akm is not None:
            client.akm_selected = pkt.assoc_akm

        if frame_type in ("assoc_req", "reassoc_req", "data", "wep_data", "eapol") and bssid:
            client.bssid = bssid

        if frame_type == "probe_req" and self._is_real_ssid(pkt.ssid):
            client.probed_ssids.add(pkt.ssid)

        if frame_type == "assoc_req":
            ap = self.access_points.get(bssid)
            if ap is not None and self._is_real_ssid(pkt.ssid):
                self._decloak(ap, pkt.ssid, "assoc_req")
        return True

    def _on_eapol_frame(self, pkt: Packet) -> bool:
        """Track the 4-way handshake / PMKID for a client on a known AP — per-client, never
        wiped. True for an eapol frame (even when the AP is unknown)."""
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
            # Refresh in case the handshake was created before the AP's RSN IE was known
            # (EAPOL can precede the first beacon).
            hs.akm_offered = list(ap.akm_suites)

        # Forged MACs keep a Handshake (for PMKID) but skip the EAPOL list — those are just AP
        # retries of M1 we never answer, and would show a false "Partial".
        if client_mac not in self.forged_macs and not hs.has_message(raw_frame):
            eapol = HandshakeMessage(
                raw=raw_frame,
                msg_num=pkt.msg_num,
                replay_hex=replay.hex(),
                nonce=pkt.nonce or b"",
                mic=pkt.mic or b"",
                key_data_len=pkt.key_data_len,
                eapol_payload=pkt.payload,
                timestamp=time.time(),
            )
            hs.messages.append(eapol)
            msg_label = f"M{eapol.msg_num}" if eapol.msg_num else "EAPOL-?"
            logger.info(f"[{msg_label}] {bssid} <-> {client_mac} (replay {eapol.replay_hex})")

        # Client's negotiated AKM, read from M2's cleartext RSN IE — the authoritative
        # per-association crackability signal (SAE vs PSK) on a WPA2/WPA3 transition AP.
        # Latest M2 wins.
        akm = pkt.akm
        if akm is not None:
            hs.akm_client = akm
        elif hs.akm_client is None:
            # No M2 yet (e.g. a PMKID-only capture) — fall back to the AKM the client
            # advertised in its (Re)Assoc Request.
            client_obj = self.clients.get(client_mac)
            if client_obj is not None and client_obj.akm_selected is not None:
                hs.akm_client = client_obj.akm_selected

        # Passive PMKID capture: AP's M1 sometimes carries a PMKID KDE. First wins.
        pmkid = pkt.pmkid
        if pmkid and not hs.pmkid:
            hs.pmkid = pmkid
            logger.info(f"[PMKID] {bssid} <-> {client_mac} captured {pmkid.hex()}")
        return True

    def _decloak(self, ap: AccessPoint, ssid: str, method: str) -> None:
        """Learn a hidden AP's real SSID: tag how it was revealed on the first reveal, then
        always refresh the SSID (renames are rare but real; a bad-probe flood is a future
        concern, fixable then by making this first-write-only)."""
        if not self._is_real_ssid(ap.ssid):
            ap.decloak_method = method
        ap.ssid = ssid

    @staticmethod
    def _is_real_ssid(ssid: Optional[str]) -> bool:
        """True for a usable SSID — present and not the hidden-network sentinel."""
        return bool(ssid) and ssid != "<hidden>"

    SIBLING_BIT_DIFF_MAX = 4

    def _recompute_siblings_for(self, bssid: str) -> None:
        """Refresh sibling links for ``bssid`` against the whole registry.

        Sibling rule: same channel AND (Hamming distance <= 4 bits OR exactly one byte
        differs) — the two branches catch the two vendor multi-BSSID schemes (scattered
        single-bit flips vs. one deliberately-randomized byte). Links are bidirectional;
        called when an AP is added or changes channel.
        """
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
                # Channel mismatch or too divergent — drop any stale link.
                if bssid in other_ap.siblings:
                    other_ap.siblings.remove(bssid)
        ap.siblings = new_siblings

    def get_access_points(self) -> List[AccessPoint]:
        """Returns a list of discovered Access Points."""
        return list(self.access_points.values())

    async def connect(self, progress_cb: Optional[Callable[[float, str], None]] = None) -> bool:
        """Initializes the underlying hardware handshake."""
        return await self.driver.connect(progress_cb=progress_cb)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to ``channel`` via the driver. ``scan=True`` (channel hopper) hints a
        transient hop so the driver may skip per-hop calibration; Focus passes False for a
        full tune."""
        success = await self.driver.set_channel(channel, scan=scan)
        if success:
            self.current_channel = channel
        return success

    def register_forged_mac(self, mac: Any) -> None:
        """Mark ``mac`` as one we forged for an active attack. Accepts bytes
        (6 B) or a colon-separated string. Idempotent."""
        if isinstance(mac, bytes):
            mac_str = ":".join(f"{b:02x}" for b in mac)
        else:
            mac_str = str(mac).lower()
        self.forged_macs.add(mac_str)

    async def set_fake_mac(self, mac: Any, bssid: Any = None) -> Optional[str]:
        """Ask the driver to HW-ACK frames addressed to ``mac`` (active-monitor) — the
        prerequisite for an ACKed conversation (WPS/EAP). ``bssid`` is the peer AP
        (firmware-offload radios only). Also registers the MAC as forged. Accepts bytes
        or a colon-string. Returns the MAC armed, or None if the card can't spoof one
        (FakeMacSupport.NONE / absent); a FIXED_MAC card returns its own MAC."""
        support = self.driver.FAKE_MAC
        if support in (FakeMacSupport.NONE, FakeMacSupport.UNIMPLEMENTED):
            logger.info("set_fake_mac: %s — active-monitor unavailable (%s)",
                        self._chipset, support.value)
            return None
        mac_b = self._to_mac_bytes(mac)
        bssid_b = self._to_mac_bytes(bssid) if bssid is not None else None
        assumed = await self.driver.enter_active_monitor(mac_b, bssid_b)
        self.register_forged_mac(assumed)
        assumed_str = ":".join(f"{b:02x}" for b in assumed)
        logger.info("[FAKEMAC] %s now HW-ACKing %s", self._chipset, assumed_str)
        return assumed_str

    async def clear_fake_mac(self) -> None:
        """Inverse of set_fake_mac: stop HW-ACKing the forged MAC, restore plain
        monitor. Idempotent and safe on drivers without the capability."""
        if self.driver.FAKE_MAC in (FakeMacSupport.SPOOFABLE, FakeMacSupport.FIXED_MAC):
            await self.driver.exit_active_monitor()
            logger.info("[FAKEMAC] %s restored plain monitor", self._chipset)

    @staticmethod
    def _to_mac_bytes(mac: Any) -> bytes:
        """Coerce a MAC given as 6 raw bytes or a colon-separated string to bytes."""
        if isinstance(mac, bytes):
            return mac
        return bytes(int(x, 16) for x in str(mac).split(":"))

    @property
    def _chipset(self) -> str:
        """The chips/<name> dir of the active driver — for driver-specific log lines."""
        parts = type(self.driver).__module__.split(".")
        return parts[-2] if len(parts) >= 2 else parts[-1]

    def active_monitor_warning(self) -> Optional[str]:
        """Treelog warning (rich markup) if this card can't HW-ACK a spoofed MAC, else None.
        WPS still runs — un-ACKed, so expect timeouts/retries. NONE = the silicon can't
        (hard MAC); UNIMPLEMENTED = this driver didn't port active-monitor."""
        support = self.driver.FAKE_MAC
        if support in (FakeMacSupport.SPOOFABLE, FakeMacSupport.FIXED_MAC):
            return None
        reason = "not possible (hard-MAC)" if support is FakeMacSupport.NONE else "not implemented"
        return (f"⚠  [orange1][bold]Active Monitor[/bold] {reason} "
                f"for [bold]{self._chipset}[/bold][/orange1]")

    def register_self_mac(self, mac: Any, bssid: Optional[str] = None) -> str:
        """Mark ``mac`` as our own forged STA and surface it in the client
        table tagged ``is_self`` (rendered "YOU"). Accepts bytes or a string;
        returns the colon-string form. Pre-creates the Client so YOU appears
        the instant fake-auth starts, before any AP reply arrives."""
        if isinstance(mac, bytes):
            mac_str = ":".join(f"{b:02x}" for b in mac)
        else:
            mac_str = str(mac).lower()
        self.self_macs.add(mac_str)
        client = self.clients.get(mac_str)
        if client is None:
            self.clients[mac_str] = Client(
                mac=mac_str, bssid=bssid, is_self=True
            )
        else:
            client.is_self = True
            if bssid:
                client.bssid = bssid
        return mac_str

    def unregister_self_mac(self, mac: Any) -> None:
        """Inverse of register_self_mac — drops the YOU client. Idempotent."""
        if isinstance(mac, bytes):
            mac_str = ":".join(f"{b:02x}" for b in mac)
        else:
            mac_str = str(mac).lower()
        self.self_macs.discard(mac_str)
        self.clients.pop(mac_str, None)

    def register_disconnect_callback(self, callback_func: Callable[[Exception], None]):
        """Register a subscriber for adapter loss: func(exc). Fired once, on the event loop."""
        if callback_func not in self._disconnect_callbacks:
            self._disconnect_callbacks.append(callback_func)

    def _on_device_lost(self, exc: Exception) -> None:
        """Single sink for 'adapter gone', from any source (RX reader death, a hopper tune on
        a dead pipe). Latched: fans to subscribers once, and drops the hop flag so the hopper
        stops poking the dead device. Always invoked on the loop thread."""
        if self._device_lost:
            return
        self._device_lost = True
        self._is_hopping = False
        logger.error(f"[{self._chipset}] adapter lost: {exc}")
        for cb in list(self._disconnect_callbacks):
            try:
                cb(exc)
            except Exception:
                logger.exception("Disconnect callback failed")

    def register_rx_callback(self, callback_func: Callable[[bytes, int, float], None]):
        """Register a raw-frame subscriber: func(frame_bytes, rssi, timestamp)."""
        if callback_func not in self._rx_callbacks:
            self._rx_callbacks.append(callback_func)

    def unregister_rx_callback(self, callback_func: Callable[[bytes, int, float], None]):
        """Idempotent inverse of register_rx_callback."""
        if callback_func in self._rx_callbacks:
            self._rx_callbacks.remove(callback_func)

    def _fire_rx_callbacks(self, frame_bytes: bytes, rssi: int):
        ts = time.time()
        for cb in self._rx_callbacks:
            try:
                cb(frame_bytes, rssi, ts)
            except Exception as e:
                logger.error(f"RX Callback failed: {e}")

    async def send_no_wait(self, frame_bytes: bytes) -> bool:
        """Inject a frame fire-and-forget: no software ACK wait, no software resend. The chip's
        own HW ACK-based retry still applies."""
        self._record_tx(frame_bytes)                 # live packet dashboard (deauth vs other)
        return await self.driver.inject_frame(frame_bytes)

    async def send_until_ack(self, frame_bytes: bytes, max_retries: int = 0) -> bool:
        """Inject a frame, then watch the monitor tap for the recipient's link-ACK, resending up
        to ``max_retries`` times on silence; returns whether it landed. Needs ``enable_rx_acks()``
        armed first, else fire-and-forget. Best-effort (see ``Driver.inject_frame_slow_retry``)."""
        self._record_tx(frame_bytes)
        return await self.driver.inject_frame_slow_retry(frame_bytes, max_resends=max_retries)

    async def enable_rx_acks(self) -> None:
        """Arm the driver's ACK tally so ``send_until_ack`` / ``acks_seen`` can observe the
        recipient's ACKs. A register write or a no-op, depending on the card."""
        await self.driver.enable_rx_acks()

    async def disable_rx_acks(self) -> None:
        """Disarm the ACK tally (inverse of ``enable_rx_acks``)."""
        await self.driver.disable_rx_acks()

    def acks_seen(self, mac: bytes) -> int:
        """ACKs the driver has tallied to source MAC ``mac`` since ``enable_rx_acks``.
        Approximate and chipset-dependent (see ``Driver.acks_seen``)."""
        return self.driver.acks_seen(mac)

    @property
    def supported_channels(self) -> List[int]:
        """Channels the active driver can tune to (delegates to the driver)."""
        return self.driver.SUPPORTED_CHANNELS

    def _record_tx(self, frame_bytes: bytes) -> None:
        """Classify an outgoing frame for the packet dashboard (deauth vs other), reusing
        the RX parser. Wrapped so a malformed inject can't break TX over a cosmetic counter."""
        try:
            parsed = WlanFrameParser.parse_80211_frame(frame_bytes, 0)
            if parsed is None:
                return
            # Mirror of [RXFRAME] for our injects — reappearing as RX would mean chip loopback.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("%s", _fmt_frame("TXFRAME", parsed.type,
                                              parsed.source, parsed.dest, parsed.bssid))
            bssid = parsed.bssid
            if bssid and bssid not in ("Unknown", "ff:ff:ff:ff:ff:ff"):
                self.packet_stats.record_tx(bssid, parsed.type == "deauth")
        except Exception:
            pass

    def _deauth_channel(self, ap_bssid: str) -> int:
        """The AP's known channel (for the log line); the current channel if it's unseen.
        Parity with the historic deauth path — the radio is expected to already be parked
        on the target channel; this doesn't re-tune."""
        if ap_bssid in self.access_points:
            return self.access_points[ap_bssid].channel
        logger.debug("[DEAUTH] %s not in registry; deauthing on current channel %d",
                     ap_bssid, self.current_channel)
        return self.current_channel

    def _deauth_frame(self, dest: bytes, src: bytes, ap_mac: bytes, dest_str: str) -> bytes:
        """One 802.11 deauth MPDU addressed dest←src (BSSID=ap_mac), reason 7 (class-3 frame
        from a nonassociated STA), with the destination-keyed ACK NAV (unicast → ACK NAV,
        group → 0; see _deauth_nav_bytes)."""
        return build_deauth(dest, src, ap_mac, 7, duration=_deauth_nav_bytes(dest_str))

    async def deauth_broadcast(self, ap_bssid: str, count: int = 20) -> int:
        """Spray AP→broadcast de-auth frames — one wave hits every associated station at once.

        Group-addressed (addr1 = ff:ff:ff:ff:ff:ff), so no station ever ACKs it: this is
        fire-and-forget with no possible delivery confirmation — the nature of broadcast.
        One direction only; a reverse broadcast→AP frame de-auths nobody. Returns the count
        of frames sent."""
        ap_bssid = ap_bssid.lower()
        target_chan = self._deauth_channel(ap_bssid)
        ap_mac = self._to_mac_bytes(ap_bssid)
        bcast = b"\xff\xff\xff\xff\xff\xff"
        frame = self._deauth_frame(bcast, ap_mac, ap_mac, "ff:ff:ff:ff:ff:ff")
        logger.info("Injecting broadcast de-auth (%dx) on CH %d from %s", count, target_chan, ap_bssid)
        for _ in range(count):
            await self.send_no_wait(frame)
            await asyncio.sleep(0.01)
        return count

    async def deauth_client(self, ap_bssid: str, client_bssid: str,
                            rounds: int = 10) -> DeauthResult:
        """De-auth one client both ways, tallying how many frames each endpoint ACKed.

        AP->Client frames are ACKed by the CLIENT (the ACK's RA is the AP MAC we spoofed as
        sender); Client->AP frames are ACKed by the AP (RA = the client MAC we spoofed). We arm
        the RX-ACK tally, send each frame with ``send_until_ack``, and count the per-direction
        hits. The count is best-effort: a busy AP's own traffic to the spoofed MACs pollutes it,
        and some cards over-count (see ``Driver.acks_seen``), so treat it as a rough signal, not
        a delivery guarantee."""
        ap_bssid = ap_bssid.lower()
        client_bssid = client_bssid.lower()
        target_chan = self._deauth_channel(ap_bssid)
        ap_mac = self._to_mac_bytes(ap_bssid)
        cl_mac = self._to_mac_bytes(client_bssid)
        client_deauth = self._deauth_frame(cl_mac, ap_mac, ap_mac, client_bssid)   # AP→Client
        ap_deauth = self._deauth_frame(ap_mac, cl_mac, ap_mac, ap_bssid)           # Client→AP
        logger.info("Injecting client de-auth (%dx pairs) on CH %d: %s <-> %s",
                    rounds, target_chan, ap_bssid, client_bssid)

        driver = self.driver
        res = DeauthResult(measured=True)
        await driver.enable_rx_acks()
        try:
            for _ in range(rounds):
                landed_c = await self.send_until_ack(client_deauth)
                res.client_sent += 1
                if landed_c:
                    res.client_acks += 1
                landed_a = await self.send_until_ack(ap_deauth)
                res.ap_sent += 1
                if landed_a:
                    res.ap_acks += 1
                await asyncio.sleep(0.01)
        finally:
            await driver.disable_rx_acks()
        return res


    async def start_hopping(self, channels: List[int] = None, interval: float = 0.5):
        """Spawns an asyncio task to loop through channels.

        If `channels` is omitted, uses the driver's `SUPPORTED_CHANNELS`
        capability. Falls back to the canonical 2.4 GHz set for drivers
        that pre-date the capability declaration.
        """
        async with self._hop_lock:
            if self._is_hopping:
                return

            if not channels:
                channels = self.supported_channels or [1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 5, 10]

            # Hop busy channels (1/6/11) first so the AP list fills before the scanner's
            # first sort tick. SUPPORTED_CHANNELS stays sequential for the filter UI.
            channels = scan_hop_order(channels)

            self._is_hopping = True
            self._hopping_task = asyncio.create_task(self._hop_loop(channels, interval))
            logger.info(
                "Started channel hopping on %s across %d channel(s) every %.2fs",
                self._chipset, len(channels), interval,
            )

    async def _hop_loop(self, channels: List[int], interval: float):
        import itertools
        channel_cycle = itertools.cycle(channels)
        last_channel = None
        while self._is_hopping:
            channel = next(channel_cycle)
            # Skip re-tuning the channel we're already on — each tune briefly blanks the
            # radio (PLL relock), dropping frames. Matters most with a single-channel filter.
            if channel != last_channel:
                # Shield the tune: its executor thread can't be cancelled, so an unshielded
                # mid-tune stop_hopping() would let it finish and land the chip on a stale
                # channel. stop_hopping drains self._tune_task instead.
                self._tune_task = asyncio.ensure_future(
                    self.set_channel(channel, scan=True)
                )
                try:
                    await asyncio.shield(self._tune_task)
                except Exception as e:
                    # The background hopper is the one automatic poker of the USB pipe: on an
                    # unplug its tune raises here. Surface it as adapter loss (device-gone) and
                    # stop hopping either way, rather than letting it kill the hop task unhandled.
                    if is_device_gone(e):
                        self._on_device_lost(e)
                    break
                last_channel = channel
            await asyncio.sleep(interval)

    async def stop_hopping(self):
        """Cancel the hopping task, then drain any in-flight tune.

        Cancelling the loop can't stop the executor thread running a mid-flight tune, so we
        await self._tune_task afterward — otherwise that orphan moves the chip off the
        channel Focus is about to pin.
        """
        async with self._hop_lock:
            task = self._hopping_task
            # Clear state FIRST so a start_hopping blocked on _hop_lock sees a clean slate.
            self._is_hopping = False
            self._hopping_task = None
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # Drain the shielded tune the cancel interrupted (its executor thread runs on).
            tune = self._tune_task
            self._tune_task = None
            if tune is not None and not tune.done():
                try:
                    await tune
                except Exception:
                    pass
            logger.info(f"Stopped channel hopping on {self._chipset}")

    async def close(self):
        """Halts the driver loops and releases the USB interface."""
        await self.stop_hopping()
        await self.driver.close()