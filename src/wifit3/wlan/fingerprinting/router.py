"""Confidence-scored AP/router identity from weak OUI and stronger WPS evidence."""
from __future__ import annotations

from typing import Iterable, TYPE_CHECKING

from wifit3.wlan.fingerprinting.router_helpers import canonical_vendor, combine_confidences
from wifit3.wlan.fingerprinting.router_rules import DISTINGUISH_RULES, IDENTIFY_RULES
from wifit3.wlan.fingerprinting.router_types import (
    RouterClaim,
    RouterConflict,
    RouterEvidence as RouterEvidence,
    RouterFingerprint,
    RouterRule,
)

if TYPE_CHECKING:
    from wifit3.models import AccessPoint


_STRONG_CONFLICT_THRESHOLD = 0.90
_CONFLICT_FIELDS = ("vendor", "brand", "model", "kind")


def _confidence_for(claims: Iterable[RouterClaim], name: str, value: str | None) -> float:
    if value is None:
        return 0.0
    return combine_confidences(claim.confidence for claim in claims if claim.name == name and claim.value == value)


def _best(claims: Iterable[RouterClaim], name: str) -> RouterClaim | None:
    matching = [claim for claim in claims if claim.name == name]
    return max(matching, key=lambda claim: claim.confidence, default=None)


def _strong_conflicts(claims: Iterable[RouterClaim]) -> tuple[RouterConflict, ...]:
    conflicts: list[RouterConflict] = []
    for name in _CONFLICT_FIELDS:
        strong = tuple(
            claim for claim in claims
            if claim.name == name and claim.confidence >= _STRONG_CONFLICT_THRESHOLD
        )
        if len({claim.value for claim in strong}) > 1:
            conflicts.append(RouterConflict(name, strong))
    return tuple(conflicts)


def _evidence_for(claims: Iterable[RouterClaim]) -> tuple[RouterEvidence, ...]:
    seen: set[RouterEvidence] = set()
    evidence: list[RouterEvidence] = []
    for claim in claims:
        for item in claim.evidence:
            if item in seen:
                continue
            seen.add(item)
            evidence.append(item)
    return tuple(evidence)


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

    vendor = _best(claims, "vendor")
    brand = _best(claims, "brand")
    model = _best(claims, "model")
    kind = _best(claims, "kind")
    wifi_generation = _best(claims, "wifi_generation")

    vendor_value = vendor.value if vendor is not None else None
    brand_value = brand.value if brand is not None else None
    model_value = model.value if model is not None else None
    if vendor_value is None and model is not None and model.vendor is not None:
        vendor_value = canonical_vendor(model.vendor)
        claims += (RouterClaim("vendor", vendor_value, model.confidence, model.evidence),)
    kind_value = kind.value if kind is not None else None
    wifi_generation_value = int(wifi_generation.value) if wifi_generation is not None else None
    vendor_confidence = _confidence_for(claims, "vendor", vendor_value)
    brand_confidence = _confidence_for(claims, "brand", brand_value)
    model_confidence = _confidence_for(claims, "model", model_value)
    kind_confidence = _confidence_for(claims, "kind", kind_value)
    wifi_generation_confidence = _confidence_for(
        claims, "wifi_generation", str(wifi_generation_value) if wifi_generation_value is not None else None
    )
    show_model = model_value is not None and model_confidence >= 0.75
    if show_model:
        identity_confidence = model_confidence
    elif brand_value is not None:
        identity_confidence = brand_confidence
    elif vendor_value is not None:
        identity_confidence = vendor_confidence
    else:
        identity_confidence = kind_confidence

    conflicts = _strong_conflicts(claims)
    evidence = _evidence_for(claims)
    if not any((brand_value, vendor_value, show_model, kind_value)):
        return None

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
        wifi_generation=wifi_generation_value,
        wifi_generation_confidence=wifi_generation_confidence,
        spoof_suspected=bool(conflicts),
        conflicts=conflicts,
        claims=claims,
        evidence=evidence,
    )
