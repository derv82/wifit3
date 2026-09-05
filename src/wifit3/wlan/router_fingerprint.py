"""Confidence-scored AP/router identity from weak OUI and stronger WPS evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, TYPE_CHECKING

from wifit3.wlan.fingerprint_vendors import VENDOR_BY_OUI

if TYPE_CHECKING:
    from wifit3.models import AccessPoint


@dataclass(frozen=True)
class RouterEvidence:
    source: str
    name: str
    value: str
    confidence: float
    passive: bool = True


@dataclass(frozen=True)
class RouterClaim:
    name: str
    value: str
    confidence: float
    evidence: tuple[RouterEvidence, ...]
    vendor: str | None = None


@dataclass(frozen=True)
class RouterFingerprint:
    label: str
    confidence: float
    vendor: str | None = None
    vendor_confidence: float = 0.0
    brand: str | None = None
    brand_confidence: float = 0.0
    model: str | None = None
    model_confidence: float = 0.0
    kind: str | None = None
    kind_confidence: float = 0.0
    claims: tuple[RouterClaim, ...] = ()
    evidence: tuple[RouterEvidence, ...] = ()


RouterRule = Callable[["AccessPoint"], Iterable[RouterClaim]]
_PREFIX_LENGTHS = (9, 7, 6)
_VENDOR_ALIASES = {
    "asus": "ASUS",
    "belkin": "Belkin",
    "dlink": "D-Link",
    "edimax": "Edimax",
    "thomson": "Thomson",
    "upvel": "Upvel",
}
_CANONICAL_VENDOR_PATTERNS = (
    (re.compile(r"\btp[-\s]?link\b", re.I), "TP-Link"),
    (re.compile(r"\bavm\b|audiovisuelles marketing", re.I), "AVM"),
    (re.compile(r"\bamv\b|amv audio", re.I), "AMV"),
    (re.compile(r"\bkaon\b", re.I), "Kaon"),
)


def _hex_mac(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").upper()


def canonical_vendor(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = name.strip()
    if not cleaned:
        return None
    for pattern, canonical in _CANONICAL_VENDOR_PATTERNS:
        if pattern.search(cleaned):
            return canonical
    return cleaned


def _vendor_for(mac: str) -> str | None:
    hex_mac = _hex_mac(mac)
    vendor = next((VENDOR_BY_OUI[hex_mac[:n]] for n in _PREFIX_LENGTHS
                   if hex_mac[:n] in VENDOR_BY_OUI), None)
    return canonical_vendor(vendor)


def _combine(confidences: Iterable[float], cap: float = 0.99) -> float:
    miss = 1.0
    for confidence in confidences:
        miss *= 1.0 - max(0.0, min(confidence, 1.0))
    return min(cap, 1.0 - miss)


def _confidence_for(claims: Iterable[RouterClaim], name: str, value: str | None) -> float:
    if value is None:
        return 0.0
    return _combine(claim.confidence for claim in claims if claim.name == name and claim.value == value)


def _best(claims: Iterable[RouterClaim], name: str) -> RouterClaim | None:
    matching = [claim for claim in claims if claim.name == name]
    return max(matching, key=lambda claim: claim.confidence, default=None)


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\x00")
    return cleaned or None


def oui_vendor_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = _vendor_for(ap.bssid)
    if vendor is None:
        return ()
    evidence = RouterEvidence("oui.vendor", "vendor", vendor, 0.30)
    return (RouterClaim("vendor", vendor, 0.30, (evidence,)),)


def router_oui_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    from wifit3.campaigns.wps.wps_router_ouis import OUI_VENDOR

    vendor = OUI_VENDOR.get(_hex_mac(ap.bssid)[:6])
    if vendor is None:
        return ()
    label = canonical_vendor(_VENDOR_ALIASES.get(vendor, vendor.title()))
    evidence = RouterEvidence("oui.router", "vendor", label, 0.45)
    return (
        RouterClaim("vendor", label, 0.45, (evidence,)),
        RouterClaim("kind", "router", 0.45, (evidence,)),
    )


def tplink_router_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    vendor = _vendor_for(ap.bssid)
    if vendor != "TP-Link":
        return ()
    evidence = RouterEvidence("oui.tplink", "vendor", vendor, 0.30)
    return (RouterClaim("kind", "router", 0.30, (evidence,)),)


def passive_wps_identity_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer = canonical_vendor(_text(getattr(ap, "wps_manufacturer", None)))
    if manufacturer is None:
        return ()
    evidence = RouterEvidence("wps.passive", "manufacturer", manufacturer, 0.99)
    return (
        RouterClaim("vendor", manufacturer, 0.99, (evidence,)),
        RouterClaim("kind", "router", 0.99, (evidence,)),
    )


def passive_wps_model_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    claims: list[RouterClaim] = []
    model = _text(getattr(ap, "wps_model_name", None)) or _text(getattr(ap, "wps_model_number", None))
    device_name = _text(getattr(ap, "wps_device_name", None))
    if model is not None:
        evidence = RouterEvidence("wps.passive", "model", model, 0.99)
        claims.append(RouterClaim("model", model, 0.99, (evidence,)))
    if device_name is not None:
        evidence = RouterEvidence("wps.passive", "device_name", device_name, 0.99)
        claims.append(RouterClaim("device_name", device_name, 0.99, (evidence,)))
    return claims


def o2_smartbox_brand_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    values = (
        _text(getattr(ap, "ssid", None)),
        _text(getattr(ap, "wps_model_name", None)),
        _text(getattr(ap, "wps_model_number", None)),
        _text(getattr(ap, "wps_device_name", None)),
    )
    matched = next((value for value in values if value and "o2smartbox" in value.lower()), None)
    if matched is None:
        return ()
    evidence = RouterEvidence("brand.o2_smartbox", "identity", matched, 0.95)
    return (
        RouterClaim("brand", "O2", 0.95, (evidence,)),
        RouterClaim("kind", "router", 0.95, (evidence,)),
    )


def celeno_vodafone_brand_rule(ap: "AccessPoint") -> Iterable[RouterClaim]:
    manufacturer = _text(getattr(ap, "wps_manufacturer", None))
    ssid = _text(getattr(ap, "ssid", None))
    if not manufacturer or not ssid:
        return ()
    if "celeno" not in manufacturer.lower() or "vodafone" not in ssid.lower():
        return ()
    evidence = RouterEvidence("brand.celeno_vodafone", "ssid", ssid, 0.70)
    return (RouterClaim("brand", "Vodafone", 0.70, (evidence,)),)


IDENTIFY_RULES: tuple[RouterRule, ...] = (
    oui_vendor_rule,
    router_oui_rule,
    tplink_router_rule,
    passive_wps_identity_rule,
    # brand rules are only used for identification, not distinction
    o2_smartbox_brand_rule, # added czech isp's i know of / found
    celeno_vodafone_brand_rule, 
)
DISTINGUISH_RULES: tuple[RouterRule, ...] = (
    passive_wps_model_rule,
)
ROUTER_RULES: tuple[RouterRule, ...] = IDENTIFY_RULES + DISTINGUISH_RULES


def fingerprint_router(
    ap: "AccessPoint",
    rules: Iterable[RouterRule] | None = None,
    identify_rules: Iterable[RouterRule] = IDENTIFY_RULES,
    distinguish_rules: Iterable[RouterRule] = DISTINGUISH_RULES,
) -> RouterFingerprint | None:
    active_rules = tuple(rules) if rules is not None else tuple(identify_rules) + tuple(distinguish_rules)
    claims = tuple(claim for rule in active_rules for claim in rule(ap))
    if rules is None:
        claims += tuple(getattr(ap, "router_claims", ()))
    if not claims:
        return None

    evidence = tuple(item for claim in claims for item in claim.evidence)
    vendor = _best(claims, "vendor")
    brand = _best(claims, "brand")
    model = _best(claims, "model")
    kind = _best(claims, "kind")

    vendor_value = vendor.value if vendor is not None else None
    brand_value = brand.value if brand is not None else None
    model_value = model.value if model is not None else None
    if vendor_value is None and model is not None and model.vendor is not None:
        vendor_value = canonical_vendor(model.vendor)
        claims += (RouterClaim("vendor", vendor_value, model.confidence, model.evidence),)
    kind_value = kind.value if kind is not None else None
    vendor_confidence = _confidence_for(claims, "vendor", vendor_value)
    brand_confidence = _confidence_for(claims, "brand", brand_value)
    model_confidence = _confidence_for(claims, "model", model_value)
    kind_confidence = _confidence_for(claims, "kind", kind_value)
    show_model = model_value is not None and model_confidence >= 0.75
    if show_model:
        identity_confidence = model_confidence
    elif brand_value is not None:
        identity_confidence = brand_confidence
    elif vendor_value is not None:
        identity_confidence = vendor_confidence
    else:
        identity_confidence = kind_confidence

    label_parts = [brand_value or vendor_value]
    if show_model:
        label_parts.append(model_value)
    if kind_value:
        label_parts.append(kind_value)
    label = " ".join(dict.fromkeys(part for part in label_parts if part))
    if identity_confidence < 0.75:
        label = f"Possible {label}"
    elif identity_confidence < 0.90:
        label = f"Likely {label}"

    return RouterFingerprint(
        label=label,
        confidence=identity_confidence,
        vendor=vendor_value,
        vendor_confidence=vendor_confidence,
        brand=brand_value,
        brand_confidence=brand_confidence,
        model=model_value,
        model_confidence=model_confidence,
        kind=kind_value,
        kind_confidence=kind_confidence,
        claims=claims,
        evidence=evidence,
    )
