"""RSN cipher and AKM suite numbers (00-0F-AC:N, named after hostapd's wpa_common.h) and
the parsed RSN and legacy WPA (00-50-F2 type 1) elements."""
from dataclasses import dataclass
from typing import Optional

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
