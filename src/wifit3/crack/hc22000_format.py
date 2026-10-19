"""Hashcat ``-m 22000`` hashline FORMAT (WPA-PBKDF2-PMKID+EAPOL).

The disk writer is ``persist.save`` (``HcFiles``).

The PMKID (``WPA*01``) line is built here; the EAPOL (``WPA*02``) lines and the
crackability decision both live in ``crack.handshake``, the single source
of truth, so a "captured" verdict and a writable hashline can never disagree.

Format spec (one line per hash):

    PROTOCOL*TYPE*PMKID_OR_MIC*MACAP*MACSTA*ESSID*ANONCE*EAPOL*MESSAGEPAIR

References:
  - Format (WPA*01 / WPA*02 fields), hashcat issue #1816:
    https://github.com/hashcat/hashcat/issues/1816
  - The cracker, module_22000.c, derives the EAPOL MIC algorithm from the Key
    Descriptor Version carried in the embedded EAPOL bytes (keyver 2 = HMAC-SHA1 /
    AKM PSK, keyver 3 = AES-CMAC / AKM PSK-SHA256), so the negotiated AKM needs no
    field of its own. A PMKID (WPA*01) is HMAC-SHA1 unless bit 0x02 of its MESSAGEPAIR
    is set (written ``03``, as hcxpcapngtool does): then HMAC-SHA256 (AKM PSK-SHA256):
    https://github.com/hashcat/hashcat/blob/master/src/modules/module_22000.c
  - hashcat WPA/WPA2 wiki: https://hashcat.net/wiki/doku.php?id=cracking_wpawpa2
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from wifit3.models import Handshake
from wifit3.crack import handshake as wpa
from wifit3.crack.handshake import mac_compact, ssid_hex

PMKID_SHA256_MESSAGE_PAIR_BIT = 0x02
_PMKID_SHA256_MESSAGE_PAIR = "03"


def pmkid_hashline(ssid: str, hs: Handshake) -> Optional[str]:
    """Return a ``WPA*01*…`` line for the PMKID, or None if not available or not
    crackable."""
    if not ssid or not hs.pmkid or len(hs.pmkid) != 16:
        return None
    akm = wpa.crackable_pmkid_akm(hs)
    if akm is None:
        return None
    message_pair = _PMKID_SHA256_MESSAGE_PAIR if akm == wpa.PMKID_PSK_SHA256_AKM else ""
    return (
        "WPA*01"
        f"*{hs.pmkid.hex()}"
        f"*{mac_compact(hs.bssid)}"
        f"*{mac_compact(hs.client_mac)}"
        f"*{ssid_hex(ssid)}"
        f"***{message_pair}"
    )


def eapol_hashlines(ssid: str, hs: Handshake) -> List[str]:
    """One ``WPA*02*…`` line per distinct *crackable* handshake instance. Empty
    when the SSID is hidden or no instance is serialisable (e.g. a clipped MIC
    frame), i.e. exactly when there's nothing hashcat could crack."""
    if not ssid:
        return []
    return [wpa.hc22000_line(ssid, hs, pair) for pair in wpa.crackable_pairs(hs)]


@dataclass(frozen=True, slots=True)
class Hc22000Line:
    """Parsed Hashcat mode 22000 record (PMKID or EAPOL handshake)."""
    kind: str           # "01" (PMKID) or "02" (EAPOL)
    pmkid_or_mic: str   # field 2: PMKID hex (if 01) or MIC (if 02)
    mac_ap: str         # field 3
    mac_sta: str        # field 4
    essid: str          # field 5
    anonce: str = ""    # field 6 (EAPOL only)
    eapol: str = ""     # field 7 (EAPOL only)
    message_pair: str = ""  # field 8 (EAPOL; on a PMKID, bit 0x02 marks HMAC-SHA256)


def parse_hc22000(line: str) -> Optional[Hc22000Line]:
    """Parse one ``WPA*01*…`` or ``WPA*02*…`` line into an Hc22000Line."""
    parts = line.strip().split("*")
    if len(parts) < 6 or parts[0] != "WPA":
        return None
    return Hc22000Line(
        kind=parts[1],
        pmkid_or_mic=parts[2].lower(),
        mac_ap=parts[3].lower(),
        mac_sta=parts[4].lower(),
        essid=parts[5],
        anonce=parts[6].lower() if len(parts) > 6 else "",
        eapol=parts[7] if len(parts) > 7 else "",
        message_pair=parts[8] if len(parts) > 8 else "",
    )
