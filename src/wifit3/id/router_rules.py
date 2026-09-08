"""Router/AP fingerprint evidence rules."""
from __future__ import annotations

import re
from typing import Iterable, TYPE_CHECKING

from .router_types import RouterClaim, RouterEvidence, RouterRule
from .router_helpers import canonical_vendor, clean_text, vendor_for_mac

if TYPE_CHECKING:
    from wifit3.models import AccessPoint


_VENDOR_ALIASES = {
    "asus": "ASUS",
    "belkin": "Belkin",
    "dlink": "D-Link",
    "edimax": "Edimax",
    "thomson": "Thomson",
    "upvel": "Upvel",
}

# Rules for rules:
# - Rules should never pass 100% as there is always a chance of misidentification.
# - SSID rules should be 30% as they can be changed by the user.
# - OUI rules should be 30% as they depend on MAC address that can be randomized
# - WPS rules should be 99% as the information is provided by the AP itself.
# - Brand rules should depend on how reliable the information is, for example,
#   if the brand is in the SSID it should be 30% as it can be changed by the user,
#   if the brand is in the WPS information it should be 99% as it is provided by the router itself.

def oui_vendor_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = vendor_for_mac(ap.bssid)
    if vendor is None:
        return ()
    evidence = RouterEvidence("oui.vendor", "vendor", vendor, 0.30)
    return (RouterClaim("vendor", vendor, 0.30, (evidence,)),)



def tplink_router_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = vendor_for_mac(ap.bssid)
    if vendor != "TP-Link":
        return ()
    evidence = RouterEvidence("oui.tplink", "kind", "router", 0.30)
    return (RouterClaim("kind", "router", 0.30, (evidence,)),)


def mikrotik_routerboard_oui_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = vendor_for_mac(ap.bssid)
    if vendor != "MikroTik":
        return ()
    evidence = RouterEvidence("oui.mikrotik", "kind", "router", 0.30)
    return (RouterClaim("kind", "router", 0.30, (evidence,)),)


def epson_printer_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = vendor_for_mac(ap.bssid)
    if vendor != "Epson":
        return ()
    evidence = RouterEvidence("oui.epson", "kind", "printer", 0.90)
    return (RouterClaim("kind", "printer", 0.90, (evidence,)),)


def epson_direct_ssid_printer_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    ssid = clean_text(getattr(ap, "ssid", None))
    if not ssid or not re.search(r"^direct-.+-epson\b", ssid, re.I):
        return ()
    evidence = RouterEvidence("ssid.epson", "ssid", ssid, 0.30)
    return (
        RouterClaim("vendor", "Epson", 0.30, (evidence,)),
        RouterClaim("kind", "printer", 0.30, (evidence,)),
    )


def wps_manufacturer_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer, source = _wps_value_source(ap, "manufacturer")
    manufacturer = canonical_vendor(manufacturer)
    if manufacturer is None:
        return ()
    evidence = RouterEvidence(source, "manufacturer", manufacturer, 0.99)
    return (RouterClaim("vendor", manufacturer, 0.99, (evidence,)),)


def wps_model_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    claims: list[RouterClaim] = []
    model, model_source = _wps_value_source(ap, "model_name")
    if model is None:
        model, model_source = _wps_value_source(ap, "model_number")
    device_name, device_source = _wps_value_source(ap, "device_name")
    if model is not None:
        evidence = RouterEvidence(model_source, "model", model, 0.99)
        claims.append(RouterClaim("model", model, 0.99, (evidence,)))
    if device_name is not None:
        evidence = RouterEvidence(device_source, "device_name", device_name, 0.99)
        claims.append(RouterClaim("device_name", device_name, 0.99, (evidence,)))
    return claims


def _wps_value_source(ap: "AccessPoint", name: str) -> tuple[str | None, str]:
    m1_value = clean_text(getattr(ap, f"wps_m1_{name}", None))
    if m1_value is not None:
        return m1_value, "wps.m1"
    return clean_text(getattr(ap, f"wps_{name}", None)), "wps.passive"


def o2_smartbox_brand_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    model, source = _wps_value_source(ap, "model_name")
    if model is None:
        model, source = _wps_value_source(ap, "model_number")
    if not model or "o2smartbox" not in model.lower():
        return ()
    evidence = RouterEvidence(source, "model", model, 0.99)
    return (
        RouterClaim("brand", "O2", 0.99, (evidence,)),
        RouterClaim("kind", "router", 0.99, (evidence,)),
    )


def vodafone_brand_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer, manufacturer_source = _wps_value_source(ap, "manufacturer")
    ssid = clean_text(getattr(ap, "ssid", None))
    if not ssid or "vodafone" not in ssid.lower():
        return ()
    ssid_evidence = RouterEvidence("ssid.vodafone", "ssid", ssid, 0.30)
    if manufacturer and "celeno" in manufacturer.lower():
        manufacturer_evidence = RouterEvidence(manufacturer_source, "manufacturer", manufacturer, 0.70)
        # TODO: find a reliable physical-device check for Celeno CL2400 Vodafone routers/extenders.
        return (RouterClaim("brand", "Vodafone", 0.70, (ssid_evidence, manufacturer_evidence)),)
    return (RouterClaim("brand", "Vodafone", 0.30, (ssid_evidence,)),)


def apple_ssid_hotspot_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    ssid = clean_text(getattr(ap, "ssid", None))
    if not ssid or not re.search(r"\b(?:iphone|ipad)\b", ssid, re.I):
        return ()
    evidence = RouterEvidence("ssid.apple", "ssid", ssid, 0.40)
    return (
        RouterClaim("brand", "Apple", 0.40, (evidence,)),
        RouterClaim("kind", "hotspot", 0.40, (evidence,)),
    )


def apple_vendor_hotspot_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer, source = _wps_value_source(ap, "manufacturer")
    vendor = canonical_vendor(manufacturer) or vendor_for_mac(ap.bssid)
    if vendor != "Apple":
        return ()
    # TODO: split Apple OUI and WPS manufacturer confidence once this rule has real-world captures.
    evidence = RouterEvidence(source if manufacturer else "vendor.apple", "vendor", vendor, 0.85)
    return (
        RouterClaim("brand", "Apple", 0.85, (evidence,)),
        RouterClaim("kind", "hotspot", 0.85, (evidence,)),
    )


IDENTIFY_RULES: tuple[RouterRule, ...] = (
    oui_vendor_rule,
    tplink_router_rule,
    mikrotik_routerboard_oui_rule,
    epson_printer_rule,
    epson_direct_ssid_printer_rule,
    wps_manufacturer_rule,
    # brand rules are only used for identification, not distinction
    o2_smartbox_brand_rule,  # added czech isp's i know of / found
    vodafone_brand_rule,
    apple_ssid_hotspot_rule,
    apple_vendor_hotspot_rule,
)
DISTINGUISH_RULES: tuple[RouterRule, ...] = (
    wps_model_rule,
)
ROUTER_RULES: tuple[RouterRule, ...] = IDENTIFY_RULES + DISTINGUISH_RULES
