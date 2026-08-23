"""Scan-registry models: an access point and its per-AP sub-data (WEP IV counters,
previously-saved capture artifacts).
"""
import time
from dataclasses import dataclass, field
from typing import Optional, List, Literal, Dict

from .handshake import Handshake


@dataclass
class WepStats:
    """WEP IV counters for one AP: unique IVs and total WEP frames seen."""
    unique_ivs: int = 0
    total_frames: int = 0


@dataclass
class PersistedCapture:
    """One previously-saved capture artifact found under captures/."""
    type: Literal["HS", "PMKID", "WEP", "WPS"]
    timestamp: int                  # epoch seconds, parsed from the filename
    path: str                       # source file under captures/
    value: Optional[str] = None     # WEP key (hex) / WPS PSK; None for HS/PMKID


@dataclass
class AccessPoint:
    bssid: str
    ssid: Optional[str] = None
    channel: int = 1
    encryption: Optional[str] = "Unknown"
    # Structured security fields from the RSN IE; `encryption` (above) is the airodump-style string.
    akms: List[str] = field(default_factory=list)
    # AKM suite numbers (00-0F-AC:N) from the RSN IE, parallel to `akms` (the names).
    akm_suites: List[int] = field(default_factory=list)
    pairwise_cipher: Optional[str] = None
    beacons: int = 0
    first_seen: float = field(default_factory=time.time)
    # Most recent beacon/probe-resp timestamp.
    last_seen: float = field(default_factory=time.time)
    wpa3: bool = False
    transition_mode: bool = False
    pmf_capable: bool = False
    pmf_required: bool = False
    beacon_protection: bool = False

    # WPS state decoded from the WPS vendor IE (tag 221, OUI 00:50:F2 type 4).
    wps: bool = False
    wps_locked: bool = False
    wps_version: Optional[str] = None  # "1.0" / "2.0"
    wps_config_methods: int = 0  # 0x1008 bitmask
    wps_device_password_id: Optional[int] = None  # 0x0004 = PBC
    # Set while the AP is advertising an active Registrar (PIN or, with
    # DevPwId 0x0004, a Push-Button walk window). Drives wps_pbc_active.
    wps_selected_registrar: bool = False

    # Most recent raw beacon bytes.
    last_beacon_frame: Optional[bytes] = None

    # Raw RSN IE bytes (tag 48, incl. the 2-byte tag header) as advertised in the AP's beacons.
    rsn_ie: Optional[bytes] = None

    # How this AP's SSID was learned, if it was ever hidden.
    # None = we never saw it hidden, or it's still hidden.
    decloak_method: Optional[str] = None

    # BSSIDs we believe are virtual interfaces of the same physical radio (Main + Guest +
    # IoT on one router). Bidirectional.
    siblings: List[str] = field(default_factory=list)

    # Per-client handshake captures, keyed by client MAC (clients can capture simultaneously).
    handshakes: Dict[str, Handshake] = field(default_factory=dict)

    # WEP IV counters, populated for WEP APs on the first encrypted Data frame (None otherwise).
    wep: Optional[WepStats] = None

    # Recovered WEP key (the cracker's payoff).
    wep_key: Optional[bytes] = None

    # Recovered WPS PSK from a successful Push-Button (PBC) capture.
    wps_pbc_psk: Optional[str] = None

    # Recovered WPS PIN + the passphrase it yielded, from a successful PIN brute-force.
    # Kept distinct from wps_pbc_psk (PIN vs Push-Button).
    wps_pin: Optional[str] = None
    wps_pin_psk: Optional[str] = None

    # Read-only capture history loaded from captures/ at scan start.
    persisted: List[PersistedCapture] = field(default_factory=list)

    # Smoothed RSSI per receiving card (card name -> dBm), written by WlanSink.
    signal_by_card: Dict[str, int] = field(default_factory=dict)

    @property
    def signal(self) -> int:
        """Strongest smoothed RSSI (dBm) across the cards that hear this AP; -100 if none yet."""
        return max(self.signal_by_card.values(), default=-100)

    @property
    def wps_pbc_active(self) -> bool:
        """True during a WPS Push-Button walk window: the AP advertises PBC
        (Device Password ID 0x0004) with an active Selected Registrar."""
        return (
            self.wps
            and self.wps_selected_registrar
            and self.wps_device_password_id == 0x0004
        )

    @property
    def known_psk(self) -> Optional[str]:
        """The passphrase we hold for this AP from any source, recovered this session
        (PBC/PIN) or loaded from a prior session's captures/ WPS file, else None. A WPS PIN
        alone (no PSK) does not count."""
        return (
            self.wps_pbc_psk
            or self.wps_pin_psk
            or next((p.value for p in self.persisted if p.type == "WPS" and p.value), None)
        )

    @property
    def has_psk(self) -> bool:
        """True once we hold this AP's passphrase (see known_psk)."""
        return self.known_psk is not None

    @property
    def is_hidden(self) -> bool:
        """No usable SSID: never seen, or still the "<hidden>" placeholder."""
        return not (self.ssid and self.ssid != "<hidden>")
