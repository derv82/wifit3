import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional, Callable, Any, Dict, Set

from wifit3.engine.models import AccessPoint, Client, Handshake, EapolFrame
from wifit3.wlan.wep_store import WepCaptureStore

logger = logging.getLogger(__name__)


# Append a hex+ASCII dump of every frame that triggers a decloak. Lives in
# CWD so a `dir` lands it; cheap (decloaks are rare). Investigates whether
# short-SSID decloaks ("F", "7", etc.) are legit, parser bugs, or spoofed
# probe responses on the channel.
DECLOAK_LOG_PATH = Path("wifit3-decloak.log")


def _log_decloak_frame(
    bssid: str, ssid: str, method: str, frame_type: str, raw: Optional[bytes]
) -> None:
    """Append a forensic dump of the frame that triggered a decloak. Silent
    on failure — must never break the RX loop."""
    if not raw:
        return
    try:
        with DECLOAK_LOG_PATH.open("a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"=== {ts}  {method}  type={frame_type}  len={len(raw)} ===\n")
            f.write(f"  bssid={bssid}  ssid={ssid!r}\n")
            for i in range(0, len(raw), 16):
                chunk = raw[i : i + 16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                ascii_part = "".join(
                    chr(b) if 0x20 <= b < 0x7F else "." for b in chunk
                )
                f.write(f"  {i:04x}  {hex_part:<48}  {ascii_part}\n")
            f.write("\n")
    except Exception:
        pass


def _enc_rank(label: str) -> int:
    """Rank an encryption label by *evidence strength*, not recency.

    OPEN(0) < WEP(1) < WPA*(2). A WPA/WPA2/WPA3/OWE label means we actually
    parsed an RSN/WPA IE — which a truncated/corrupt beacon can't fabricate —
    whereas WEP/OPEN are inferred from the always-present Privacy bit when no
    IE was found. Used to keep the strongest evidence ever seen so a dropped
    IE on a weak radio can't flap a WPA2 AP to "WEP" (and a mis-parsed first
    beacon can't pin it there either). See _on_frame_parsed.
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
    """
    High-level 802.11 abstraction for a hardware driver.
    The UI interacts exclusively with this class.
    """
    def __init__(self, driver_instance: Any, name: str, description: str):
        self.driver = driver_instance
        self.name = name
        self.description = description
        self.current_channel = 1
        
        self.access_points: Dict[str, AccessPoint] = {}
        self.clients: Dict[str, Client] = {}

        # Passive WEP IV tallying (RX-only). Hooked from _on_frame_parsed for
        # every WEP-encrypted Data frame; updates AccessPoint.wep counters.
        self.wep_store = WepCaptureStore()

        # MACs we forged for our own active attacks (e.g. PMKID harvest).
        # Frames addressed to these MACs come from the AP, but they aren't
        # "real clients" — skip client registration and don't append EAPOL
        # retries to the handshake. PMKID extraction still runs.
        self.forged_macs: Set[str] = set()

        # The single stable forged STA MAC for a long-lived active attack
        # (WEP fake-auth). Unlike forged_macs these ARE registered as a
        # client — tagged is_self so the UI shows "YOU"
        self.self_macs: Set[str] = set()

        self._rx_callbacks: List[Callable[[bytes, int, float], None]] = []
        self._hopping_task: Optional[asyncio.Task] = None
        self._is_hopping = False
        # Serializes start_hopping / stop_hopping so they can't interleave.
        # Without this, stop_hopping's `await task.cancel()` yields control
        # while _is_hopping is already False, letting a concurrent
        # start_hopping (e.g. from a screen-resume callback) create a new
        # task whose reference then gets clobbered when stop_hopping clears
        # _hopping_task. Orphaned tasks ping-pong the chip across channels.
        self._hop_lock = asyncio.Lock()

        if hasattr(self.driver, 'register_rx_callback'):
            self.driver.register_rx_callback(self._on_frame_parsed)

    def _on_frame_parsed(self, parsed: dict):
        """
        Mutator callback. Receives a flat dictionary from the hardware driver
        and updates the AccessPoint registry.
        """
        frame_type = parsed.get("type")
        bssid = parsed.get("bssid")
        rssi = parsed.get("rssi", -100)

        # Fan out the raw frame to any (rx_callback,) subscribers (used by
        # attacks like SAEGroupProbeAttack that watch for specific reply
        # frames). Done early so subscribers see frames even when our own
        # state-update path bails on bssid filtering below.
        raw = parsed.get("raw")
        if raw is not None and self._rx_callbacks:
            self._fire_rx_callbacks(raw, rssi)

        # Diagnostic (WIFIT3_LOG=debug): trace every data/EAPOL frame's
        # direction. We see M1/M3 (from_ds, AP→client) but never M2/M4
        # (to_ds, client→AP) — this reveals whether client→AP frames reach
        # software at all (→ RX filter / PHY) or arrive but mis-parse (→ here
        # they'd show as "data" not "eapol"). Guarded so it's free when off.
        if frame_type in ("data", "eapol", "wep_data") and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[RXFRAME] %-9s to_ds=%s from_ds=%s %s -> %s (bssid %s)",
                frame_type, parsed.get("to_ds"), parsed.get("from_ds"),
                parsed.get("source"), parsed.get("dest"), bssid,
            )

        if not bssid or bssid == "Unknown" or bssid == "ff:ff:ff:ff:ff:ff":
            return
        
        # We primarily build APs from beacons and probe responses
        if frame_type in ("beacon", "probe_resp"):
            ssid = parsed.get("ssid")
            channel = parsed.get("channel", self.current_channel)
            enc = parsed.get("encryption", "OPEN")
            akms = parsed.get("akms", []) or []
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

            if bssid not in self.access_points:
                self.access_points[bssid] = AccessPoint(
                    bssid=bssid,
                    ssid=ssid if ssid != "<hidden>" else None,
                    channel=channel,
                    signal=rssi,
                    encryption=enc,
                    akms=list(akms),
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
                )
                self._recompute_siblings_for(bssid)
                if ssid and ssid != "<hidden>":
                    logger.info(f"[NEW AP] Found '{ssid}' ({bssid}) on CH {channel}")
            else:
                ap = self.access_points[bssid]
                old_channel = ap.channel
                if frame_type == "beacon":
                    ap.beacons += 1

                # Update SSID if it was hidden and we now see it. Method label
                # tracks the actual frame type: "beacon" for the rare case where
                # a hidden AP starts broadcasting its SSID in beacons (reconfig
                # / firmware quirk), "probe_resp" for the normal directed-probe
                # echo path. CaptureEventDetector fires a UI event off this.
                if ssid and ssid != "<hidden>":
                    if not ap.ssid or ap.ssid == "<hidden>":
                        ap.decloak_method = frame_type  # "beacon" or "probe_resp"
                        _log_decloak_frame(
                            bssid, ssid, frame_type, frame_type, parsed.get("raw")
                        )
                    ap.ssid = ssid

                # Smooth RSSI (simple average for now, could use EMA)
                ap.signal = (ap.signal + rssi) // 2

                # Update channel if it shifted — sibling links are
                # channel-scoped, so re-evaluate on actual moves.
                ap.channel = channel
                if old_channel != channel:
                    self._recompute_siblings_for(bssid)
                # Keep the strongest encryption evidence ever seen rather than
                # blindly trusting the latest beacon. A WPA/WPA2/WPA3/OWE label
                # comes from a parsed RSN/WPA IE (truncation can't fabricate
                # one); "WEP" is just the Privacy-bit fallback when no IE was
                # found — which a dropped/truncated beacon yields exactly like a
                # real WEP AP. Overwriting unconditionally made WPA2 APs flicker
                # to "WEP" on weak radios (RTL8188EUS). A WPA*-vs-WPA* update
                # still passes (rank 2 >= 2), so WPA3/PMF config reloads stay in
                # sync; only a downgrade to WEP/OPEN is suppressed. Mirrors the
                # "only when the IE is present" rule the WPS block below uses.
                if _enc_rank(enc) >= _enc_rank(ap.encryption):
                    ap.encryption = enc
                    ap.akms = list(akms)
                    ap.pairwise_cipher = pairwise_cipher
                    ap.wpa3 = wpa3
                    ap.transition_mode = transition_mode
                    ap.pmf_capable = pmf_capable
                    ap.pmf_required = pmf_required
                # Only refresh WPS when this frame actually carried the IE —
                # a probe response without it must not clear a known WPS flag.
                # (locked/state CAN change, so re-read when present.)
                if wps:
                    ap.wps = True
                    ap.wps_locked = wps_locked
                    ap.wps_version = wps_version
                    ap.wps_config_methods = wps_config_methods
                    ap.wps_device_password_id = wps_device_password_id

            # Always bump the recency clock on the AP we just saw — drives
            # stale-row dim-out and "Last Beacon: Ns ago" in FocusView.
            self.access_points[bssid].last_seen = time.time()

            # Always stash the latest RSN IE bytes (covers both new + existing
            # AP branches above). PMKID harvest echoes this into its Assoc Req.
            rsn_ie = parsed.get("rsn_ie_raw")
            if rsn_ie:
                self.access_points[bssid].rsn_ie = rsn_ie

        # WEP capture — passive, RX-only. Only observe frames whose AP we've
        # already classified as WEP from a beacon (guards against a stray
        # ExtIV-clear frame on a non-WEP AP). The store does all the routing
        # (IV count, ARP-replay seed, PTW crack sample) — the interface stays
        # out of the ARP-size / cipher-offset business.
        if frame_type == "wep_data" and bssid in self.access_points:
            ap = self.access_points[bssid]
            if (ap.encryption or "").upper() == "WEP":
                stats = self.wep_store.observe(bssid, parsed)
                if stats is not None and ap.wep is None:
                    ap.wep = stats

        # Client Tracking
        if frame_type in ("probe_req", "assoc_req", "data", "wep_data", "eapol", "deauth", "assoc_resp"):
            client_mac = None
            source = parsed.get("source")
            dest = parsed.get("dest")
            
            if frame_type == "probe_req":
                client_mac = source
            else:
                # Deduce client MAC (the one that isn't the BSSID)
                if source and source != bssid: client_mac = source
                elif dest and dest != bssid: client_mac = dest
            
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
                
                # Track association
                if frame_type in ("assoc_req", "data", "wep_data", "eapol"):
                    if bssid: client.bssid = bssid
                    
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
                            _log_decloak_frame(
                                bssid, ssid, "assoc_req", frame_type, parsed.get("raw")
                            )
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
                    )
                    ap.handshakes[client_mac] = hs

                # For forged MACs (our own active-attack STAs) we still keep
                # the Handshake entry so PMKID has somewhere to land, but we
                # skip the EAPOL frame list — those are just AP retries of M1
                # we'll never respond to. Avoids the spurious "Partial x1" in
                # the per-client handshake column.
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

                # Passive PMKID capture: AP's M1 sometimes carries a PMKID
                # KDE in Key Data. First non-zero PMKID wins — never clobber.
                pmkid = parsed.get("eapol_pmkid")
                if pmkid and not hs.pmkid:
                    hs.pmkid = pmkid
                    logger.info(
                        f"[PMKID] {bssid} <-> {client_mac} captured {pmkid.hex()}"
                    )

        # Beacon handling: stash the most recent beacon on the AP, and
        # back-fill any existing handshakes that don't have one yet (covers
        # the case where EAPOL arrived before the first beacon).
        if frame_type == "beacon" and bssid in self.access_points:
            ap = self.access_points[bssid]
            raw_beacon = parsed.get("raw")
            if raw_beacon:
                ap.last_beacon_frame = raw_beacon
                for hs in ap.handshakes.values():
                    if not hs.beacon_frame:
                        hs.beacon_frame = raw_beacon

    SIBLING_BIT_DIFF_MAX = 4

    def _recompute_siblings_for(self, bssid: str) -> None:
        """Refresh sibling links for ``bssid`` against the whole registry.

        Sibling rule: same channel AND (Hamming distance ≤ 4 bits OR
        exactly one byte differs). Two-branch OR because vendors use two
        distinct schemes:
          - Multi-byte single-bit flips (U/L bit + 1-2 bits elsewhere) →
            caught by the bit-diff branch (2 bits across 2 bytes is the
            common shape).
          - Single-byte multi-bit randomization (deliberately distinct
            first byte) → caught by the byte-diff branch (up to 8 bits
            packed into one byte still reads as a sibling).
        FP rate with ~50 APs in range is still ~10⁻⁶ — the same-channel
        constraint does most of the disambiguation work.

        Mutates the sibling list on ``bssid`` AND on every counterpart so
        the relationship stays bidirectional. Called when an AP is added
        or its channel actually changes; O(N) per call.
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

    async def set_channel(self, channel: int) -> bool:
        """Translates a channel number into the driver's register sequences."""
        success = await self.driver.set_channel(channel)
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
        """
        UI registers a function here.
        Expected signature: func(frame_bytes, rssi, timestamp)
        """
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
        sw_seq: Optional[int] = None,
    ) -> bool:
        """
        Injects a raw 802.11 frame.
        The underlying driver is responsible for wrapping it in the correct
        hardware descriptors (e.g., ath_tx_status) before sending.

        ``sw_seq`` requests a software-supplied 802.11 sequence number for this
        frame instead of the hardware-assigned one — needed so a fragment train
        (WEP fragmentation) shares one sequence number across its fragments.
        Drivers that don't support it ignore the hint (hardware seq); the
        fragmentation attack checks ``supports_sw_seq`` before relying on it.
        """
        if hasattr(self.driver, 'inject_frame'):
            # Only pass sw_seq to drivers that advertise support — others'
            # inject_frame has no such parameter and would raise. Callers that
            # NEED software seq must gate on supports_sw_seq first.
            if sw_seq is not None and getattr(self.driver, "SUPPORTS_SW_SEQ", False):
                return await self.driver.inject_frame(
                    frame_bytes, use_no_ack, sw_seq=sw_seq
                )
            return await self.driver.inject_frame(frame_bytes, use_no_ack)
        logger.warning(f"Driver for {self.name} does not support injection.")
        return False

    @property
    def supports_sw_seq(self) -> bool:
        """Whether the driver can inject a frame with a software-supplied 802.11
        sequence number (needed for WEP fragmentation's fragment trains)."""
        return bool(getattr(self.driver, "SUPPORTS_SW_SEQ", False))
    
    async def deauth(self, ap_bssid: str, client_bssid: str, burst_count: int = 10):
        """
        Sends a burst of Deauthentication frames to the AP and the Client.
        """
        ap_bssid = ap_bssid.lower()
        client_bssid = client_bssid.lower()
        
        # 1. Get the target channel
        target_chan = self.current_channel
        if ap_bssid in self.access_points:
            target_chan = self.access_points[ap_bssid].channel
            print(f"[DEAUTH] Found AP {ap_bssid} on channel {target_chan}.")
        else:
            print(f"[DEAUTH] AP {ap_bssid} not in registry. Defaulting to channel {target_chan}.")

        import struct
        
        def _str_to_mac(mac_str: str) -> bytes:
            return bytes(int(x, 16) for x in mac_str.split(':'))
            
        ap_mac = _str_to_mac(ap_bssid)
        cl_mac = _str_to_mac(client_bssid)
        
        # Frame Control: 0xC0 (Deauth, Mgmt), Flags: 0x00
        # Duration: 0x0000 (Let hardware fill if needed, or leave 0)
        # Reason Code: 7 (Class 3 frame received from nonassociated STA)
        fc_dur = b'\xc0\x00\x00\x00'
        reason = struct.pack("<H", 7)
        seq = b'\x00\x00' # Hardware usually overwrites seq
        
        # 1. Deauth the Client (Spoofing the AP)
        # Addr1=Dest(Client), Addr2=Source(AP), Addr3=BSSID(AP)
        client_deauth = fc_dur + cl_mac + ap_mac + ap_mac + seq + reason
        
        # 2. Deauth the AP (Spoofing the Client)
        # Addr1=Dest(AP), Addr2=Source(Client), Addr3=BSSID(AP)
        ap_deauth = fc_dur + ap_mac + cl_mac + ap_mac + seq + reason
        
        logger.info(f"Injecting Deauth Burst ({burst_count}x) on CH {target_chan}: {ap_bssid} <-> {client_bssid}")
        
        # Inject the frames using the hardware driver
        # We use use_no_ack=True for "fire and forget". We are spoofing, 
        # so ACKs will go to the real targets and cause endless hardware retries for us!
        for i in range(burst_count):
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
            # Skip redundant re-tunes: re-issuing set_channel for the channel
            # we're already parked on is pure RX disruption — each tune briefly
            # blanks the radio (PLL relock), dropping beacons/frames. Most
            # visible with a single-channel filter, which otherwise re-tuned
            # every `interval`. Multi-channel hopping still tunes every hop.
            if channel != last_channel:
                await self.set_channel(channel)
                last_channel = channel
            await asyncio.sleep(interval)

    async def stop_hopping(self):
        """Cancels the hopping task."""
        async with self._hop_lock:
            task = self._hopping_task
            # Clear state FIRST so a concurrent start_hopping (which would
            # have been blocked on _hop_lock) sees a clean slate when it
            # acquires the lock after we release it.
            self._is_hopping = False
            self._hopping_task = None
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            logger.info(f"Stopped channel hopping on {self.name}")

    async def close(self):
        """Halts the driver loops and releases the USB interface."""
        await self.stop_hopping()
        await self.driver.close()