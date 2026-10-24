"""Render AccessPoint security info as Rich-markup for the UI."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from wifit3.crack.handshake import PSK_CAPTURE_AKMS
from wifit3.models import AccessPoint
from wifit3.models.access_point import eap_only


# Actionability palette: color signals what's worth targeting:
#   bright_green = attackable today (we have an attack)
#   yellow       = interesting but no attack yet (PRs welcome)
#   red          = out of scope (we don't support cracking this protocol)
#   dim (muted)  = no attack needed (OPEN networks)
_ATTACKABLE = "bright_green"
_NO_ATTACK_YET = "yellow"
_OUT_OF_SCOPE = "red"


def psk_capture_supported(ap: AccessPoint) -> bool | None:
    """Whether a current AKM yields a supported PSK capture; None without AKM evidence."""
    if ap.akm_suites:
        return bool(set(ap.akm_suites) & PSK_CAPTURE_AKMS)
    if ap.akms:
        return False                  # vendor-OUI AKMs only
    encryption = (ap.encryption or "").upper()
    if encryption == "WPA" or encryption.startswith("WPA-"):
        return False
    if encryption.startswith("WPA3") or encryption == "OWE":
        return False
    return None


class EncryptionType(Enum):
    """One security bucket per AP; the single classification the ENCRYPT column and
    the scan filter both read, so the two can never disagree."""
    OPEN = "OPEN"
    WEP = "WEP"
    WPA1 = "WPA"
    WPA2 = "WPA2"
    WPA3_TRANSITION = "WPA3_TRANSITION"
    WPA3 = "WPA3"
    OWE = "OWE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_ap(cls, ap: AccessPoint) -> "EncryptionType":
        """Bucket an AP, RSN evidence winning over the legacy ``encryption`` string."""
        if ap.wpa3 and ap.transition_mode:
            return cls.WPA3_TRANSITION
        if ap.wpa3:
            return cls.WPA3
        if "OWE" in ap.akms:
            return cls.OWE
        enc = (ap.encryption or "").upper()
        if enc == "WPA" or enc.startswith("WPA-"):
            return cls.WPA1
        if ap.akms:
            return cls.WPA2
        if enc in ("", "OPEN", "UNKNOWN"):
            return cls.OPEN
        if enc == "WEP":
            return cls.WEP
        return cls.UNKNOWN


def _format_iv_count(n: int) -> str:
    """Compact IV count for the ENCRYPT cell: 1234 → '1.2k', 12345 → '12.3k'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def _simplified_akms(akms: List[str]) -> str:
    """Compact AKM token string for the Scanner view's ENCRYPT column."""
    parts: List[str] = []
    represented: set[str] = set()
    for name, tok in (
        ("PSK", "PSK"), ("PSK-SHA256", "PSK256"), ("PSK-SHA384", "PSK384"),
        ("FT-PSK", "FT-PSK"), ("FT-PSK-SHA384", "FT-PSK384"),
        ("EAP", "EAP"), ("EAP-SHA256", "EAP256"), ("FT-EAP", "FT-EAP"),
        ("EAP-SUITE-B", "SUITE-B"), ("EAP-SUITE-B-192", "SUITE-B-192"),
        ("FT-EAP-SUITE-B-192", "FT-SUITE-B-192"), ("FT-EAP-SHA384", "FT-EAP384"),
    ):
        if name in akms:
            parts.append(tok)
            represented.add(name)
    if any("SAE" in a for a in akms):          # SAE / FT-SAE / SAE-EXT-KEY / …
        parts.append("SAE")
    if "OWE" in akms:
        parts.append("OWE")
        represented.add("OWE")
    parts.extend(
        name for name in akms
        if name not in represented and "SAE" not in name
    )
    if not parts:
        # Fall back to whatever the parser gave us (unknown / vendor AKMs).
        return "/".join(akms) if akms else ""
    return "/".join(parts)


def _detail_markup(
    akms_tok: str,
    cipher: Optional[str],
    show_cipher: bool,
    muted: str = "dim",
) -> str:
    """Build the muted-parens detail suffix, e.g. ` [dim](PSK)[/dim]`."""
    inner_parts: List[str] = []
    if akms_tok:
        inner_parts.append(akms_tok)
    if show_cipher and cipher:
        inner_parts.append(cipher)
    if not inner_parts:
        return ""
    return f" [{muted}]({'·'.join(inner_parts)})[/{muted}]"


@dataclass(frozen=True, slots=True)
class EncryptionSummary:
    """The plain facts behind the ENCRYPT cell, so a table row can be diffed
    without building markup first."""
    type: EncryptionType
    akms: str
    cipher: Optional[str]
    wep_ivs: int
    raw: str
    eap_only: bool = False
    psk_attackable: bool = False

    @classmethod
    def from_ap(cls, ap: AccessPoint) -> "EncryptionSummary":
        return cls(
            type=EncryptionType.from_ap(ap),
            akms=_simplified_akms(ap.akms),
            cipher=ap.pairwise_cipher,
            wep_ivs=ap.wep.unique_ivs if ap.wep else 0,
            raw=(ap.encryption or "").upper(),
            eap_only=eap_only(ap),
            psk_attackable=psk_capture_supported(ap) is True,
        )

    def markup(self, detailed: bool = False, muted: str = "dim") -> str:
        """Rich-markup for the ENCRYPT cell.
        ``detailed=False`` (scanner) drops the pairwise cipher entirely."""
        show_cipher = detailed and self.cipher is not None
        detail = _detail_markup(self.akms, self.cipher, show_cipher, muted)

        # WPA3 transition: the "2" tail is green only when a crackable PSK is on offer;
        # the "WPA3" head is red for SAE (out of scope), yellow for 802.1X (no attack yet).
        if self.type is EncryptionType.WPA3_TRANSITION:
            tail = _ATTACKABLE if self.psk_attackable else _NO_ATTACK_YET
            head_color = _NO_ATTACK_YET if self.eap_only else "bright_red"
            head = f"[{head_color}]WPA3[/{head_color}]→[{tail}]2[/{tail}]"
            return head if not detailed else head + detail

        # Pure WPA3. SAE is out of scope; 802.1X has no attack yet.
        if self.type is EncryptionType.WPA3:
            color = _NO_ATTACK_YET if self.eap_only else "bright_red"
            return f"[{color}]WPA3[/{color}]" + detail

        # OWE (Enhanced Open): no attack yet.
        if self.type is EncryptionType.OWE:
            return f"[{_NO_ATTACK_YET}]OWE[/{_NO_ATTACK_YET}]"

        # RSN WPA2. A crackable PSK is attackable; 802.1X and the SHA384-only PSK
        # suites have no passphrase attack yet.
        if self.type is EncryptionType.WPA2:
            color = _ATTACKABLE if self.psk_attackable else _NO_ATTACK_YET
            return f"[{color}]WPA2[/{color}]" + detail

        if self.type is EncryptionType.OPEN:
            return f"[{muted}]OPEN[/{muted}]"

        if self.type is EncryptionType.WEP:
            # Attackable now (IV capture → replay → crack).
            head = f"[{_ATTACKABLE}]WEP[/{_ATTACKABLE}]"
            if detailed:
                return head
            return head + f"[{muted}]·{_format_iv_count(self.wep_ivs)} IVs[/{muted}]"

        if self.type is EncryptionType.WPA1:
            # Legacy WPA1 vendor IE; no AKMs means it was malformed. Out of scope for wifit3.
            head = f"[{_OUT_OF_SCOPE}]WPA[/{_OUT_OF_SCOPE}]"
            if self.akms:
                return head + detail
            tail = f" [{muted}](PSK·TKIP)[/{muted}]" if detailed else f" [{muted}](PSK)[/{muted}]"
            return head + tail

        # Unknown: show raw string muted.
        return f"[{muted}]{self.raw}[/{muted}]"


def format_encryption_markup(ap: AccessPoint, detailed: bool = False, muted: str = "dim") -> str:
    """Return Rich-markup for the ENCRYPT cell."""
    return EncryptionSummary.from_ap(ap).markup(detailed=detailed, muted=muted)


def wep_key_ascii(key_hex: str) -> str:
    """A recovered WEP key as ``<hex> = "<ascii>"``."""
    try:
        kb = bytes.fromhex(key_hex)
    except ValueError:
        return key_hex
    if kb and all(0x20 <= b < 0x7F for b in kb):
        return f'{key_hex} = "{kb.decode("ascii")}"'
    return key_hex
