from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from wifit3.dot11.wsc import messages as M


@dataclass(frozen=True)
class WpsM1Identity:
    manufacturer: Optional[str] = None
    model_name: Optional[str] = None
    model_number: Optional[str] = None
    device_name: Optional[str] = None
    primary_device_type: Optional[str] = None

    @property
    def present(self) -> bool:
        return any((
            self.manufacturer, self.model_name, self.model_number,
            self.device_name, self.primary_device_type,
        ))


def wps_text(value: bytes) -> str:
    return value.rstrip(b"\x00").decode("utf-8", "replace").strip()


def identity_from_attrs(attrs: dict[int, bytes]) -> WpsM1Identity:
    return WpsM1Identity(
        manufacturer=_text(attrs, M.ATTR_MANUFACTURER),
        model_name=_text(attrs, M.ATTR_MODEL_NAME),
        model_number=_text(attrs, M.ATTR_MODEL_NUMBER),
        device_name=_text(attrs, M.ATTR_DEV_NAME),
        primary_device_type=primary_device_type(attrs.get(M.ATTR_PRIMARY_DEV_TYPE)),
    )


_WPS_DEVICE_CATEGORIES = {
    1: "computer",
    2: "input_device",
    3: "printer",
    4: "camera",
    5: "storage",
    6: "network_infrastructure",
    7: "display",
    8: "multimedia",
    9: "gaming",
    10: "telephone",
    11: "audio",
}


def primary_device_type(value: bytes | None) -> Optional[str]:
    if value is None or len(value) < 2:
        return None
    category = int.from_bytes(value[:2], "big")
    return _WPS_DEVICE_CATEGORIES.get(category)


def _text(attrs: dict[int, bytes], attr: int) -> Optional[str]:
    raw = attrs.get(attr)
    if not raw:
        return None
    return wps_text(raw) or None
