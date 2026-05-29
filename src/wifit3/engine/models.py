import time

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Literal, Set, Dict, Tuple


class EapolFrame(BaseModel):
    """
    A single parsed EAPOL-Key frame, classified by its role in the 4-way
    handshake (M1/M2/M3/M4) and tagged with the fields a cracker needs.
    """
    raw: bytes
    msg_num: int  # 1, 2, 3, 4, or 0 if unclassified (group rekey etc.)
    replay_hex: str
    nonce: bytes
    mic: bytes
    key_data_len: int
    # 802.1X version byte through end of key data — i.e. the portion of the
    # frame hashcat embeds in the mode-22000 hashline. May be empty if the
    # capture was truncated.
    eapol_payload: bytes = b""
    # Arrival time (epoch seconds), stamped by the interface on capture. Used to
    # bind frames to a single handshake instance: a real 4-way completes in well
    # under a second, so frames far apart are different associations even if
    # their replay counters coincide. 0.0 = unset (e.g. unit-test fixtures).
    timestamp: float = 0.0


class Handshake(BaseModel):
    """
    Captured WPA/WPA2 4-way handshake (or PMKID) for one (BSSID, client_mac)
    pair. ``is_complete`` is True only when the stored EAPOL frames actually
    form a hashcat-crackable pair (M1+M2, M2+M3, M3+M4, or M1+M4) — two M1
    retries with the same replay counter no longer mistakenly qualify.
    """
    bssid: str
    client_mac: str
    beacon_frame: Optional[bytes] = None

    # All EAPOL-Key frames we've seen for this client, in arrival order.
    # We never wipe — only append-with-dedup — so once a valid pair is
    # captured nothing can clobber it.
    eapol_frames: List[EapolFrame] = Field(default_factory=list)

    # Captured PMKID bytes (16 B from RSN IE in EAPOL M1). Forward-compat —
    # populated by the PMKID attack path when it lands.
    pmkid: Optional[bytes] = None

    # -- Crack-validity --------------------------------------------------------
    # Delegated to engine.wpa.handshake — the single source of truth shared with
    # the hc22000 / auto-save path, so "captured" and "saveable" can't diverge.
    # Deferred import: that module imports this one.

    def _crackable_pairs(self):
        from wifit3.engine.wpa import handshake as _wpa
        return _wpa.crackable_pairs(self)

    @staticmethod
    def _ordered(pair) -> Tuple[EapolFrame, EapolFrame]:
        """A CrackablePair rendered as a (lower-msg, higher-msg) frame tuple."""
        return tuple(sorted((pair.anonce_frame, pair.mic_frame),
                            key=lambda f: f.msg_num))

    def find_valid_pair(self) -> Optional[Tuple[EapolFrame, EapolFrame]]:
        """Highest-confidence crackable (lower-msg, higher-msg) pair, or None."""
        pairs = self._crackable_pairs()
        return self._ordered(pairs[0]) if pairs else None

    def valid_pairs_by_instance(self) -> Dict[bytes, Tuple[EapolFrame, EapolFrame]]:
        """Each crackable handshake instance (keyed by its ANonce — fresh per
        association) mapped to its (lower-msg, higher-msg) pair. Gated on a
        beacon: we don't announce a capture for an AP we've never heard beacon."""
        if not self.beacon_frame:
            return {}
        return {p.instance_key: self._ordered(p) for p in self._crackable_pairs()}

    @property
    def complete_instances(self) -> int:
        """Distinct crackable 4-way handshakes captured for this client."""
        return len(self.valid_pairs_by_instance())

    @property
    def is_complete(self) -> bool:
        return bool(self.beacon_frame) and bool(self._crackable_pairs())

    @property
    def captured_messages(self) -> Set[int]:
        """Set of message numbers (1-4) we've seen, ignoring unclassified."""
        return {f.msg_num for f in self.eapol_frames if f.msg_num}

    @property
    def total_eapol_frames(self) -> int:
        return len(self.eapol_frames)

    def has_frame(self, raw: bytes) -> bool:
        return any(f.raw == raw for f in self.eapol_frames)

class WepStats(BaseModel):
    """Light, UI-facing WEP IV counters for one AP.

    Deliberately just monotonic integers — the heavy buffers (the unique-IV
    set, full validation packets, captured ARPs) live in
    ``wlan.wep_store.WepCaptureStore`` so this model stays cheap to poll and
    serialize. ``WlanInterface`` attaches one of these to an AP the first
    time a WEP-encrypted Data frame for that BSSID is seen; live IV
    rate / ETA are computed on demand from the collector, not stored here.
    """
    unique_ivs: int = 0
    total_frames: int = 0


class PersistedCapture(BaseModel):
    """One previously-saved capture artifact found under captures/ at scan start.

    Read-only history, hydrated onto an AccessPoint by ``engine.capture_history``
    and matched by BSSID. Kept deliberately separate from the live
    ``handshakes`` / ``wep_key`` plumbing so it drives only the persisted
    Scanner badges and the Focus "existing capture data" summary — it never
    feeds the live CaptureEventDetector banners.
    """
    kind: Literal["HS", "PMKID", "WEP", "WPS"]
    timestamp: int                  # epoch seconds, parsed from the filename
    value: Optional[str] = None     # WEP key (hex) / WPS PSK; None for HS/PMKID
    path: str                       # source file under captures/


class AccessPoint(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    bssid: str
    ssid: Optional[str] = Field(default=None)
    channel: int = Field(default=1)
    signal: int = Field(default=-100)
    encryption: Optional[str] = Field(default="Unknown")
    # Structured security fields surfaced from the RSN IE parser. The UI uses
    # these to render colorized + AKM-dimmed labels; `encryption` (above) is
    # still the airodump-style string for saved captures + logs.
    akms: List[str] = Field(default_factory=list)
    pairwise_cipher: Optional[str] = Field(default=None)
    beacons: int = Field(default=0)
    first_seen: float = Field(default_factory=lambda: time.time())
    # Most recent beacon/probe-resp timestamp. Drives "Last Beacon: Ns ago"
    # in FocusView and the stale-row dim-out in ScannerView.
    last_seen: float = Field(default_factory=lambda: time.time())
    wpa3: bool = Field(default=False)
    transition_mode: bool = Field(default=False)
    pmf_capable: bool = Field(default=False)
    pmf_required: bool = Field(default=False)

    # WPS state decoded from the WPS vendor IE (tag 221, OUI 00:50:F2 type 4)
    # in beacons / probe responses. `wps_locked` is the key attack signal —
    # locked APs rate-limit PIN attempts so Reaver/Pixie won't progress.
    wps: bool = Field(default=False)
    wps_locked: bool = Field(default=False)
    wps_version: Optional[str] = Field(default=None)        # "1.0" / "2.0"
    wps_config_methods: int = Field(default=0)              # 0x1008 bitmask
    wps_device_password_id: Optional[int] = Field(default=None)  # 0x0004 = PBC
    # Set while the AP is advertising an active Registrar (PIN or, with
    # DevPwId 0x0004, a Push-Button walk window). Drives wps_pbc_active.
    wps_selected_registrar: bool = Field(default=False)

    # Most recent raw beacon bytes — used so per-client handshakes created
    # AFTER the first beacon still get a beacon stamped onto them.
    last_beacon_frame: Optional[bytes] = Field(default=None)

    # Raw RSN IE bytes (tag 48, including the 2-byte tag header) as
    # advertised in the AP's beacons. Used by the PMKID harvester to echo
    # the AP's exact RSN config in its forged Assoc Req — some APs reject
    # mismatched IEs with status 40 / unsupported-cipher.
    rsn_ie: Optional[bytes] = Field(default=None)

    # How this AP's SSID was learned, if it was ever hidden. None means
    # we either never saw it hidden, or it's still hidden. Set once at
    # the moment of transition by WlanInterface and consumed by the
    # CaptureEventDetector to surface a "Decloaked" event.
    decloak_method: Optional[str] = Field(default=None)

    # BSSIDs we believe are virtual interfaces of the same physical radio
    # (e.g. Main + Guest + IoT on the same router). Rule (5-of-6 byte
    # match + same channel) catches both "increment last byte" and
    # "locally-administered first byte" vendor schemes — see
    # WlanInterface._recompute_siblings_for. Maintained bidirectionally.
    siblings: List[str] = Field(default_factory=list)

    # Cached SAE-Commit probe results, keyed by finite-cyclic group ID.
    # Values are "supported" or "rejected"; timeouts / unknowns are NOT stored
    # so the next probe re-tries them. Used to skip already-determined groups
    # on subsequent SAE probes and to drive the SECURITY panel's "SAE Groups"
    # row. Dragonblood-relevant groups (22/23/24) flagged in the UI.
    sae_groups: Dict[int, str] = Field(default_factory=dict)

    # Per-client handshake captures, keyed by client MAC. Replaces the old
    # single-handshake-per-AP field — multiple clients can be capturing
    # simultaneously and we must not overwrite a complete one when a new
    # client's EAPOL arrives.
    handshakes: Dict[str, Handshake] = Field(default_factory=dict)

    # WEP IV counters, populated only for WEP APs once the first encrypted
    # Data frame arrives (None on every other AP). The Scanner ENCRYPT cell
    # and Focus CAPTURE panel read this; the live IV-acquisition rate / ETA
    # come from the WepCaptureStore keyed by this BSSID.
    wep: Optional[WepStats] = Field(default=None)

    # Recovered WEP key (the cracker's payoff). Lives on the AP so it survives
    # the Generate-IVs campaign being torn down, and so Save can write it out.
    wep_key: Optional[bytes] = Field(default=None)

    # Recovered WPS PSK from a successful Push-Button (PBC) capture. The payoff
    # of the opportunistic PBC attack; persisted to captures/ on capture.
    wps_pbc_psk: Optional[str] = Field(default=None)

    # Recovered WPS PIN + the passphrase it yielded, from a successful PIN
    # brute-force. Kept distinct from wps_pbc_psk so the win-event log can say
    # which attack found the passphrase (PIN vs Push-Button).
    wps_pin: Optional[str] = Field(default=None)
    wps_pin_psk: Optional[str] = Field(default=None)

    # Read-only capture history loaded from captures/ at scan start, matched to
    # this AP by BSSID. Drives the persisted Scanner badges + the Focus
    # "existing capture data" summary; never touches the live capture plumbing.
    persisted: List[PersistedCapture] = Field(default_factory=list)

    @property
    def wps_pbc_active(self) -> bool:
        """True during a WPS Push-Button walk window — the AP advertises PBC
        (Device Password ID 0x0004) with an active Selected Registrar. This is
        the (passive, beacon-derived) trigger for opportunistic PBC capture."""
        return (
            self.wps
            and self.wps_selected_registrar
            and self.wps_device_password_id == 0x0004
        )

class Client(BaseModel):
    """
    Represents a wireless client (e.g., a phone or laptop).
    Maps to the bottom table in airodump-ng.
    """
    model_config = ConfigDict(validate_assignment=True)
    
    mac: str
    bssid: Optional[str] = Field(default=None) # The AP it is currently connected to or probing for
    signal: int = Field(default=-100)
    packets: int = Field(default=0)
    probed_ssids: Set[str] = Field(default_factory=set) # List of SSIDs this client is actively searching for
    # True for the forged STA *we* inject as (e.g. WEP fake-auth). Rendered as
    # "YOU" in the client table — honest (it's this device, not a stranger)
    is_self: bool = Field(default=False)
