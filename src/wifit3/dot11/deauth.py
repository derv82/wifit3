"""802.11 Deauthentication / Disassociation frame builder (pure spec)."""
from typing import Optional
from wifit3.dot11.mac import mac_header
from wifit3.dot11.packet import is_group_mac

# Standard 802.11 Deauthentication / Disassociation reason codes.
REASON_CODES = {
    1: "unspecified",
    2: "prev-auth-invalid",
    3: "deauth-leaving",
    4: "disassoc-inactivity",
    5: "ap-overloaded",
    6: "class2-from-nonauth",
    7: "class3-from-nonassoc",
    8: "disassoc-leaving",
    9: "not-authenticated",
    10: "power-cap-unacceptable",
    11: "supported-channels-unacceptable",
    13: "invalid-ie",
    14: "mic-failure",
    15: "4way-timeout",
    16: "group-key-timeout",
    17: "handshake-ie-mismatch",
    18: "group-cipher-invalid",
    19: "pairwise-cipher-invalid",
    20: "akm-invalid",
    21: "unsupported-rsne-version",
    22: "invalid-rsne-caps",
    23: "802.1X-auth-failed",
    24: "cipher-suite-rejected",
    31: "pmf-policy-violation",
}


def reason_description(code: Optional[int]) -> str:
    """Human name for an 802.11 deauth/disassoc reason code."""
    if code is None:
        return "none"
    return REASON_CODES.get(code, f"unknown(0x{code:02x})")


# SIFS + a 1 Mbps long-preamble ACK (µs): the unicast-ACK NAV. Matches aireplay-ng's
# hardcoded deauth duration (0x013A); our injectors default to 1 Mbps CCK, so this is the
# time the addressed STA needs to ACK back.
_DEAUTH_ACK_NAV_US = 0x013A


def _deauth_nav_bytes(dest_mac: str) -> bytes:
    """Little-endian duration/NAV for a deauth addressed to ``dest_mac`` (addr1).

    A group-addressed (broadcast/multicast) destination is never ACKed → NAV 0; a unicast
    destination reserves the medium for the SIFS + ACK it returns. The chip does NOT fill
    this in for raw monitor-injected frames (mac80211 only computes NAV for its own managed
    TX, not ``IEEE80211_TX_CTL_INJECTED`` frames), so we set it in the frame ourselves."""
    nav = 0 if is_group_mac(dest_mac) else _DEAUTH_ACK_NAV_US
    return nav.to_bytes(2, "little")


def build_deauth(a1: bytes, a2: bytes, a3: bytes, reason: int, *,
                 disassoc: bool = False, duration: bytes = b"\x00\x00") -> bytes:
    """One 802.11 Deauth (default) / Disassoc MPDU (no FCS): FC + ``duration`` NAV + addr1/2/3
    + seq (0, HW-filled) + reason. ``duration`` is the little-endian NAV bytes (0 for a
    group-addressed / un-ACKed frame). Shared by the interface deauth path, PMKID's leaving
    deauth, and WPS's client-leaving frame."""
    subtype = 0x0A if disassoc else 0x0C          # Disassoc / Deauth (mgmt subtypes)
    return (mac_header(bytes([subtype << 4, 0x00]), a1, a2, a3, duration=duration)
            + reason.to_bytes(2, "little"))
