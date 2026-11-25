"""802.11 information-element builders and decoders + the shared RSN helper (pure spec).

Consolidates the SSID / rates / RSN / DS-param IE assembly that the auth, assoc, and
probe frame builders each used to hand-roll, plus the client-side RSN rewrite the PMKID
attack uses.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import List, Optional

from wifit3.dot11.rsn import (
    AKM_PSK,
    CIPHER_AES_128_CMAC,
    PSK_AKM_SUITES,
    RSN_OUI,
    SAE_AKM_SUITES,
    WPA_OUI,
    RsnElement,
    WpaElement,
    classify_wpa3_enterprise,
    parse_rsn_element,
    parse_wpa_element,
)
from wifit3.dot11.wsc.element import WpsElement, parse_wps_element


def iter_information_elements(data: bytes, start: int = 0) -> Iterator[tuple[int, bytes, bytes]]:
    """Walk 802.11 Information Elements (1B tag, 1B len), yielding (tag_id, body, raw)."""
    i, n = start, len(data)
    while i + 2 <= n:
        tag_id = data[i]
        length = data[i + 1]
        end = i + 2 + length
        if end > n:
            break
        yield tag_id, data[i + 2 : end], data[i : end]
        i = end

# Supported / Extended supported rate menus (APs only spot-check that they parse).
SUPPORTED_RATES = bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
EXT_SUPPORTED_RATES = bytes([0x30, 0x48, 0x60, 0x6C])
SUPPORTED_RATES_5GHZ = bytes([0x8C, 0x12, 0x98, 0x24, 0xB0, 0x48, 0x60, 0x6C])

# Generic WPA2-PSK-CCMP RSN IE (tag 48): version 1, CCMP group + pairwise, single
# AKM = PSK, no PMF. The client-side fallback when the AP's own IE is unusable.
GENERIC_RSN_IE = bytes.fromhex("30140100000fac040100000fac040100000fac020000")

_RSN_CAP_MFPC = 0x0080          # RSN caps MFPC bit (bit 7)
_BIP_CMAC_128 = RSN_OUI + bytes([CIPHER_AES_128_CMAC])


SSID_MAX_OCTETS = 32


def ssid_ie(ssid_bytes: bytes) -> bytes:
    """SSID IE (tag 0), truncated to the 32-byte spec maximum."""
    s = ssid_bytes[:SSID_MAX_OCTETS]
    return bytes([0x00, len(s)]) + s


def ssid_text(ssid_bytes: bytes) -> str:
    """The SSID as printable text: UTF-8 with undecodable bytes dropped and each non-printable character
    as \\xNN of its UTF-8 bytes, or \\xNN per byte when nothing decodes."""
    text = ssid_bytes.decode("utf-8", errors="ignore")
    if not text:
        return "".join(f"\\x{byte:02x}" for byte in ssid_bytes)
    characters = []
    for character in text:
        if character.isprintable():
            characters.append(character)
        else:
            characters.append("".join(f"\\x{byte:02x}" for byte in character.encode("utf-8")))
    return "".join(characters)


def rates_ie(channel: int = 1) -> bytes:
    """Supported Rates IE (tag 1), band-aware (OFDM on 5 GHz, CCK+OFDM on 2.4 GHz)."""
    rates = SUPPORTED_RATES_5GHZ if channel > 14 else SUPPORTED_RATES
    return bytes([0x01, len(rates)]) + rates


def ext_rates_ie(channel: int = 1) -> bytes:
    """Extended Supported Rates IE (tag 50), omitted on 5 GHz where all rates fit tag 1."""
    if channel > 14:
        return b""
    return bytes([0x32, len(EXT_SUPPORTED_RATES)]) + EXT_SUPPORTED_RATES


def ht_cap_ie() -> bytes:
    """HT Capabilities IE (tag 45): 20 MHz-only, 1-stream MCS 0-7, static SMPS."""
    return bytes([0x2D, 0x1A]) + b"\x2d\x01\x1b" + b"\xff" + (b"\x00" * 15) + (b"\x00" * 7)


def ds_param_ie(channel: int) -> bytes:
    """DS Parameter Set IE (tag 3): the operating channel."""
    return bytes([0x03, 0x01, channel & 0xFF])


def csa_ie(new_channel: int, *, mode: int = 1, count: int = 0) -> bytes:
    """Channel Switch Announcement IE (tag 37): mode (1 = halt TX until the switch), target channel, count."""
    return bytes([0x25, 0x03, mode & 0xFF, new_channel & 0xFF, count & 0xFF])


def ecsa_ie(new_channel: int, *, operating_class: int, mode: int = 1, count: int = 0) -> bytes:
    """Extended Channel Switch Announcement IE (tag 60): mode, new operating class, target channel, count."""
    return bytes([0x3C, 0x04, mode & 0xFF, operating_class & 0xFF, new_channel & 0xFF, count & 0xFF])


def secondary_channel_offset_ie(offset: int = 0) -> bytes:
    """Secondary Channel Offset IE (tag 62): 0 = SCN (20 MHz), 1 = above, 3 = below."""
    return bytes([0x3E, 0x01, offset & 0xFF])


def force_psk_akm(rsn_ie: bytes, akm: int = AKM_PSK, *, pmf_capable: bool = False) -> Optional[bytes]:
    """Rewrite an RSN IE to a single ``00-0F-AC:akm`` AKM (PSK by default) over the
    AP's ciphers, authoring a client RSN tail that mirrors the AP's PMF posture:
    ``pmf_capable`` → MFPC=1 (MFPR=0) + BIP group-mgmt (a transition AP often only
    associates PMF-capable STAs); else clean 0x0000 caps. Drops the AP's PMKID list
    either way; returns None if the IE is malformed (caller falls back to generic).

    Selecting one PSK AKM (not echoing the AP's full list, which claims SAE and gets
    us ignored) runs the PSK 4-way → PMKID in M1. RSNE body layout: version(2)
    group(4) pw_count(2) pw(4*n) akm_count(2) akm(4*m) [caps(2)] [pmkid_count(2)
    pmkid...] [group-mgmt(4)]."""
    if len(rsn_ie) < 2 or rsn_ie[0] != 0x30:
        return None
    body = rsn_ie[2:2 + rsn_ie[1]]
    if len(body) < 8:
        return None
    pw_count = int.from_bytes(body[6:8], "little")
    akm_off = 8 + 4 * pw_count
    if akm_off + 2 > len(body):
        return None
    akm_count = int.from_bytes(body[akm_off:akm_off + 2], "little")
    akm_end = akm_off + 2 + 4 * akm_count
    if akm_end > len(body):
        return None
    new_akm = b"\x01\x00" + RSN_OUI + bytes([akm])        # count=1 + 00-0F-AC:akm
    if pmf_capable:                                       # MFPC=1, PMKID-count 0, BIP
        tail = _RSN_CAP_MFPC.to_bytes(2, "little") + b"\x00\x00" + _BIP_CMAC_128
    else:
        tail = b"\x00\x00"                               # clean caps, no MFP
    new_body = body[:akm_off] + new_akm + tail
    return bytes([0x30, len(new_body)]) + new_body


# Vendor IE OUIs a chipset's driver adds. Epigram (00:90:4C, Broadcom's) is left out:
# hostapd adds its 2.4 GHz VHT IE on any chipset.
CHIPSET_BY_VENDOR_IE_OUI = {
    bytes.fromhex("001018"): "Broadcom",
    bytes.fromhex("000c43"): "MediaTek",
    bytes.fromhex("000ce7"): "MediaTek",
    bytes.fromhex("00037f"): "Qualcomm",
    bytes.fromhex("8cfdf0"): "Qualcomm",
    bytes.fromhex("00e04c"): "Realtek",
}


def rsn_akm_selection(data: bytes, start: int = 0) -> tuple[Optional[int], bool]:
    """The first RSN IE's AKM when it lists exactly one 00-0F-AC AKM, and whether an RSN IE
    (complete, truncated or malformed) is present without one."""
    end = start
    for tag_id, val, raw in iter_information_elements(data, start=start):
        if tag_id == 48:  # RSN IE (element id 48)
            rsn = parse_rsn_element(val)
            if rsn is not None and rsn.akm_suite_count == 1 and len(rsn.akm_suites) == 1:
                return rsn.akm_suites[0], False
            return None, True
        end += len(raw)
    return None, end < len(data) and data[end] == 48


@dataclass(frozen=True, slots=True)
class Elements:
    """A frame's Information Elements, decoded."""
    ssid: Optional[str]
    ssid_bytes: Optional[bytes]
    channel: Optional[int]
    encryption: str
    rsn: Optional[RsnElement]
    rsn_element_raw: Optional[bytes]
    wpa: Optional[WpaElement]
    wps: Optional[WpsElement]
    chipset: Optional[str]
    beacon_protection: bool
    wpa3: bool
    transition_mode: bool

    @property
    def pairwise_cipher(self) -> Optional[str]:
        if self.rsn is not None:
            return self.rsn.pairwise_cipher
        if self.wpa is not None:
            return self.wpa.pairwise_cipher
        return None

    @property
    def akms(self) -> list[str]:
        if self.rsn is not None:
            return list(self.rsn.akms)
        if self.wpa is not None:
            return list(self.wpa.akms)
        return []

    @property
    def akm_suites(self) -> list[int]:
        if self.rsn is not None:
            return list(self.rsn.akm_suites)
        if self.wpa is not None:
            return list(self.wpa.akm_suites)
        return []

    @property
    def vendor_akm_offered(self) -> bool:
        if self.rsn is not None:
            return self.rsn.vendor_akm_offered
        if self.wpa is not None:
            return self.wpa.vendor_akm_offered
        return False

    @property
    def pmf_capable(self) -> bool:
        return self.rsn is not None and self.rsn.pmf_capable

    @property
    def pmf_required(self) -> bool:
        return self.rsn is not None and self.rsn.pmf_required


def parse_elements(elements: bytes, privacy: bool) -> Elements:
    """Decode a beacon's, probe response's or (re)association request's Information Elements.
    ``privacy``: the Capability field's Privacy bit."""
    ssid: Optional[str] = None
    ssid_bytes: Optional[bytes] = None
    has_wpa = False
    has_rsn = False
    has_wpa3 = False
    transition_mode = False
    beacon_protection = False
    rsn_element: Optional[RsnElement] = None
    rsn_element_raw: Optional[bytes] = None
    wpa_element: Optional[WpaElement] = None
    chipset: Optional[str] = None
    channel_ds: Optional[int] = None
    channel_ht: Optional[int] = None
    channel_vht: Optional[int] = None

    # Per 802.11 the SSID IE is mandatory and FIRST. A later tag_id=0 is a malformed
    # frame or the walker straying into trailing bytes (unstripped metadata, padding),
    # so honor only the first occurrence.
    seen_ssid = False
    wps_payloads: List[bytes] = []

    for tag_id, tag_data, raw_elem in iter_information_elements(elements):
        if tag_id == 0 and not seen_ssid: # SSID (only the first)
            seen_ssid = True
            if len(tag_data) <= 32 and not tag_data.strip(b"\x00"):   # hidden: empty or NUL-filled, no text
                ssid_bytes = tag_data
            elif len(tag_data) <= 32:
                ssid = ssid_text(tag_data)
                ssid_bytes = tag_data
        elif tag_id == 3: # DS Parameter Set (Channel)
            if len(tag_data) == 1:
                channel_ds = tag_data[0]
        elif tag_id == 61: # HT Operation: primary channel = first byte
            if len(tag_data) >= 1:
                channel_ht = tag_data[0]
        elif tag_id == 192: # VHT Operation: center freq seg 0 at byte 1
            if len(tag_data) >= 2:
                channel_vht = tag_data[1]
        elif tag_id == 127: # Extended Capabilities: bit 84 = Beacon Protection Enabled
            if len(tag_data) >= 11:
                beacon_protection = bool(tag_data[10] & 0x10)   # bit 84 = octet 10, bit 4
        elif tag_id == 48: # RSN (WPA2/WPA3)
            has_rsn = True
            rsn = parse_rsn_element(tag_data)
            if rsn is not None:
                rsn_element_raw = bytes(raw_elem)
                rsn_element = rsn
                # SAE-family => WPA3; SAE + a PSK-family suite => transition.
                # Suite-number based so WPA3-H2E (SAE-EXT-KEY, 24) is caught.
                has_wpa3 = bool(SAE_AKM_SUITES.intersection(rsn.akm_suites))
                transition_mode = has_wpa3 and bool(
                    PSK_AKM_SUITES.intersection(rsn.akm_suites)
                )
            elif rsn_element is None and rsn_element_raw is None:
                rsn_element_raw = bytes(raw_elem)
        elif tag_id == 221: # Vendor Specific
            if len(tag_data) >= 4:
                oui = tag_data[:3]
                oui_type = tag_data[3]
                if oui == WPA_OUI:
                    if oui_type == 1: # WPA
                        has_wpa = True
                        if wpa_element is None:
                            wpa_element = parse_wpa_element(tag_data)
                    elif oui_type == 4: # WPS
                        # tag_data = OUI(3) + type(1) + WPS TLVs.
                        wps_payloads.append(tag_data[4:])
                elif oui in CHIPSET_BY_VENDOR_IE_OUI and chipset is None:
                    chipset = CHIPSET_BY_VENDOR_IE_OUI[oui]

    wps_element: Optional[WpsElement] = None
    if wps_payloads:
        wps_element = parse_wps_element(b"".join(wps_payloads))

    if rsn_element is None and wpa_element is not None:
        has_rsn = False

    if rsn_element is not None:
        wpa3_enterprise, enterprise_transition = classify_wpa3_enterprise(
            rsn_element, has_wpa
        )
        has_wpa3 = has_wpa3 or wpa3_enterprise
        transition_mode = transition_mode or enterprise_transition

    # Channel preference: DS Param (tag 3, 2.4 GHz authoritative) → HT Op (tag 61, the
    # only cross-band source; 5 GHz often omits DS per 802.11-2020 9.4.2.3) → VHT Op
    # (tag 192, last resort). Caller falls back to its tuned channel if none present.
    channel: Optional[int] = None
    if channel_ds is not None:
        channel = channel_ds
    elif channel_ht is not None:
        channel = channel_ht
    elif channel_vht is not None:
        channel = channel_vht

    akms: List[str] = []
    pairwise_cipher: Optional[str] = None
    if rsn_element is not None:
        akms = list(rsn_element.akms)
        pairwise_cipher = rsn_element.pairwise_cipher
    elif wpa_element is not None:
        akms = list(wpa_element.akms)
        pairwise_cipher = wpa_element.pairwise_cipher
    encryption = encryption_label(
        privacy=privacy,
        has_rsn=has_rsn,
        has_wpa=has_wpa,
        has_wpa3=has_wpa3,
        transition_mode=transition_mode,
        akms=akms,
        pairwise_cipher=pairwise_cipher,
    )
    return Elements(
        ssid=ssid,
        ssid_bytes=ssid_bytes,
        channel=channel,
        encryption=encryption,
        rsn=rsn_element,
        rsn_element_raw=rsn_element_raw,
        wpa=wpa_element,
        wps=wps_element,
        chipset=chipset,
        beacon_protection=beacon_protection,
        wpa3=has_wpa3,
        transition_mode=transition_mode,
    )


def encryption_label(
    *,
    privacy: bool,
    has_rsn: bool,
    has_wpa: bool,
    has_wpa3: bool,
    transition_mode: bool,
    akms: List[str],
    pairwise_cipher: Optional[str],
) -> str:
    """Build an airodump-style encryption label. ``privacy``: the Capability field's Privacy bit.

    Examples:
        "WPA2-PSK-CCMP"
        "WPA3-SAE-CCMP"
        "WPA2/WPA3-PSK+SAE-CCMP"   (transition mode)
        "WPA2-EAP-CCMP"
        "WPA-EAP-TKIP"             (legacy WPA1 vendor IE only)
        "OPEN" / "WEP"
    """
    if has_rsn:
        # Every SAE-family name (SAE, FT-SAE, SAE-EXT-KEY, FT-SAE-EXT-KEY)
        # contains "SAE", so this also catches WPA3-H2E, matching the
        # suite-number `wpa3` flag so label and flag never disagree.
        has_sae = any("SAE" in a for a in akms)
        has_psk = any("PSK" in a for a in akms)
        enterprise_akms = [
            a for a in akms if a.startswith("EAP") or a.startswith("FT-EAP")
        ]
        has_owe = "OWE" in akms

        if transition_mode:
            wpa_tag = "WPA2/WPA3"
        elif has_wpa3:
            wpa_tag = "WPA3"
        elif has_owe:
            wpa_tag = "OWE"
        else:
            wpa_tag = "WPA2"

        if has_sae and has_psk:
            akm_tag = "PSK+SAE"
        elif has_sae:
            akm_tag = "SAE"
        elif has_owe:
            akm_tag = None
        elif enterprise_akms and has_psk:
            akm_tag = "PSK+" + "+".join(enterprise_akms)
        elif enterprise_akms:
            akm_tag = "+".join(enterprise_akms)
        elif has_psk:
            akm_tag = "PSK"
        else:
            # Unknown AKM(s): fall back to listing them.
            akm_tag = "+".join(akms) if akms else None

        parts = [wpa_tag]
        if akm_tag:
            parts.append(akm_tag)
        if pairwise_cipher:
            parts.append(pairwise_cipher)
        return "-".join(parts)

    if has_wpa:
        if not akms:
            return "WPA-PSK-TKIP"   # malformed WPA1 vendor IE
        parts = ["WPA", "+".join(akms)]
        if pairwise_cipher:
            parts.append(pairwise_cipher)
        return "-".join(parts)

    if privacy:
        return "WEP"
    return "OPEN"
