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


@dataclass(frozen=True)
class CaptureEvent:
    kind: str  # "eapol" | "handshake_complete" | "pmkid"
    bssid: str
    client_mac: str
    ssid: Optional[str] = None
    # eapol-only
    msg_num: Optional[int] = None
    replay_hex: Optional[str] = None
    # handshake_complete-only
    pair_label: Optional[str] = None


class CaptureEventDetector:
    """Stateful event differ.

    Pass ``granular_eapol=True`` (FocusView) to also surface every new
    EAPOL frame; ``False`` (ScannerView) skips them and only emits
    completions + PMKID captures.
    """

    def __init__(self, *, granular_eapol: bool = True):
        self._granular_eapol = granular_eapol
        # Keys are (bssid, client_mac).
        self._seen_eapol: dict[Tuple[str, str], Set[Tuple[int, str]]] = {}
        self._completed: Set[Tuple[str, str]] = set()
        self._pmkid: Set[Tuple[str, str]] = set()

    def reset(self) -> None:
        """Drop all state. Useful when refocusing on a new target."""
        self._seen_eapol.clear()
        self._completed.clear()
        self._pmkid.clear()

    def poll(
        self,
        ap: AccessPoint,
        *,
        forged_macs: Set[str] = frozenset(),
    ) -> Iterator[CaptureEvent]:
        """Yield ``CaptureEvent``s for state newly observed on ``ap``."""
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
                seen = self._seen_eapol.setdefault(key, set())
                for f in hs.eapol_frames:
                    frame_key = (f.msg_num, f.replay_hex)
                    if frame_key in seen:
                        continue
                    seen.add(frame_key)
                    yield CaptureEvent(
                        kind="eapol",
                        bssid=ap.bssid,
                        client_mac=client_mac,
                        ssid=ap.ssid,
                        msg_num=f.msg_num,
                        replay_hex=f.replay_hex,
                    )

            if hs.is_complete and key not in self._completed:
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
