from __future__ import annotations

from enum import Enum, auto
from typing import Any, Optional


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\x00")
    return cleaned or None


class IdSource(Enum):
    """Origin of identity evidence ordered by priority."""
    WSC_M1 = 1       # Active M1 exchange (authenticated/cryptographic TLVs)
    PROBE = 2        # Active Layer-3 UDP probe (WinBox, MNDP, UBNT)
    WSC_BEACON = 3   # Passive Beacon/ProbeResp Tag 221 WSC element
    SSID = 4         # Advertised SSID heuristics
    OUI = 5          # IEEE MAC prefix registry

    @property
    def label(self) -> str:
        return {
            IdSource.WSC_M1: "wps.m1",
            IdSource.PROBE: "probe",
            IdSource.WSC_BEACON: "wps.passive",
            IdSource.SSID: "ssid",
            IdSource.OUI: "oui",
        }[self]


class IdKey(Enum):
    """Standardized identity attribute keys."""
    MANUFACTURER = auto()
    MODEL_NAME = auto()
    MODEL_NUMBER = auto()
    DEVICE_NAME = auto()
    SERIAL_NUMBER = auto()


class ApIdentity:
    """Multi-source identity evidence store for an AccessPoint."""

    def __init__(
        self,
        *,
        manufacturer: str | None = None,
        model_name: str | None = None,
        model_number: str | None = None,
        device_name: str | None = None,
        serial_number: str | None = None,
        claims: tuple[Any, ...] = (),
    ) -> None:
        self._evidence: dict[IdKey, dict[IdSource, str]] = {}
        self.claims: tuple[Any, ...] = claims
        self.fingerprint: Optional[Any] = None

        if manufacturer:
            self.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, manufacturer)
        if model_name:
            self.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, model_name)
        if model_number:
            self.set(IdSource.WSC_BEACON, IdKey.MODEL_NUMBER, model_number)
        if device_name:
            self.set(IdSource.WSC_BEACON, IdKey.DEVICE_NAME, device_name)
        if serial_number:
            self.set(IdSource.WSC_BEACON, IdKey.SERIAL_NUMBER, serial_number)

    def set(self, source: IdSource, key: IdKey, value: str | None) -> None:
        """Store cleaned evidence for a given source and key."""
        cleaned = clean_text(value)
        if cleaned:
            self._evidence.setdefault(key, {})[source] = cleaned

    def get(self, key: IdKey) -> tuple[str | None, IdSource | None]:
        """Resolve highest priority value and its source for a key."""
        sources = self._evidence.get(key)
        if not sources:
            return None, None
        for src in IdSource:
            if src in sources:
                return sources[src], src
        return None, None

    def get_source_value(self, key: IdKey, source: IdSource) -> str | None:
        """Retrieve the value for a specific key and source."""
        return self._evidence.get(key, {}).get(source)

    @property
    def manufacturer(self) -> str | None:
        return self.get(IdKey.MANUFACTURER)[0]

    @property
    def model_name(self) -> str | None:
        return self.get(IdKey.MODEL_NAME)[0]

    @property
    def model_number(self) -> str | None:
        return self.get(IdKey.MODEL_NUMBER)[0]

    @property
    def device_name(self) -> str | None:
        return self.get(IdKey.DEVICE_NAME)[0]

    @property
    def serial_number(self) -> str | None:
        return self.get(IdKey.SERIAL_NUMBER)[0]
