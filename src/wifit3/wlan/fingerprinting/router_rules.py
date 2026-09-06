"""Router/AP fingerprint evidence rules."""
from __future__ import annotations

import re
from typing import Iterable, TYPE_CHECKING

from wifit3.wlan.fingerprinting.router_types import RouterClaim, RouterEvidence, RouterRule
from wifit3.wlan.fingerprinting.router_helpers import canonical_vendor, clean_text, hex_mac, vendor_for_mac

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


def oui_vendor_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = vendor_for_mac(ap.bssid)
    if vendor is None:
        return ()
    evidence = RouterEvidence("oui.vendor", "vendor", vendor, 0.30)
    return (RouterClaim("vendor", vendor, 0.30, (evidence,)),)


def router_oui_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    from wifit3.campaigns.wps.wps_router_ouis import OUI_VENDOR

    vendor = OUI_VENDOR.get(hex_mac(ap.bssid)[:6])
    if vendor is None:
        return ()
    label = canonical_vendor(_VENDOR_ALIASES.get(vendor, vendor.title()))
    evidence = RouterEvidence("oui.router", "vendor", label, 0.45)
    return (
        RouterClaim("vendor", label, 0.45, (evidence,)),
        RouterClaim("kind", "router", 0.45, (evidence,)),
    )


def tplink_router_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = vendor_for_mac(ap.bssid)
    if vendor != "TP-Link":
        return ()
    evidence = RouterEvidence("oui.tplink", "kind", "router", 0.30)
    return (RouterClaim("kind", "router", 0.30, (evidence,)),)


def ubiquiti_router_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = vendor_for_mac(ap.bssid)
    if vendor != "Ubiquiti":
        return ()
    evidence = RouterEvidence("oui.ubiquiti", "kind", "router", 0.30)
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


def passive_wps_identity_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer, source = _wps_value_source(ap, "manufacturer")
    manufacturer = canonical_vendor(manufacturer)
    if manufacturer is None:
        return ()
    evidence = RouterEvidence(source, "manufacturer", manufacturer, 0.99)
    return (
        RouterClaim("vendor", manufacturer, 0.99, (evidence,)),
        RouterClaim("kind", "router", 0.99, (evidence,)),
    )


def passive_wps_model_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
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
    values = (
        clean_text(getattr(ap, "ssid", None)),
        clean_text(getattr(ap, "wps_model_name", None)),
        clean_text(getattr(ap, "wps_model_number", None)),
        clean_text(getattr(ap, "wps_device_name", None)),
    )
    matched = next((value for value in values if value and "o2smartbox" in value.lower()), None)
    if matched is None:
        return ()
    evidence = RouterEvidence("brand.o2_smartbox", "identity", matched, 0.95)
    return (
        RouterClaim("brand", "O2", 0.95, (evidence,)),
        RouterClaim("kind", "router", 0.95, (evidence,)),
    )


def vodafone_ssid_brand_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer = clean_text(getattr(ap, "wps_manufacturer", None))
    ssid = clean_text(getattr(ap, "ssid", None))
    if not ssid or "vodafone" not in ssid.lower():
        return ()
    if manufacturer and "celeno" in manufacturer.lower():
        return ()
    evidence = RouterEvidence("brand.vodafone_ssid", "ssid", ssid, 0.30)
    return (RouterClaim("brand", "Vodafone", 0.30, (evidence,)),)


def celeno_vodafone_brand_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer = clean_text(getattr(ap, "wps_manufacturer", None))
    ssid = clean_text(getattr(ap, "ssid", None))
    if not manufacturer or not ssid:
        return ()
    if "celeno" not in manufacturer.lower() or "vodafone" not in ssid.lower():
        return ()
    evidence = RouterEvidence("brand.vodafone", "ssid", ssid, 0.70)
    return (RouterClaim("brand", "Vodafone", 0.70, (evidence,)),)


def apple_ssid_hotspot_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    ssid = clean_text(getattr(ap, "ssid", None))
    if not ssid or not re.search(r"\b(?:iphone|ipad)\b", ssid, re.I):
        return ()
    evidence = RouterEvidence("brand.apple_ssid", "ssid", ssid, 0.40)
    return (
        RouterClaim("brand", "Apple", 0.40, (evidence,)),
        RouterClaim("kind", "hotspot", 0.40, (evidence,)),
    )


def apple_vendor_hotspot_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = canonical_vendor(clean_text(getattr(ap, "wps_manufacturer", None))) or vendor_for_mac(ap.bssid)
    if vendor != "Apple":
        return ()
    evidence = RouterEvidence("vendor.apple", "vendor", vendor, 0.85)
    return (
        RouterClaim("brand", "Apple", 0.85, (evidence,)),
        RouterClaim("kind", "hotspot", 0.85, (evidence,)),
    )


IDENTIFY_RULES: tuple[RouterRule, ...] = (
    oui_vendor_rule,
    router_oui_rule,
    tplink_router_rule,
    ubiquiti_router_rule,
    epson_printer_rule,
    epson_direct_ssid_printer_rule,
    passive_wps_identity_rule,
    # brand rules are only used for identification, not distinction
    o2_smartbox_brand_rule, # added czech isp's i know of / found
    vodafone_ssid_brand_rule,
    celeno_vodafone_brand_rule,
    apple_ssid_hotspot_rule,
    apple_vendor_hotspot_rule,
)
DISTINGUISH_RULES: tuple[RouterRule, ...] = (
    passive_wps_model_rule,
)
ROUTER_RULES: tuple[RouterRule, ...] = IDENTIFY_RULES + DISTINGUISH_RULES


