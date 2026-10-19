"""Hashcat ``-m 22000`` hashline FORMAT (WPA-PBKDF2-PMKID+EAPOL).

The disk writer is ``persist.save`` (``HcFiles``).

PMKID records (``WPA*01`` / ``WPA*03``) are built here; EAPOL records
(``WPA*02`` / ``WPA*04``) and their crackability decision live in
``crack.handshake``, so capture verdicts and writable hashlines agree.

Format spec (one line per hash):

    PROTOCOL*TYPE*PMKID_OR_MIC*MACAP*MACSTA*ESSID*ANONCE*EAPOL*MESSAGEPAIR
    FT records append: MDID*R0KHID*R1KHID

References:
  - Original WPA*01 / WPA*02 format, hashcat issue #1816:
    https://github.com/hashcat/hashcat/issues/1816
  - The cracker, module_22000.c, derives the EAPOL MIC algorithm from the Key
    Descriptor Version carried in regular EAPOL bytes (keyver 2 = HMAC-SHA1 /
    AKM PSK, keyver 3 = AES-CMAC / AKM PSK-SHA256). WPA*01 message-pair bit 0x02
    selects HMAC-SHA256; FT uses WPA*03/WPA*04 plus mobility identifiers:
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


def pmkid_hashline(ssid: str, hs: Handshake) -> Optional[str]:
    """Return a ``WPA*01``/``WPA*03`` PMKID line, or None when incomplete."""
    if not ssid or not hs.pmkid or len(hs.pmkid) != 16:
        return None
    if not wpa.pmkid_crackable(hs):
        return None
    kind = "03" if hs.pmkid_akm == 4 else "01"
    message_pair = "20" if kind == "03" else (
        "02" if hs.pmkid_akm in (6, 20) or hs.pmkid_key_descriptor_version == 3 else ""
    )
    line = (
        f"WPA*{kind}"
        f"*{hs.pmkid.hex()}"
        f"*{mac_compact(hs.bssid)}"
        f"*{mac_compact(hs.client_mac)}"
        f"*{ssid_hex(ssid)}"
        f"***{message_pair}"
    )
    if kind == "03":
        line += (
            f"*{hs.pmkid_mdid.hex()}"
            f"*{hs.pmkid_r0khid.hex()}"
            f"*{hs.pmkid_r1khid.hex()}"
        )
    return line


def eapol_hashlines(ssid: str, hs: Handshake) -> List[str]:
    """One ``WPA*02``/``WPA*04`` line per distinct crackable handshake instance.

    Empty means the SSID is hidden or no complete instance is serialisable.
    """
    if not ssid:
        return []
    return [wpa.hc22000_line(ssid, hs, pair) for pair in wpa.crackable_pairs(hs)]


@dataclass(frozen=True, slots=True)
class Hc22000Line:
    """Parsed Hashcat mode 22000 record (PMKID or EAPOL handshake)."""
    kind: str
    pmkid_or_mic: str
    mac_ap: str         # field 3
    mac_sta: str        # field 4
    essid: str          # field 5
    anonce: str = ""    # field 6 (EAPOL only)
    eapol: str = ""     # field 7 (EAPOL only)
    message_pair: str = ""
    mdid: str = ""
    r0khid: str = ""
    r1khid: str = ""


def _hex(value: str, *, exact: int | None = None, maximum: int | None = None) -> bool:
    if len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", value):
        return False
    if exact is not None and len(value) != exact:
        return False
    return maximum is None or len(value) <= maximum


def parse_hc22000(line: str) -> Optional[Hc22000Line]:
    """Parse and validate one Hashcat mode 22000 record."""
    parts = line.strip().split("*")
    if len(parts) not in (9, 12) or parts[0] != "WPA" or parts[1] not in {"01", "02", "03", "04"}:
        return None
    kind = parts[1]
    if (kind in {"01", "02"}) != (len(parts) == 9):
        return None
    if not all((_hex(parts[2], exact=32), _hex(parts[3], exact=12),
                _hex(parts[4], exact=12), _hex(parts[5], maximum=64))):
        return None
    if kind in {"01", "03"}:
        if parts[6] or parts[7] or (parts[8] and not _hex(parts[8], exact=2)):
            return None
    elif not all((_hex(parts[6], exact=64), _hex(parts[7]), _hex(parts[8], exact=2))):
        return None
    if kind in {"03", "04"} and not all((
        _hex(parts[9], exact=4), 0 < len(parts[10]) <= 96,
        _hex(parts[10]), 0 < len(parts[11]) <= 96, _hex(parts[11])
    )):
        return None
    return Hc22000Line(
        kind=kind,
        pmkid_or_mic=parts[2].lower(),
        mac_ap=parts[3].lower(),
        mac_sta=parts[4].lower(),
        essid=parts[5].lower(),
        anonce=parts[6].lower(),
        eapol=parts[7].lower(),
        message_pair=parts[8].lower(),
        mdid=parts[9].lower() if len(parts) == 12 else "",
        r0khid=parts[10].lower() if len(parts) == 12 else "",
        r1khid=parts[11].lower() if len(parts) == 12 else "",
    )
