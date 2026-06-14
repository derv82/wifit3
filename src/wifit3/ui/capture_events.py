"""Detect new capture events (EAPOL frames, handshake completions, PMKID
harvests) by diffing AP state across polls.

Lives between the engine and the views: engines mutate ``AccessPoint``
objects, views poll ``CaptureEventDetector`` once per UI tick and decide
how to render each event. The detector is purely structural — no Rich
markup, no logging — so ScannerView and FocusView can shape events
differently while sharing dedup state.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional, Set, Tuple

from wifit3.engine.models import AccessPoint
from wifit3.engine.wpa import handshake as wpa


# Human-readable labels for AccessPoint.decloak_method / CaptureEvent.method.
# Centralised so Scanner + Focus render the same wording (and future decloak
# sources only need to be added here).
DECLOAK_METHOD_LABELS = {
    "beacon": "Beacon Leak",
    "probe_resp": "Probe Response",
    "assoc_req": "Association Request",
}


class CaptureKind(str, Enum):
    """Kinds the detector emits. The ``str`` mixin keeps a member a plain string
    (drop-in for comparisons / dict keys / JSON); we just never rely on
    ``str(member)``, which on <3.11 renders the member name, not the value."""
    EAPOL     = "eapol"
    HANDSHAKE = "handshake_complete"
    PMKID     = "pmkid"
    DECLOAK   = "decloak"
    WEP_KEY   = "wep_key"      # recovered WEP key (hex)
    WPS_PIN   = "wps_pin"      # router's WPS PIN
    WPS_PSK   = "wps_psk"      # passphrase via WPS PIN attack
    WPS_PBC   = "wps_pbc"      # passphrase via WPS Push-Button


@dataclass(frozen=True)
class CaptureEvent:
    kind: CaptureKind
    bssid: str
    client_mac: str = ""  # empty for AP-scoped events like decloak
    ssid: Optional[str] = None
    # eapol-only
    msg_num: Optional[int] = None
    replay_hex: Optional[str] = None
    # eapol-only content descriptor (from engine.wpa.describe) — the hashcat-
    # relevant fields, so the UI can tick each (ANonce/SNonce/MIC/EAPOL) and dim
    # a frame that arrived degraded. ``useful`` = "contributes to a crackable
    # pair"; a useful frame is NOT a capture on its own (that's HANDSHAKE).
    has_nonce: Optional[bool] = None
    has_mic: Optional[bool] = None
    eapol_complete: Optional[bool] = None
    useful: Optional[bool] = None
    # M1-only: whether the handshake carries a PMKID (the KDE rides M1's key
    # data). True/False for an M1 frame, None for every other message — so the
    # renderer ticks PMKID on M1 and omits the field elsewhere.
    has_pmkid: Optional[bool] = None
    # AKM crackability of the association this frame belongs to (engine.wpa.eapol_verdict)
    #  - True  = -m 22000-crackable,
    #  - False = uncrackable (SAE / FT-PSK)
    #  - None  = not yet known (transition AP, pre-M2).
    # The renderer flags False frames "SAE ONLY" to avoid misleading the user.
    crackable: Optional[bool] = None
    # Why a frame is uncrackable: "SAE" or "FT" or None when crackable/unknown.
    akm_label: Optional[str] = None
    # handshake_complete-only
    pair_label: Optional[str] = None
    # decloak-only: "probe_resp" | "assoc_req" (future: "mbssid_ie", "beacon_leak")
    method: Optional[str] = None
    # recovered credential for WEP_KEY / WPS_* kinds (key hex / PSK / PIN); the
    # kind says which it is. None for the others.
    value: Optional[str] = None


class CaptureEventDetector:
    """Stateful event differ.

    Pass ``granular_eapol=True`` (FocusView) to also surface every new
    EAPOL frame; ``False`` (ScannerView) skips them and only emits
    completions + PMKID captures.
    """

    def __init__(self, *, granular_eapol: bool = True):
        self._granular_eapol = granular_eapol
        # Keyed (bssid, client_mac) → count of eapol frames already surfaced, so every
        # distinct frame logs (incl. M1/M3 retries). Handshakes are rare, so err verbose.
        self._seen_eapol_count: dict[Tuple[str, str], int] = {}
        # Keyed (bssid, client_mac, anonce) — one entry per captured handshake
        # instance, so a re-handshake (new ANonce) re-announces.
        self._completed: Set[Tuple[str, str, bytes]] = set()
        self._pmkid: Set[Tuple[str, str]] = set()
        # BSSIDs we've observed as hidden during this detector's lifetime.
        # Required so we only fire "decloak" on an actual None→SSID transition
        # we witnessed — not for APs that already had an SSID on first poll.
        self._seen_hidden: Set[str] = set()
        self._decloak_announced: Set[str] = set()
        # (bssid, kind) for one-shot recovered-credential events (WEP key, WPS
        # PIN/PSK/PBC) — each announces exactly once per AP.
        self._creds_announced: Set[Tuple[str, CaptureKind]] = set()

    def reset(self) -> None:
        """Drop all state. Useful when refocusing on a new target."""
        self._seen_eapol_count.clear()
        self._completed.clear()
        self._pmkid.clear()
        self._seen_hidden.clear()
        self._decloak_announced.clear()
        self._creds_announced.clear()

    def poll(
        self,
        ap: AccessPoint,
        *,
        forged_macs: Set[str] = frozenset(),
    ) -> Iterator[CaptureEvent]:
        """Yield ``CaptureEvent``s for state newly observed on ``ap``."""
        # Decloak: only announce for an AP we observed as hidden during our lifetime, so
        # entering Focus on an already-decloaked AP stays silent while Scanner (saw it from
        # the first beacon) fires once.
        if not ap.ssid or ap.ssid == "<hidden>":
            self._seen_hidden.add(ap.bssid)
        else:
            if (
                ap.bssid in self._seen_hidden
                and ap.bssid not in self._decloak_announced
            ):
                self._decloak_announced.add(ap.bssid)
                yield CaptureEvent(
                    kind=CaptureKind.DECLOAK,
                    bssid=ap.bssid,
                    ssid=ap.ssid,
                    method=ap.decloak_method,
                )

        for client_mac, hs in ap.handshakes.items():
            key = (ap.bssid, client_mac)

            # Forged MACs (our own active attacks) — record PMKID for
            # dedup but skip EAPOL frames + completion events. The
            # attack already logs its own outcome.
            if client_mac in forged_macs:
                if hs.pmkid and key not in self._pmkid:
                    self._pmkid.add(key)
                continue

            if self._granular_eapol:
                # One event per newly-seen EAPOL frame (flat per-Mx trace). Completeness is
                # reported only by the handshake_complete banner below, so "valid 4-way" fires
                # once, not on every later M1/M3 retransmit.
                seen_n = self._seen_eapol_count.get(key, 0)
                frames = hs.eapol_frames
                if len(frames) > seen_n:
                    verdict = wpa.eapol_verdict(hs)
                    crackable = None if verdict == "unknown" else verdict == "crackable"
                    akm_label = wpa.uncrackable_label(hs)
                    for f in frames[seen_n:]:
                        info = wpa.describe(f)
                        yield CaptureEvent(
                            kind=CaptureKind.EAPOL,
                            bssid=ap.bssid,
                            client_mac=client_mac,
                            ssid=ap.ssid,
                            msg_num=f.msg_num,
                            replay_hex=f.replay_hex,
                            has_nonce=info.has_nonce,
                            has_mic=info.has_mic,
                            eapol_complete=info.eapol_complete,
                            useful=info.useful,
                            # PMKID is parsed onto hs.pmkid in the same frame-
                            # ingest call that appended this frame, so it's known
                            # by the time we surface an M1. Tie it to M1 only.
                            has_pmkid=bool(hs.pmkid) if f.msg_num == 1 else None,
                            crackable=crackable,
                            akm_label=akm_label,
                        )
                    self._seen_eapol_count[key] = len(frames)

            # Completion banner — once per handshake INSTANCE (keyed by ANonce), in both
            # modes: M2/M3/M4 of one handshake share the instance (one banner), but a genuine
            # re-handshake (new ANonce) re-fires.
            for anonce, pair in hs.valid_pairs_by_instance().items():
                ikey = (ap.bssid, client_mac, anonce)
                if ikey in self._completed:
                    continue
                self._completed.add(ikey)
                yield CaptureEvent(
                    kind=CaptureKind.HANDSHAKE,
                    bssid=ap.bssid,
                    client_mac=client_mac,
                    ssid=ap.ssid,
                    pair_label=f"M{pair[0].msg_num}+M{pair[1].msg_num}",
                )

            if hs.pmkid and key not in self._pmkid and wpa.pmkid_crackable(hs):
                self._pmkid.add(key)
                yield CaptureEvent(
                    kind=CaptureKind.PMKID,
                    bssid=ap.bssid,
                    client_mac=client_mac,
                    ssid=ap.ssid,
                )

        # AP-scoped recovered-credential wins. Atomic: one value per event, its
        # meaning fixed by the kind — so a WPS PIN attack emits WPS_PIN *and*
        # WPS_PSK (two log lines), while PBC emits only WPS_PBC. Fire-once per
        # (bssid, kind) so each credential announces exactly once.
        for kind, value in (
            (CaptureKind.WEP_KEY, ap.wep_key.hex() if ap.wep_key else None),
            (CaptureKind.WPS_PIN, ap.wps_pin),
            (CaptureKind.WPS_PSK, ap.wps_pin_psk),
            (CaptureKind.WPS_PBC, ap.wps_pbc_psk),
        ):
            if value and (ap.bssid, kind) not in self._creds_announced:
                self._creds_announced.add((ap.bssid, kind))
                yield CaptureEvent(
                    kind=kind, bssid=ap.bssid, ssid=ap.ssid, value=value,
                )
