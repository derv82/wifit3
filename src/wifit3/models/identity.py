from __future__ import annotations

from enum import Enum, auto
from typing import Any, Optional


_DUMMY_STRINGS = frozenset({
    "0", "00000000", "12345", "12345678", "1.0", "n/a", "na", "none",
    "default", "unknown", "null", "undefined", "generic", "string",
})


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\x00")
    if not cleaned or cleaned.lower() in _DUMMY_STRINGS or set(cleaned) == {"?"}:
        return None
    return cleaned


class IdSource(Enum):
    """Origin of identity evidence ordered by priority."""
    WSC_M1 = 1          # Active or passive M1 cryptographic TLVs
    WINBOX_PROBE = 2    # MikroTik WinBox UDP (port 20561)
    MNDP_PROBE = 3      # MikroTik MNDP UDP (port 5678)
    UBNT_PROBE = 4      # Ubiquiti Discovery UDP (port 10001)
    WSC_BEACON = 5      # Passive Beacon/ProbeResp Tag 221 WSC element
    OUI = 6             # IEEE MAC prefix registry

    @property
    def label(self) -> str:
        return {
            IdSource.WSC_M1: "WSC M1",
            IdSource.WINBOX_PROBE: "WinBox Probe",
            IdSource.MNDP_PROBE: "MNDP Probe",
            IdSource.UBNT_PROBE: "Ubiquiti Probe",
            IdSource.WSC_BEACON: "WSC Beacon",
            IdSource.OUI: "IEEE OUI",
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

    @property
    def model(self) -> str | None:
        return self.model_name or self.model_number

    @property
    def summary(self) -> str:
        if not self.manufacturer:
            return self.model or ""
        if not self.model:
            return self.manufacturer
        if self.model.lower().startswith(self.manufacturer.lower()):
            return self.model
        return f"{self.manufacturer} {self.model}"
