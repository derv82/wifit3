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

    @property
    def present(self) -> bool:
        return any((self.manufacturer, self.model_name, self.model_number, self.device_name))


def wps_text(value: bytes) -> str:
    return value.rstrip(b"\x00").decode("utf-8", "replace").strip()


def identity_from_attrs(attrs: dict[int, bytes]) -> WpsM1Identity:
    return WpsM1Identity(
        manufacturer=_text(attrs, M.ATTR_MANUFACTURER),
        model_name=_text(attrs, M.ATTR_MODEL_NAME),
        model_number=_text(attrs, M.ATTR_MODEL_NUMBER),
        device_name=_text(attrs, M.ATTR_DEV_NAME),
    )


def _text(attrs: dict[int, bytes], attr: int) -> Optional[str]:
    raw = attrs.get(attr)
    if not raw:
        return None
    return wps_text(raw) or None
