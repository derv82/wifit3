"""Hashcat ``-m 22000`` hashline writer (WPA-PBKDF2-PMKID+EAPOL).

Replaces the external ``hcxpcapngtool`` dependency for the common case
of one AP + one or more clients with valid 4-way handshakes or a PMKID.

Format spec (one line per hash):

    PROTOCOL*TYPE*PMKID_OR_MIC*MACAP*MACSTA*ESSID*ANONCE*EAPOL*MESSAGEPAIR

- Type ``01`` = PMKID:   ``WPA*01*<pmkid>*<ap>*<sta>*<essid>***``
- Type ``02`` = EAPOL:   ``WPA*02*<mic>*<ap>*<sta>*<essid>*<anonce>*<eapol>*<pair>``

The EAPOL field is the 802.1X+key-descriptor bytes of the MIC-carrying
frame (M2 / M3 / M4) with the MIC field zeroed — hashcat re-computes
MIC candidates against the saved value.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from wifit3.engine.models import AccessPoint, EapolFrame, Handshake

# Message-pair byte values (hashcat semantics — see hashcat/src/modules/module_22000.c).
# Encodes which two M-frames formed the pair AND which of the two supplies the
# MIC frame (EAPOL field of the hashline).
_PAIR_M1M2_FROM_M2 = 0x00
_PAIR_M1M4_FROM_M4 = 0x01
_PAIR_M2M3_FROM_M2 = 0x02
_PAIR_M2M3_FROM_M3 = 0x03
_PAIR_M3M4_FROM_M3 = 0x04
_PAIR_M3M4_FROM_M4 = 0x05

# MIC offset within an EAPOL payload (i.e. starting at the 802.1X version byte).
_MIC_OFFSET = 81
_MIC_LEN = 16


def _mac_compact(mac: str) -> str:
    """``aa:bb:cc:dd:ee:ff`` -> ``aabbccddeeff`` (hashcat's MAC encoding)."""
    return mac.replace(":", "").replace("-", "").lower()


def _ssid_hex(ssid: str) -> str:
    """SSID -> UTF-8 hex. Hashcat consumes the raw bytes as the AP advertised them.

    Anything that wouldn't UTF-8-encode cleanly (very rare) is replaced with
    U+FFFD; in practice every consumer-router SSID is ASCII or plain UTF-8.
    """
    return ssid.encode("utf-8", errors="replace").hex()


def pmkid_hashline(ssid: str, hs: Handshake) -> Optional[str]:
    """Return a ``WPA*01*…`` line for the PMKID, or None if not available."""
    if not ssid or not hs.pmkid:
        return None
    if len(hs.pmkid) != 16:
        return None
    return (
        "WPA*01"
        f"*{hs.pmkid.hex()}"
        f"*{_mac_compact(hs.bssid)}"
        f"*{_mac_compact(hs.client_mac)}"
        f"*{_ssid_hex(ssid)}"
        "***"
    )


def _pick_pair(
    pair: Tuple[EapolFrame, EapolFrame],
) -> Optional[Tuple[EapolFrame, EapolFrame, int]]:
    """Select ANonce-providing frame, MIC-providing frame, and pair byte.

    Returns (anonce_frame, mic_frame, pair_byte) or None if the pair shape
    isn't one we know how to emit.
    """
    a, b = pair
    kinds = (a.msg_num, b.msg_num)
    if kinds == (1, 2):
        return (a, b, _PAIR_M1M2_FROM_M2)  # ANonce from M1, MIC from M2
    if kinds == (2, 3):
        # M3 carries an ANonce too; M2 has the SNonce + a clean MIC.
        # Prefer EAPOL from M2 (more reliably matches PMK derivation).
        return (b, a, _PAIR_M2M3_FROM_M2)  # ANonce from M3, MIC frame = M2
    if kinds == (3, 4):
        return (a, b, _PAIR_M3M4_FROM_M4)  # ANonce from M3, MIC from M4
    if kinds == (1, 4):
        return (a, b, _PAIR_M1M4_FROM_M4)  # ANonce from M1, MIC from M4
    return None


def eapol_hashline(ssid: str, hs: Handshake) -> Optional[str]:
    """Return a ``WPA*02*…`` line for the 4-way handshake, or None if not valid.

    None when:
      - no valid M-pair captured (delegates to ``Handshake.find_valid_pair``)
      - the MIC-bearing frame is missing its 802.1X payload (truncated capture)
      - the SSID is unknown (hidden network — hashcat can't compute the PMK)
    """
    if not ssid:
        return None
    pair = hs.find_valid_pair()
    if pair is None:
        return None
    selected = _pick_pair(pair)
    if selected is None:
        return None
    anonce_frame, mic_frame, pair_byte = selected
    if not mic_frame.eapol_payload or len(mic_frame.eapol_payload) < _MIC_OFFSET + _MIC_LEN:
        return None
    if len(anonce_frame.nonce) != 32 or len(mic_frame.mic) != 16:
        return None

    # Zero the MIC field within the EAPOL payload before hex-encoding.
    payload = bytearray(mic_frame.eapol_payload)
    payload[_MIC_OFFSET: _MIC_OFFSET + _MIC_LEN] = b"\x00" * _MIC_LEN

    return (
        "WPA*02"
        f"*{mic_frame.mic.hex()}"
        f"*{_mac_compact(hs.bssid)}"
        f"*{_mac_compact(hs.client_mac)}"
        f"*{_ssid_hex(ssid)}"
        f"*{anonce_frame.nonce.hex()}"
        f"*{bytes(payload).hex()}"
        f"*{pair_byte:02x}"
    )


def format_ap_hashlines(ap: AccessPoint) -> List[str]:
    """Collect every hashline we can produce for this AP across all clients."""
    if not ap.ssid:
        return []  # hidden network → can't fill ESSID field
    lines: List[str] = []
    for hs in ap.handshakes.values():
        line = pmkid_hashline(ap.ssid, hs)
        if line:
            lines.append(line)
        line = eapol_hashline(ap.ssid, hs)
        if line:
            lines.append(line)
    return lines


def write_hc22000(path: Path, ap: AccessPoint) -> int:
    """Write all hashlines for *ap* to *path*. Returns the count written.

    If no hashlines could be produced, no file is created and 0 is returned.
    """
    lines = format_ap_hashlines(ap)
    if not lines:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)
