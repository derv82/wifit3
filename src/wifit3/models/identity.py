from __future__ import annotations

from enum import Enum, auto
import re


_DUMMY_STRINGS = frozenset({
    "0", "00000000", "12345", "123456", "12345678", "1.0", "n/a", "na", "none",
    "default", "unknown", "null", "undefined", "generic", "string",
    "wi-fi protected setup router", "wifi protected setup router", "wps router",
    "ralink wireless access point", "ralink wireless ap", "ralinkaps",
    "realtek wireless access point", "realtek wireless ap",
})

_CANONICAL_VENDOR_PATTERNS = (
    (re.compile(r"\basus(?:tek)?\b", re.I), "ASUS"),
    (re.compile(r"\bnetgear\b", re.I), "Netgear"),
    (re.compile(r"\btp[-\s]?link\b", re.I), "TP-Link"),
    (re.compile(r"\bcisco\b", re.I), "Cisco"),
    (re.compile(r"\blinksys\b", re.I), "Linksys"),
    (re.compile(r"\bd[-\s]?link\b", re.I), "D-Link"),
    (re.compile(r"\bbelkin\b", re.I), "Belkin"),
    (re.compile(r"\bkaon\b", re.I), "Kaon"),
    (re.compile(r"\b(?:mikrotik|routerboard(?:\.com)?)\b", re.I), "MikroTik"),
    (re.compile(r"\bubiquiti\b", re.I), "Ubiquiti"),
    (re.compile(r"\btechnicolor\b", re.I), "Technicolor"),
    (re.compile(r"\bavm\b|audiovisuelles marketing", re.I), "AVM"),
    (re.compile(r"\bamv\b|amv audio", re.I), "AMV"),
    (re.compile(r"\bepson\b", re.I), "Epson"),
    (re.compile(r"\bapple\b", re.I), "Apple"),
    (re.compile(r"\bralink\b", re.I), "Ralink"),
    (re.compile(r"\bnokia\b", re.I), "Nokia"),
    (re.compile(r"\b(?:hewlett[-\s]?packard|hp)\b", re.I), "HP"),
    (re.compile(r"\bcommscope\b", re.I), "CommScope"),
    (re.compile(r"\bjensen\b", re.I), "Jensen"),
)

_SILICON_ODMS = frozenset({
    "Ralink", "Realtek", "Celeno", "Broadcom", "MediaTek", "Qualcomm Atheros",
})


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\x00")
    if not cleaned or cleaned.lower() in _DUMMY_STRINGS or set(cleaned) == {"?"}:
        return None
    return cleaned


def canonical_vendor(name: str | None) -> str | None:
    cleaned = clean_text(name)
    if cleaned is None:
        return None
    for pattern, canonical in _CANONICAL_VENDOR_PATTERNS:
        if pattern.search(cleaned):
            return canonical
    return cleaned


class IdSource(Enum):
    """Origin of identity evidence ordered by priority."""
    WSC_M1 = 1          # Active or passive M1 cryptographic TLVs
    WSC_BEACON = 2      # Passive Beacon/ProbeResp Tag 221 WSC element
    OUI = 3             # IEEE MAC prefix registry

    @property
    def label(self) -> str:
        return {
            IdSource.WSC_M1: "WSC M1",
            IdSource.WSC_BEACON: "WSC Beacon",
            IdSource.OUI: "IEEE OUI",
        }[self]


class IdKey(Enum):
    """Standardized identity attribute keys."""
    MANUFACTURER = auto()
    MODEL_NAME = auto()
    MODEL_NUMBER = auto()
    DEVICE_NAME = auto()
    DEVICE_TYPE = auto()
    SERIAL_NUMBER = auto()


class ApIdentity:
    """Multi-source identity evidence store for an AccessPoint."""

    def __init__(
        self,
        source: IdSource | None = None,
        *,
        manufacturer: str | None = None,
        model_name: str | None = None,
        model_number: str | None = None,
        device_name: str | None = None,
        serial_number: str | None = None,
        device_type: str | None = None,
    ) -> None:
        self._evidence: dict[IdKey, dict[IdSource, str]] = {}
        has_attrs = any(
            v is not None
            for v in (manufacturer, model_name, model_number, device_name, serial_number, device_type)
        )
        if has_attrs:
            if source is None:
                raise ValueError("IdSource must be specified when initializing ApIdentity with attributes")
            self.update(
                source,
                manufacturer=manufacturer,
                model_name=model_name,
                model_number=model_number,
                device_name=device_name,
                serial_number=serial_number,
                device_type=device_type,
            )

    def set(self, source: IdSource, key: IdKey, value: str | None) -> None:
        """Store cleaned evidence for a given source and key."""
        cleaned = clean_text(value)
        if cleaned:
            if key is IdKey.MANUFACTURER:
                cleaned = canonical_vendor(cleaned) or cleaned
            self._evidence.setdefault(key, {})[source] = cleaned

    def update(
        self,
        source: IdSource,
        *,
        manufacturer: str | None = None,
        model_name: str | None = None,
        model_number: str | None = None,
        device_name: str | None = None,
        serial_number: str | None = None,
        device_type: str | None = None,
    ) -> None:
        """Store multiple cleaned identity attributes for a given source."""
        attrs = (
            (IdKey.MANUFACTURER, manufacturer),
            (IdKey.MODEL_NAME, model_name),
            (IdKey.MODEL_NUMBER, model_number),
            (IdKey.DEVICE_NAME, device_name),
            (IdKey.SERIAL_NUMBER, serial_number),
            (IdKey.DEVICE_TYPE, device_type),
        )
        for key, val in attrs:
            if val is not None:
                self.set(source, key, val)

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
        val, _ = self.get(IdKey.MANUFACTURER)
        if val in _SILICON_ODMS:
            oui_mfr = self.get_source_value(IdKey.MANUFACTURER, IdSource.OUI)
            if oui_mfr and oui_mfr not in _SILICON_ODMS:
                return oui_mfr
        return val

    @property
    def manufacturer_source(self) -> IdSource | None:
        val, src = self.get(IdKey.MANUFACTURER)
        if val in _SILICON_ODMS:
            oui_mfr = self.get_source_value(IdKey.MANUFACTURER, IdSource.OUI)
            if oui_mfr and oui_mfr not in _SILICON_ODMS:
                return IdSource.OUI
        return src

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
    def device_type(self) -> str | None:
        return self.get(IdKey.DEVICE_TYPE)[0]

    def _is_valid_model(self, val: str | None) -> bool:
        if not val:
            return False
        mfr = self.manufacturer
        if mfr and val.strip().lower() == mfr.strip().lower():
            return False
        return True

    @property
    def model(self) -> str | None:
        if self._is_valid_model(self.model_name):
            return self.model_name
        if self._is_valid_model(self.model_number):
            return self.model_number
        if self._is_valid_model(self.device_name):
            return self.device_name
        return None

    @property
    def model_source(self) -> IdSource | None:
        """Resolve the IdSource from which the effective model was derived."""
        if self._is_valid_model(self.model_name):
            return self.get(IdKey.MODEL_NAME)[1]
        if self._is_valid_model(self.model_number):
            return self.get(IdKey.MODEL_NUMBER)[1]
        if self._is_valid_model(self.device_name):
            return self.get(IdKey.DEVICE_NAME)[1]
        return None

    @property
    def summary(self) -> str:
        base = self._name_summary()
        device_type = self.device_type
        if device_type:
            return f"{base} ({device_type})".strip()
        return base

    def _name_summary(self) -> str:
        if not self.manufacturer:
            return self.model or ""
        if not self.model:
            return self.manufacturer
        mfr_lower = self.manufacturer.lower()
        model_lower = self.model.lower()
        if mfr_lower in model_lower or model_lower.startswith(mfr_lower.split()[0]):
            return self.model
        return f"{self.manufacturer} {self.model}"
