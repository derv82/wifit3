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


def _replay_to_int(replay_hex: str) -> int:
    return int.from_bytes(bytes.fromhex(replay_hex), "big")


# A real 4-way completes sub-second; allow a generous window for retransmits and
# jitter. Two frames whose replay counters happen to match but that arrived
# farther apart than this are from different association attempts — pairing them
# mixes ANonce/SNonce from different PTKs, i.e. an uncrackable hashline. The
# minute-late-M2 bug lived exactly here.
_EAPOL_PAIR_WINDOW_S = 2.0


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

    # -- Crack-validity -------------------------------------------------------

    def _within_window(self, a: EapolFrame, b: EapolFrame) -> bool:
        """True if two frames are close enough in time to be one handshake.
        Skipped when either timestamp is unset (0.0) — keeps fixtures / any
        pre-timestamp capture working off the replay-counter rules alone."""
        if a.timestamp <= 0 or b.timestamp <= 0:
            return True
        return abs(a.timestamp - b.timestamp) <= _EAPOL_PAIR_WINDOW_S

    def _anonce_consistent(self, m1: EapolFrame) -> bool:
        """Guard for M1-sourced ANonce pairs. The AP reuses one ANonce across
        M1 and M3 of a handshake, so if we captured this instance's M3
        (replay == M1.replay + 1) with a *different* nonce, this M1 belongs to
        another association and must not supply the ANonce."""
        if len(m1.nonce) != 32:
            return True
        target_rc = _replay_to_int(m1.replay_hex) + 1
        for f in self.eapol_frames:
            if (
                f.msg_num == 3
                and _replay_to_int(f.replay_hex) == target_rc
                and len(f.nonce) == 32
                and f.nonce != m1.nonce
            ):
                return False
        return True

    def find_valid_pair(self) -> Optional[Tuple[EapolFrame, EapolFrame]]:
        """Return the highest-confidence hashcat-valid (lower-msg, higher-msg)
        pair from a *single handshake instance*, else None.

        A pair must belong to one association: the right replay-counter
        relationship AND arrival within ``_EAPOL_PAIR_WINDOW_S`` AND (when the
        ANonce comes from M1) a consistent ANonce. Replay counters alone are
        insufficient — they restart per re-association, so an M1 from one
        attempt and an M2 from a later one can collide and produce an
        uncrackable hashline. Preference favours M2+M3 / M3+M4, where M3 carries
        the ANonce right beside the MIC frame (self-consistent); M1-sourced
        pairs are the fragile ones and carry the extra guards.

        Accepted pairs (hcxpcapngtool semantics):
          M2+M3 (M3.replay == M2.replay + 1)
          M3+M4 (same replay)
          M1+M2 (same replay)
          M1+M4 (M4.replay == M1.replay + 1)
        """
        return next(self._iter_valid_pairs(), None)

    def _iter_valid_pairs(self):
        """Yield every same-instance hashcat-valid (lower-msg, higher-msg) pair,
        in confidence order (M2+M3, M3+M4, M1+M2, M1+M4). ``find_valid_pair``
        takes the first; instance counting walks them all."""
        by_msg: Dict[int, List[EapolFrame]] = {}
        for f in self.eapol_frames:
            if f.msg_num:
                by_msg.setdefault(f.msg_num, []).append(f)

        def rc(f: EapolFrame) -> int:
            return _replay_to_int(f.replay_hex)

        same = lambda a, b: a.replay_hex == b.replay_hex          # noqa: E731
        plus1 = lambda a, b: rc(b) == rc(a) + 1                   # noqa: E731

        def gen(ma, mb, rc_ok, *, anonce_from_m1=False):
            for a in by_msg.get(ma, []):
                for b in by_msg.get(mb, []):
                    if not rc_ok(a, b):
                        continue
                    if not self._within_window(a, b):
                        continue
                    if anonce_from_m1 and not self._anonce_consistent(a):
                        continue
                    yield (a, b)

        yield from gen(2, 3, plus1)
        yield from gen(3, 4, same)
        yield from gen(1, 2, same, anonce_from_m1=True)
        yield from gen(1, 4, plus1, anonce_from_m1=True)

    def valid_pairs_by_instance(self) -> Dict[bytes, Tuple[EapolFrame, EapolFrame]]:
        """Map each captured handshake *instance* (keyed by its ANonce — fresh
        per association) to its best valid pair. A single 4-way collapses to one
        entry no matter how many M-frame combos validate; a re-handshake (new
        ANonce) adds another. Requires a beacon (needed to crack)."""
        out: Dict[bytes, Tuple[EapolFrame, EapolFrame]] = {}
        if not self.beacon_frame:
            return out
        for a, b in self._iter_valid_pairs():
            # ANonce comes from whichever frame is M1 or M3.
            anonce_frame = a if a.msg_num in (1, 3) else b
            if len(anonce_frame.nonce) == 32:
                out.setdefault(anonce_frame.nonce, (a, b))
        return out

    @property
    def complete_instances(self) -> int:
        """How many distinct 4-way handshakes we've captured for this client."""
        return len(self.valid_pairs_by_instance())

    @property
    def is_complete(self) -> bool:
        if not self.beacon_frame:
            return False
        return self.find_valid_pair() is not None

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
    kind: Literal["HS", "PMKID", "WEP"]
    timestamp: int                  # epoch seconds, parsed from the filename
    value: Optional[str] = None     # WEP key (hex) for WEP; None for HS/PMKID
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

    # Read-only capture history loaded from captures/ at scan start, matched to
    # this AP by BSSID. Drives the persisted Scanner badges + the Focus
    # "existing capture data" summary; never touches the live capture plumbing.
    persisted: List[PersistedCapture] = Field(default_factory=list)

    @property
    def has_capture(self) -> bool:
        """True iff there's something worth saving — a WPA handshake/PMKID, or
        a recovered WEP key."""
        if self.wep_key is not None:
            return True
        return any(hs.is_complete or hs.pmkid for hs in self.handshakes.values())

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
