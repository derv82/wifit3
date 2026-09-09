from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from wifit3.dot11.wsc import messages as M

if TYPE_CHECKING:
    from wifit3.models import ApIdentity, IdSource


_WPS_DEVICE_CATEGORIES = {
    1: "computer",
    2: "input_device",
    3: "printer",
    4: "camera",
    5: "storage",
    6: None, # "network_infrastructure",
    7: "display",
    8: "multimedia",
    9: "gaming",
    10: "telephone",
    11: "audio",
}


def wps_text(value: bytes) -> str:
    return value.rstrip(b"\x00").decode("utf-8", "replace").strip()


def apply_wsc_identity(
    identity: ApIdentity,
    source: IdSource,
    attrs: dict[int, bytes],
) -> bool:
    """Extract WSC identity TLVs and apply to ApIdentity. Returns True if any identity was found."""
    mfr = _text(attrs, M.ATTR_MANUFACTURER)
    model_name = _text(attrs, M.ATTR_MODEL_NAME)
    model_number = _text(attrs, M.ATTR_MODEL_NUMBER)
    device_name = _text(attrs, M.ATTR_DEV_NAME)
    if not any((mfr, model_name, model_number, device_name)):
        return False
    device_type = device_type_label(attrs.get(M.ATTR_PRIMARY_DEV_TYPE))
    identity.update(
        source,
        manufacturer=mfr,
        model_name=model_name,
        model_number=model_number,
        device_name=device_name,
        device_type=device_type,
    )
    return True


def device_type_label(value: bytes | None) -> Optional[str]:
    """Decode a WSC Primary Device Type (0x1054) to a category label, or None."""
    if value is None or len(value) < 2:
        return None
    category = int.from_bytes(value[:2], "big")
    return _WPS_DEVICE_CATEGORIES.get(category)


def _text(attrs: dict[int, bytes], attr: int) -> Optional[str]:
    raw = attrs.get(attr)
    if not raw:
        return None
    return wps_text(raw) or None
