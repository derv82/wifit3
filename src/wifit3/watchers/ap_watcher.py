"""Detect new events on access points (EAPOL frames, handshakes, PMKIDs, decloaks, recovered
credentials) by diffing AP state across polls."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import AbstractSet, Dict, Iterable, List, NamedTuple, Optional, Set

from wifit3.crack import handshake as wpa
from wifit3.models import AccessPoint, Handshake, HandshakeMessage
from wifit3.persist.config import Config
from wifit3.persist.vault import HashlineSaveResult, Vault
from wifit3.wlan.array import WlanArray


@dataclass(frozen=True)
class ApEvent:
    bssid: str
    ssid: Optional[str]
    channel: int


@dataclass(frozen=True)
class EapolFrameSeen(ApEvent):
    client_mac: str
    msg_num: int                 # 1-4, or 0 if unclassified
    replay_hex: str
    # The hashcat-relevant per-field flags (crack.handshake.describe) the aggregator ticks.
    has_nonce: bool
    has_mic: bool
    eapol_complete: bool
    has_pmkid: Optional[bool]    # M1 only: the PMKID KDE rides M1's key data


@dataclass(frozen=True)
class HandshakeCaptured(ApEvent):
    client_mac: str
    pair_label: str              # "M1+M2", "M2+M3", ...
    authorized: bool             # M3/M4 in the pair: the AP confirmed the client's PSK
    save_result: Optional[HashlineSaveResult] = None   # None when not saved (no SSID yet)


@dataclass(frozen=True)
class UncrackableHandshakeSeen(ApEvent):
    client_mac: str
    akm_label: str               # EAP / OWE: a usable 4-way the AKM gate withheld


@dataclass(frozen=True)
class PmkidCaptured(ApEvent):
    client_mac: str
    save_result: Optional[HashlineSaveResult] = None   # None when not saved (no SSID yet)


@dataclass(frozen=True)
class ApDecloaked(ApEvent):
    method: Optional[str]        # AccessPoint.decloak_method


@dataclass(frozen=True)
class PbcWindowOpened(ApEvent):
    pass


@dataclass(frozen=True)
class WepKeyRecovered(ApEvent):
    key_hex: str


@dataclass(frozen=True)
class WpsPinRecovered(ApEvent):
    pin: str                     # "" when the router accepts an empty PIN


@dataclass(frozen=True)
class WpsPskRecovered(ApEvent):
    psk: str


@dataclass(frozen=True)
class WpsPbcPskRecovered(ApEvent):
    psk: str


class ApWatcher:
    """Polls every AP in the array except twins and silenced APs, saves new handshakes and
    PMKIDs to the Vault, and returns the new events. A hidden AP's captures are saved, and
    reported again, once its SSID is known."""

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._array: Optional[WlanArray] = None
        self._detector = CaptureEventDetector()
        self._captures_waiting_for_ssid: Dict[str, List[HandshakeCaptured | PmkidCaptured]] = {}

    def poll(self, array: WlanArray) -> List[ApEvent]:
        if array is not self._array:
            self._array = array
            self._detector = CaptureEventDetector()
            self._captures_waiting_for_ssid = {}
        events: List[ApEvent] = []
        self._detector.evict_stale_aps(array.access_points.keys())
        for ap in array.get_access_points(include_eviltwin=False):
            if Config.is_silenced(ap.bssid):
                continue
            for event in self._detector.poll(ap, array.forged_macs):
                if isinstance(event, (HandshakeCaptured, PmkidCaptured)):
                    event = self._saved(ap, event)
                    if event.save_result is None and ap.is_hidden:
                        self._captures_waiting_for_ssid.setdefault(ap.bssid, []).append(event)
                events.append(event)
            if not ap.is_hidden and ap.bssid in self._captures_waiting_for_ssid:
                for event in self._captures_waiting_for_ssid.pop(ap.bssid):
                    events.append(self._saved(ap, replace(event, ssid=ap.ssid)))
        return events

    def _saved(self, ap: AccessPoint,
               event: HandshakeCaptured | PmkidCaptured) -> HandshakeCaptured | PmkidCaptured:
        if isinstance(event, HandshakeCaptured):
            return replace(event, save_result=self._vault.save_handshake(ap, event.client_mac))
        return replace(event, save_result=self._vault.save_pmkid(ap, event.client_mac))


class ApClient(NamedTuple):
    bssid: str
    client_mac: str


class HandshakeInstance(NamedTuple):
    bssid: str
    client_mac: str
    anonce: bytes
    authorized: bool


def _pairing_inputs(handshake: Handshake) -> tuple:
    """Every Handshake field the pairing code reads; unchanged inputs mean unchanged pairs."""
    last_message = handshake.messages[-1] if handshake.messages else None
    return (last_message, len(handshake.messages), handshake.beacon_frame is not None,
            tuple(handshake.akm_offered), handshake.vendor_akm_offered, handshake.rsn_ie_seen)


class DecloakDetector:
    """An AP seen hidden that now has an SSID."""

    def __init__(self) -> None:
        self._hidden_bssids: Set[str] = set()
        self._reported_bssids: Set[str] = set()

    def poll(self, ap: AccessPoint) -> List[ApDecloaked]:
        if ap.is_hidden:
            self._hidden_bssids.add(ap.bssid)
            return []
        if ap.bssid not in self._hidden_bssids or ap.bssid in self._reported_bssids:
            return []
        self._reported_bssids.add(ap.bssid)
        return [ApDecloaked(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel, method=ap.decloak_method)]


class EapolFrameDetector:
    """Every EAPOL frame appended to a client's handshake, retransmits included."""

    def __init__(self) -> None:
        self._last_reported_frames: Dict[ApClient, HandshakeMessage] = {}

    def poll(self, ap: AccessPoint, client_mac: str, handshake: Handshake) -> List[EapolFrameSeen]:
        key = ApClient(ap.bssid, client_mac)
        last_reported = self._last_reported_frames.get(key)
        new_frames = []
        for frame in reversed(handshake.messages):
            if frame is last_reported:
                break
            new_frames.append(frame)
        new_frames.reverse()
        events = []
        for frame in new_frames:
            info = wpa.describe(frame)
            events.append(EapolFrameSeen(
                bssid=ap.bssid,
                ssid=ap.ssid,
                channel=ap.channel,
                client_mac=client_mac,
                msg_num=frame.msg_num,
                replay_hex=frame.replay_hex,
                has_nonce=info.has_nonce,
                has_mic=info.has_mic,
                eapol_complete=info.eapol_complete,
                has_pmkid=bool(handshake.pmkid) if frame.msg_num == 1 else None,
            ))
        if handshake.messages:
            self._last_reported_frames[key] = handshake.messages[-1]
        return events


class HandshakeDetector:
    """Each crackable handshake instance (ANonce), and again when it becomes authorized."""

    def __init__(self) -> None:
        self._reported_instances: Set[HandshakeInstance] = set()
        self._polled_pairing_inputs: Dict[ApClient, tuple] = {}

    def poll(self, ap: AccessPoint, client_mac: str, handshake: Handshake) -> List[HandshakeCaptured]:
        key = ApClient(ap.bssid, client_mac)
        pairing_inputs = _pairing_inputs(handshake)
        if self._polled_pairing_inputs.get(key) == pairing_inputs:
            return []
        self._polled_pairing_inputs[key] = pairing_inputs
        events = []
        for anonce, (first, second) in handshake.valid_pairs_by_instance().items():
            instance = HandshakeInstance(ap.bssid, client_mac, anonce, authorized=second.msg_num > 2)
            if instance in self._reported_instances:
                continue
            self._reported_instances.add(instance)
            events.append(HandshakeCaptured(
                bssid=ap.bssid,
                ssid=ap.ssid,
                channel=ap.channel,
                client_mac=client_mac,
                pair_label=f"M{first.msg_num}+M{second.msg_num}",
                authorized=instance.authorized,
            ))
        return events


class UncrackableHandshakeDetector:
    """A usable 4-way whose AKM (EAP/OWE) has no -m 22000 crack path; once per AP."""

    def __init__(self) -> None:
        self._reported_bssids: Set[str] = set()
        self._polled_pairing_inputs: Dict[ApClient, tuple] = {}

    def poll(self, ap: AccessPoint, client_mac: str,
             handshake: Handshake) -> List[UncrackableHandshakeSeen]:
        if ap.bssid in self._reported_bssids:
            return []
        key = ApClient(ap.bssid, client_mac)
        pairing_inputs = _pairing_inputs(handshake)
        if self._polled_pairing_inputs.get(key) == pairing_inputs:
            return []
        self._polled_pairing_inputs[key] = pairing_inputs
        akm_label = wpa.withheld_capture_label(handshake)
        if not akm_label:
            return []
        self._reported_bssids.add(ap.bssid)
        return [UncrackableHandshakeSeen(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel,
                                         client_mac=client_mac, akm_label=akm_label)]


class PmkidDetector:
    """A crackable PMKID, once per client."""

    def __init__(self) -> None:
        self._reported_pmkids: Set[ApClient] = set()

    def poll(self, ap: AccessPoint, client_mac: str, handshake: Handshake) -> List[PmkidCaptured]:
        key = ApClient(ap.bssid, client_mac)
        if not handshake.pmkid or key in self._reported_pmkids or not wpa.pmkid_crackable(handshake):
            return []
        self._reported_pmkids.add(key)
        return [PmkidCaptured(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel, client_mac=client_mac)]


class PbcWindowDetector:
    """A WPS Push-Button window opening; again after it closes and reopens."""

    def __init__(self) -> None:
        self._open_bssids: Set[str] = set()

    def poll(self, ap: AccessPoint) -> List[PbcWindowOpened]:
        if not ap.wps_pbc_active:
            self._open_bssids.discard(ap.bssid)
            return []
        if ap.bssid in self._open_bssids:
            return []
        self._open_bssids.add(ap.bssid)
        return [PbcWindowOpened(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel)]

    def evict_stale_open_bssids(self, present_bssids: Iterable[str]) -> None:
        """Forget evicted APs that previously had open PBC windows."""
        self._open_bssids.intersection_update(present_bssids)


class WepKeyDetector:
    """A recovered WEP key, once per AP."""

    def __init__(self) -> None:
        self._reported_bssids: Set[str] = set()

    def poll(self, ap: AccessPoint) -> List[WepKeyRecovered]:
        if not ap.wep_key or ap.bssid in self._reported_bssids:
            return []
        self._reported_bssids.add(ap.bssid)
        return [WepKeyRecovered(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel,
                                key_hex=ap.wep_key.hex())]


class WpsPinDetector:
    """A recovered WPS PIN, once per AP."""

    def __init__(self) -> None:
        self._reported_bssids: Set[str] = set()

    def poll(self, ap: AccessPoint) -> List[WpsPinRecovered]:
        if ap.wps_pin is None or ap.bssid in self._reported_bssids:
            return []
        self._reported_bssids.add(ap.bssid)
        return [WpsPinRecovered(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel, pin=ap.wps_pin)]


class WpsPskDetector:
    """A PSK recovered with the WPS PIN, once per AP."""

    def __init__(self) -> None:
        self._reported_bssids: Set[str] = set()

    def poll(self, ap: AccessPoint) -> List[WpsPskRecovered]:
        if not ap.wps_pin_psk or ap.bssid in self._reported_bssids:
            return []
        self._reported_bssids.add(ap.bssid)
        return [WpsPskRecovered(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel, psk=ap.wps_pin_psk)]


class WpsPbcPskDetector:
    """A PSK recovered with WPS Push-Button, once per AP."""

    def __init__(self) -> None:
        self._reported_bssids: Set[str] = set()

    def poll(self, ap: AccessPoint) -> List[WpsPbcPskRecovered]:
        if not ap.wps_pbc_psk or ap.bssid in self._reported_bssids:
            return []
        self._reported_bssids.add(ap.bssid)
        return [WpsPbcPskRecovered(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel,
                                   psk=ap.wps_pbc_psk)]


class CaptureEventDetector:
    """Runs every detector over one AP."""

    def __init__(self) -> None:
        self._decloaks = DecloakDetector()
        self._eapol_frames = EapolFrameDetector()
        self._handshakes = HandshakeDetector()
        self._uncrackable_handshakes = UncrackableHandshakeDetector()
        self._pmkids = PmkidDetector()
        self._pbc_windows = PbcWindowDetector()
        self._wep_keys = WepKeyDetector()
        self._wps_pins = WpsPinDetector()
        self._wps_psks = WpsPskDetector()
        self._wps_pbc_psks = WpsPbcPskDetector()

    def evict_stale_aps(self, present_bssids: Iterable[str]) -> None:
        """Forget state kept for APs no longer in the array."""
        self._pbc_windows.evict_stale_open_bssids(present_bssids)

    def poll(self, ap: AccessPoint, forged_macs: AbstractSet[str]) -> List[ApEvent]:
        events: List[ApEvent] = []
        events += self._decloaks.poll(ap)
        for client_mac, handshake in ap.handshakes.items():
            if client_mac in forged_macs:
                # Our own attacks report their outcome: their PMKID is only marked as reported.
                self._pmkids.poll(ap, client_mac, handshake)
                continue
            events += self._eapol_frames.poll(ap, client_mac, handshake)
            events += self._handshakes.poll(ap, client_mac, handshake)
            events += self._uncrackable_handshakes.poll(ap, client_mac, handshake)
            events += self._pmkids.poll(ap, client_mac, handshake)
        events += self._pbc_windows.poll(ap)
        events += self._wep_keys.poll(ap)
        events += self._wps_pins.poll(ap)
        events += self._wps_psks.poll(ap)
        events += self._wps_pbc_psks.poll(ap)
        return events
