"""Hashcat ``-m 22000`` hashline FORMAT (WPA-PBKDF2-PMKID+EAPOL).

The disk writer is ``persist.save`` (``HcFiles``).

The PMKID (``WPA*01`` / FT ``WPA*03``) line is built here; the EAPOL (``WPA*02`` / FT
``WPA*04``) lines and the crackability decision both live in ``crack.handshake``, the
single source of truth, so a "captured" verdict and a writable hashline can never disagree.

Format spec (one line per hash):

    PROTOCOL*TYPE*PMKID_OR_MIC*MACAP*MACSTA*ESSID*ANONCE*EAPOL*MESSAGEPAIR
    FT-PSK records (03 / 04) append: *MDID*R0KHID*R1KHID

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
import re
from typing import List, Optional

from wifit3.models import Handshake
from wifit3.crack import handshake as wpa
from wifit3.crack.handshake import mac_compact, ssid_hex

PMKID_SHA256_MESSAGE_PAIR_BIT = 0x02
_PMKID_SHA256_MESSAGE_PAIR = "03"


def pmkid_hashline(ssid: str, hs: Handshake) -> Optional[str]:
    """Return a ``WPA*01*…`` (or FT-PSK ``WPA*03*…``) line for the PMKID, or None if not
    available or not crackable."""
    if not ssid or not hs.pmkid or len(hs.pmkid) != 16:
        return None
    akm = wpa.crackable_pmkid_akm(hs)
    if akm is None:
        return None
    prefix = (
        f"*{hs.pmkid.hex()}"
        f"*{mac_compact(hs.bssid)}"
        f"*{mac_compact(hs.client_mac)}"
        f"*{ssid_hex(ssid)}"
    )
    ft_frame = wpa.ft_identifiers_frame(hs) if akm == wpa.FT_PSK_AKM else None
    if ft_frame is not None:
        # hashcat uses a PMKID's MESSAGEPAIR only on WPA*01, so it stays empty here.
        return (f"WPA*03{prefix}***"
                f"*{ft_frame.mdid.hex()}*{ft_frame.r0khid.hex()}*{ft_frame.r1khid.hex()}")
    message_pair = _PMKID_SHA256_MESSAGE_PAIR if akm == wpa.PMKID_PSK_SHA256_AKM else ""
    return f"WPA*01{prefix}***{message_pair}"


def eapol_hashlines(ssid: str, hs: Handshake) -> List[str]:
    """One ``WPA*02*…`` / ``WPA*04*…`` line per distinct *crackable* handshake instance. Empty
    when the SSID is hidden or no instance is serialisable (e.g. a clipped MIC
    frame), i.e. exactly when there's nothing hashcat could crack."""
    if not ssid:
        return []
    return [wpa.hc22000_line(ssid, hs, pair) for pair in wpa.crackable_pairs(hs)]


@dataclass(frozen=True, slots=True)
class Hc22000Line:
    """Parsed Hashcat mode 22000 record (PMKID or EAPOL handshake)."""
    kind: str           # "01"/"03" (PMKID, FT PMKID) or "02"/"04" (EAPOL, FT EAPOL)
    pmkid_or_mic: str   # field 2: PMKID hex (if 01/03) or MIC (if 02/04)
    mac_ap: str         # field 3
    mac_sta: str        # field 4
    essid: str          # field 5
    anonce: str = ""    # field 6 (EAPOL only)
    eapol: str = ""     # field 7 (EAPOL only)
    message_pair: str = ""  # field 8 (EAPOL; on a PMKID, bit 0x02 marks HMAC-SHA256)
    mdid: str = ""      # field 9 (FT only)
    r0khid: str = ""    # field 10 (FT only)
    r1khid: str = ""    # field 11 (FT only)


def _is_hex(value: str, *, exact: int | None = None, maximum: int | None = None) -> bool:
    if len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", value):
        return False
    if exact is not None and len(value) != exact:
        return False
    return maximum is None or len(value) <= maximum


def parse_hc22000(line: str) -> Optional[Hc22000Line]:
    """Parse and validate one ``WPA*01``-``WPA*04`` line into an Hc22000Line, or None."""
    parts = line.strip().split("*")
    if len(parts) not in (9, 12) or parts[0] != "WPA" or parts[1] not in {"01", "02", "03", "04"}:
        return None
    kind = parts[1]
    if (kind in {"01", "02"}) != (len(parts) == 9):
        return None
    if not all((_is_hex(parts[2], exact=32), _is_hex(parts[3], exact=12),
                _is_hex(parts[4], exact=12), _is_hex(parts[5], maximum=64))):
        return None
    if kind in {"01", "03"}:
        if parts[6] or parts[7] or (parts[8] and not _is_hex(parts[8], exact=2)):
            return None
    elif not all((_is_hex(parts[6], exact=64), _is_hex(parts[7]), _is_hex(parts[8], exact=2))):
        return None
    if kind in {"03", "04"} and not all((
        _is_hex(parts[9], exact=4), 0 < len(parts[10]) <= 96, _is_hex(parts[10]),
        0 < len(parts[11]) <= 96, _is_hex(parts[11]),
    )):
        return None
    is_ft = len(parts) == 12
    return Hc22000Line(
        kind=kind,
        pmkid_or_mic=parts[2].lower(),
        mac_ap=parts[3].lower(),
        mac_sta=parts[4].lower(),
        essid=parts[5].lower(),
        anonce=parts[6].lower(),
        eapol=parts[7].lower(),
        message_pair=parts[8].lower(),
        mdid=parts[9].lower() if is_ft else "",
        r0khid=parts[10].lower() if is_ft else "",
        r1khid=parts[11].lower() if is_ft else "",
    )
