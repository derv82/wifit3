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
from typing import Iterator, Optional, Set, Tuple

from wifit3.engine.models import AccessPoint


# Human-readable labels for AccessPoint.decloak_method / CaptureEvent.method.
# Centralised so Scanner + Focus render the same wording (and future decloak
# sources only need to be added here).
DECLOAK_METHOD_LABELS = {
    "beacon": "Beacon Leak",
    "probe_resp": "Probe Response",
    "assoc_req": "Association Request",
}


@dataclass(frozen=True)
class CaptureEvent:
    kind: str  # "eapol" | "handshake_complete" | "pmkid" | "decloak"
    bssid: str
    client_mac: str = ""  # empty for AP-scoped events like decloak
    ssid: Optional[str] = None
    # eapol-only
    msg_num: Optional[int] = None
    replay_hex: Optional[str] = None
    # handshake_complete-only
    pair_label: Optional[str] = None
    # decloak-only: "probe_resp" | "assoc_req" (future: "mbssid_ie", "beacon_leak")
    method: Optional[str] = None


class CaptureEventDetector:
    """Stateful event differ.

    Pass ``granular_eapol=True`` (FocusView) to also surface every new
    EAPOL frame; ``False`` (ScannerView) skips them and only emits
    completions + PMKID captures.
    """

    def __init__(self, *, granular_eapol: bool = True):
        self._granular_eapol = granular_eapol
        # Keys are (bssid, client_mac). Count of eapol frames already surfaced,
        # NOT keyed by (msg, replay) — so every distinct frame the radio
        # delivers logs (incl. M1/M3 retries), matching the [Mx] file-log
        # trace. Handshakes are rare + central to WPA2 attacks, so err verbose.
        self._seen_eapol_count: dict[Tuple[str, str], int] = {}
        self._completed: Set[Tuple[str, str]] = set()
        self._pmkid: Set[Tuple[str, str]] = set()
        # BSSIDs we've observed as hidden during this detector's lifetime.
        # Required so we only fire "decloak" on an actual None→SSID transition
        # we witnessed — not for APs that already had an SSID on first poll.
        self._seen_hidden: Set[str] = set()
        self._decloak_announced: Set[str] = set()

    def reset(self) -> None:
        """Drop all state. Useful when refocusing on a new target."""
        self._seen_eapol_count.clear()
        self._completed.clear()
        self._pmkid.clear()
        self._seen_hidden.clear()
        self._decloak_announced.clear()

    def poll(
        self,
        ap: AccessPoint,
        *,
        forged_macs: Set[str] = frozenset(),
    ) -> Iterator[CaptureEvent]:
        """Yield ``CaptureEvent``s for state newly observed on ``ap``."""
        # Decloak detection: we must have *observed* this AP as hidden during
        # our lifetime before we can announce its decloak. Entering Focus on
        # an already-decloaked AP therefore stays silent (old news), while
        # Scanner — which saw it from the first beacon — fires once.
        if not ap.ssid or ap.ssid == "<hidden>":
            self._seen_hidden.add(ap.bssid)
        else:
            if (
                ap.bssid in self._seen_hidden
                and ap.bssid not in self._decloak_announced
            ):
                self._decloak_announced.add(ap.bssid)
                yield CaptureEvent(
                    kind="decloak",
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
                seen_n = self._seen_eapol_count.get(key, 0)
                frames = hs.eapol_frames
                if len(frames) > seen_n:
                    # Completeness is a property of the whole handshake, not one
                    # frame — compute it once and tag each new frame so the UI
                    # can render "full" vs "partial" as each arrives.
                    pair = hs.find_valid_pair() if hs.beacon_frame else None
                    pair_label = (
                        f"M{pair[0].msg_num}+M{pair[1].msg_num}" if pair else None
                    )
                    for f in frames[seen_n:]:
                        yield CaptureEvent(
                            kind="eapol",
                            bssid=ap.bssid,
                            client_mac=client_mac,
                            ssid=ap.ssid,
                            msg_num=f.msg_num,
                            replay_hex=f.replay_hex,
                            pair_label=pair_label,
                        )
                    self._seen_eapol_count[key] = len(frames)

            # Standalone completion event only when we're NOT surfacing every
            # frame (Scanner). In granular mode (Focus) the eapol line above
            # carries the "full handshake" label on the completing frame, so a
            # separate event would double-log.
            if (
                not self._granular_eapol
                and hs.is_complete
                and key not in self._completed
            ):
                self._completed.add(key)
                pair = hs.find_valid_pair()
                pair_label = (
                    f"M{pair[0].msg_num}+M{pair[1].msg_num}" if pair else "?"
                )
                yield CaptureEvent(
                    kind="handshake_complete",
                    bssid=ap.bssid,
                    client_mac=client_mac,
                    ssid=ap.ssid,
                    pair_label=pair_label,
                )

            if hs.pmkid and key not in self._pmkid:
                self._pmkid.add(key)
                yield CaptureEvent(
                    kind="pmkid",
                    bssid=ap.bssid,
                    client_mac=client_mac,
                    ssid=ap.ssid,
                )
