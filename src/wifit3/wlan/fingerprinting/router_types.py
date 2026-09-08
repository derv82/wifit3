from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, TYPE_CHECKING

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
class RouterConflict:
    name: str
    claims: tuple[RouterClaim, ...]


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
    wifi_generation: int | None = None
    wifi_generation_confidence: float = 0.0
    spoof_suspected: bool = False
    conflicts: tuple[RouterConflict, ...] = ()
    claims: tuple[RouterClaim, ...] = ()
    evidence: tuple[RouterEvidence, ...] = ()


RouterRule = Callable[["AccessPoint"], Iterable[RouterClaim]]
