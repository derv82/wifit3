"""RSN cipher and AKM suite numbers (00-0F-AC:N, named after hostapd's wpa_common.h) and
the parsed RSN and legacy WPA (00-50-F2 type 1) elements, and their decoders."""
from dataclasses import dataclass
from typing import Dict, List, Optional

RSN_OUI = b"\x00\x0f\xac"
WPA_OUI = b"\x00\x50\xf2"
WFA_OUI = b"\x50\x6f\x9a"

# Wi-Fi Alliance AKM selectors, all 4 bytes: the suite number alone collides with 00-0F-AC.
AKM_SELECTOR_OSEN = WFA_OUI + b"\x01"
AKM_SELECTOR_DPP = WFA_OUI + b"\x02"

AKM_802_1X = 1
AKM_PSK = 2
AKM_FT_802_1X = 3
AKM_FT_PSK = 4
AKM_802_1X_SHA256 = 5
AKM_PSK_SHA256 = 6
AKM_SAE = 8
AKM_FT_SAE = 9
AKM_802_1X_SUITE_B = 11
AKM_802_1X_SUITE_B_192 = 12
AKM_FT_802_1X_SUITE_B_192 = 13
AKM_FILS_SHA256 = 14
AKM_FILS_SHA384 = 15
AKM_FT_FILS_SHA256 = 16
AKM_FT_FILS_SHA384 = 17
AKM_OWE = 18
AKM_FT_PSK_SHA384 = 19
AKM_PSK_SHA384 = 20
AKM_FT_802_1X_SHA384 = 22
AKM_802_1X_SHA384 = 23
AKM_SAE_EXT_KEY = 24
AKM_FT_SAE_EXT_KEY = 25

CIPHER_WEP40 = 1
CIPHER_TKIP = 2
CIPHER_CCMP = 4
CIPHER_WEP104 = 5
CIPHER_AES_128_CMAC = 6
CIPHER_GCMP = 8
CIPHER_GCMP_256 = 9
CIPHER_CCMP_256 = 10
CIPHER_BIP_GMAC_256 = 12

ENTERPRISE_AKM_SUITES = frozenset({
    AKM_802_1X, AKM_FT_802_1X, AKM_802_1X_SHA256, AKM_802_1X_SUITE_B, AKM_802_1X_SUITE_B_192,
    AKM_FT_802_1X_SUITE_B_192, AKM_FILS_SHA256, AKM_FILS_SHA384, AKM_FT_FILS_SHA256,
    AKM_FT_FILS_SHA384, AKM_FT_802_1X_SHA384, AKM_802_1X_SHA384,
})
SAE_AKM_SUITES = frozenset({AKM_SAE, AKM_FT_SAE, AKM_SAE_EXT_KEY, AKM_FT_SAE_EXT_KEY})
PSK_AKM_SUITES = frozenset({
    AKM_PSK, AKM_FT_PSK, AKM_PSK_SHA256, AKM_FT_PSK_SHA384, AKM_PSK_SHA384,
})


@dataclass(frozen=True, slots=True)
class RsnElement:
    """An RSN element (802.11-2020 9.4.2.24). Suite numbers are 00-0F-AC suites only;
    ``pairwise_cipher`` names the first known pairwise cipher."""
    group_cipher_suite: Optional[int]
    pairwise_cipher: Optional[str]
    pairwise_cipher_suites: tuple[int, ...]
    pairwise_cipher_count: int
    akms: tuple[str, ...]
    akm_suites: tuple[int, ...]
    akm_suite_count: int
    vendor_akm_offered: bool  # an AKM outside 00-0F-AC
    pmf_capable: bool
    pmf_required: bool
    group_management_cipher_suite: Optional[int]


@dataclass(frozen=True, slots=True)
class WpaElement:
    """A WPA vendor element (00-50-F2 type 1). ``akm_suites`` holds the RSN suite numbers
    its AKMs equal (802.1X, PSK); ``pairwise_cipher`` names the first known pairwise cipher."""
    pairwise_cipher: Optional[str]
    akms: tuple[str, ...]
    akm_suites: tuple[int, ...]
    vendor_akm_offered: bool  # an AKM other than 00-50-F2:1 / 00-50-F2:2


_CIPHER_NAMES = {
    CIPHER_WEP40: "WEP-40",
    CIPHER_TKIP: "TKIP",
    CIPHER_CCMP: "CCMP",
    CIPHER_WEP104: "WEP-104",
    CIPHER_AES_128_CMAC: "BIP-CMAC-128",
    CIPHER_GCMP: "GCMP-128",
    CIPHER_GCMP_256: "GCMP-256",
    CIPHER_CCMP_256: "CCMP-256",
}
_AKM_NAMES = {
    AKM_802_1X: "EAP",      # 802.1X (Enterprise)
    AKM_PSK: "PSK",
    AKM_FT_802_1X: "FT-EAP",
    AKM_FT_PSK: "FT-PSK",
    AKM_802_1X_SHA256: "EAP-SHA256",
    AKM_PSK_SHA256: "PSK-SHA256",
    AKM_SAE: "SAE",      # WPA3
    AKM_FT_SAE: "FT-SAE",
    AKM_802_1X_SUITE_B: "EAP-SUITE-B",
    AKM_802_1X_SUITE_B_192: "EAP-SUITE-B-192",
    AKM_FT_802_1X_SUITE_B_192: "FT-EAP-SUITE-B-192",
    AKM_FILS_SHA256: "FILS-SHA256",
    AKM_FILS_SHA384: "FILS-SHA384",
    AKM_FT_FILS_SHA256: "FT-FILS-SHA256",
    AKM_FT_FILS_SHA384: "FT-FILS-SHA384",
    AKM_OWE: "OWE",      # Enhanced Open
    AKM_FT_PSK_SHA384: "FT-PSK-SHA384",
    AKM_PSK_SHA384: "PSK-SHA384",
    AKM_FT_802_1X_SHA384: "FT-EAP-SHA384",
    AKM_802_1X_SHA384: "EAP-SHA384",
    AKM_SAE_EXT_KEY: "SAE-EXT-KEY",      # WPA3 H2E (group-dependent hash)
    AKM_FT_SAE_EXT_KEY: "FT-SAE-EXT-KEY",
}
_VENDOR_AKM_NAMES = {
    AKM_SELECTOR_OSEN: "OSEN",
    AKM_SELECTOR_DPP: "DPP",
}

_WPA_AKM_SUITES = {1: AKM_802_1X, 2: AKM_PSK}   # 00-50-F2:N -> 00-0F-AC:N

_WPA3_DATA_CIPHERS = frozenset({CIPHER_CCMP, CIPHER_GCMP, CIPHER_GCMP_256, CIPHER_CCMP_256})
# TODO: FT-PSK family (suites 4 & 19) is "crackable" but the FT key hierarchy
#       (PMK-R0 → PMK-R1 → PTK) is more involved than plain PSK.

def _suite_name(suite: bytes, table: Dict[int, str]) -> Optional[str]:
    if len(suite) != 4 or suite[:3] != RSN_OUI:
        return None
    return table.get(suite[3])

def parse_rsn_element(tag_data: bytes) -> Optional[RsnElement]:
    """Parse the RSN IE body (tag 48 contents, sans the 2-byte header), or None if
    the IE is malformed.

    Field layout (per IEEE 802.11-2020 § 9.4.2.24):
        Version (2 B LE) | Group Cipher Suite (4 B) |
        Pairwise Suite Count (2 B LE) | Pairwise Suite List (4 B × N) |
        AKM Suite Count    (2 B LE) | AKM Suite List    (4 B × N) |
        RSN Capabilities   (2 B LE) | [optional PMKID list, GMCS, ...]
    """
    try:
        n = len(tag_data)
        # Need at least version (2) + group cipher (4) + 2 size fields (2+2) = 10.
        if n < 10:
            return None
        if int.from_bytes(tag_data[:2], "little") != 1:
            return None
        group_cipher_suite = tag_data[5] if tag_data[2:5] == RSN_OUI else None
        p = 6  # skip version + group cipher
        pairwise_count = int.from_bytes(tag_data[p:p+2], "little")
        p += 2
        if p + 4 * pairwise_count > n:
            return None
        pairwise: Optional[str] = None
        pairwise_suites: List[int] = []
        for i in range(pairwise_count):
            suite = tag_data[p:p + 4]
            name = _suite_name(suite, _CIPHER_NAMES)
            if suite[:3] == RSN_OUI:
                pairwise_suites.append(suite[3])
            # Stick with the first listed pairwise cipher (the AP's
            # preferred one). Some APs list TKIP+CCMP for compatibility;
            # CCMP is conventionally listed first.
            if pairwise is None and name is not None:
                pairwise = name
            p += 4
        if p + 2 > n:
            return None
        akm_count = int.from_bytes(tag_data[p:p+2], "little")
        p += 2
        if p + 4 * akm_count > n:
            return None
        akms: List[str] = []
        akm_suites: List[int] = []
        vendor_akm_offered = False
        for _ in range(akm_count):
            suite = tag_data[p:p + 4]
            p += 4
            if len(suite) != 4:
                continue
            if suite[:3] == RSN_OUI:
                sid = suite[3]
                if sid not in akm_suites:
                    akm_suites.append(sid)   # raw 00-0F-AC:N, drives crackability
                name = _AKM_NAMES.get(sid, f"AKM-{suite.hex()}")
            else:
                vendor_akm_offered = True
                name = _VENDOR_AKM_NAMES.get(bytes(suite), "Vendor")
            if name not in akms:
                akms.append(name)
        pmf_capable = False
        pmf_required = False
        group_management_cipher_suite: Optional[int] = None
        if p < n:
            if p + 2 > n:
                return None
            rsn_caps = int.from_bytes(tag_data[p:p+2], "little")
            pmf_capable = bool(rsn_caps & 0x0080)  # Bit 7 (MFPC)
            pmf_required = bool(rsn_caps & 0x0040)  # Bit 6 (MFPR)
            p += 2
            # Optional tail: PMKID Count (2 B) + PMKID List (16 B each) +
            # Group Management Cipher Suite. GMCS is mandatory for WPA3-Ent-192.
            if p < n:
                if p + 2 > n:
                    return None
                pmkid_count = int.from_bytes(tag_data[p:p+2], "little")
                p += 2
                pmkid_bytes = 16 * pmkid_count
                if p + pmkid_bytes > n:
                    return None
                p += pmkid_bytes
                if p < n and p + 4 > n:
                    return None
                if p + 4 <= n and tag_data[p:p+3] == RSN_OUI:
                    group_management_cipher_suite = tag_data[p+3]
        return RsnElement(
            group_cipher_suite=group_cipher_suite,
            pairwise_cipher=pairwise,
            pairwise_cipher_suites=tuple(pairwise_suites),
            pairwise_cipher_count=pairwise_count,
            akms=tuple(akms),
            akm_suites=tuple(akm_suites),
            akm_suite_count=akm_count,
            vendor_akm_offered=vendor_akm_offered,
            pmf_capable=pmf_capable,
            pmf_required=pmf_required,
            group_management_cipher_suite=group_management_cipher_suite,
        )
    except Exception:
        return None

def parse_wpa_element(tag_data: bytes) -> Optional[WpaElement]:
    """Parse a WPA vendor IE body (from its 00-50-F2 OUI and type 1 through the AKM list),
    or None if malformed or truncated."""
    n = len(tag_data)
    # OUI + type (4) + version (2) + group cipher (4) + pairwise count (2) = 12.
    if n < 12 or int.from_bytes(tag_data[4:6], "little") != 1:
        return None
    pairwise_count = int.from_bytes(tag_data[10:12], "little")
    p = 12
    if p + 4 * pairwise_count + 2 > n:
        return None
    pairwise: Optional[str] = None
    for _ in range(pairwise_count):
        suite = tag_data[p:p + 4]
        if pairwise is None and suite[:3] == WPA_OUI:
            pairwise = _CIPHER_NAMES.get(suite[3])
        p += 4
    akm_count = int.from_bytes(tag_data[p:p + 2], "little")
    p += 2
    if p + 4 * akm_count > n:
        return None
    akms: List[str] = []
    akm_suites: List[int] = []
    vendor_akm_offered = False
    for _ in range(akm_count):
        suite = tag_data[p:p + 4]
        p += 4
        akm = _WPA_AKM_SUITES.get(suite[3]) if suite[:3] == WPA_OUI else None
        if akm is None:
            vendor_akm_offered = True
            name = "Vendor"
        else:
            name = _AKM_NAMES[akm]
            if akm not in akm_suites:
                akm_suites.append(akm)
        if name not in akms:
            akms.append(name)
    return WpaElement(
        pairwise_cipher=pairwise,
        akms=tuple(akms),
        akm_suites=tuple(akm_suites),
        vendor_akm_offered=vendor_akm_offered,
    )

def classify_wpa3_enterprise(rsn: RsnElement, has_wpa: bool) -> tuple[bool, bool]:
    if has_wpa or not rsn.pmf_capable or not _enterprise_akms_only(rsn):
        return False, False

    akm_suites = set(rsn.akm_suites)
    if _wpa3_enterprise_192_security(rsn):
        if akm_suites == {AKM_802_1X_SUITE_B_192}:
            return True, False
        ft_192_akms = {AKM_802_1X_SUITE_B_192, AKM_FT_802_1X_SUITE_B_192}
        if AKM_FT_802_1X_SUITE_B_192 in akm_suites and akm_suites.issubset(ft_192_akms):
            return True, False

    if not _modern_rsn_ciphers(rsn) or AKM_802_1X_SHA256 not in akm_suites:
        return False, False
    if AKM_802_1X in akm_suites:
        return not rsn.pmf_required, not rsn.pmf_required
    return rsn.pmf_required, False

def _modern_rsn_ciphers(rsn: RsnElement) -> bool:
    return (
        rsn.group_cipher_suite in _WPA3_DATA_CIPHERS
        and rsn.pairwise_cipher_count == len(rsn.pairwise_cipher_suites)
        and bool(rsn.pairwise_cipher_suites)
        and set(rsn.pairwise_cipher_suites).issubset(_WPA3_DATA_CIPHERS)
    )

def _wpa3_enterprise_192_security(rsn: RsnElement) -> bool:
    return (
        rsn.pmf_required
        and rsn.pairwise_cipher_count == 1
        and rsn.pairwise_cipher_suites == (CIPHER_GCMP_256,)
        and rsn.group_cipher_suite == CIPHER_GCMP_256
        and rsn.group_management_cipher_suite == CIPHER_BIP_GMAC_256
    )

def _enterprise_akms_only(rsn: RsnElement) -> bool:
    return (
        rsn.akm_suite_count == len(rsn.akm_suites)
        and bool(rsn.akm_suites)
        and set(rsn.akm_suites).issubset(ENTERPRISE_AKM_SUITES)
    )
