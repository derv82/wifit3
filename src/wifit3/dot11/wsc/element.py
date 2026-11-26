"""Decoding of the WPS vendor element (221, 00-50-F2 type 4) that beacons and probe responses carry."""
from dataclasses import dataclass
from typing import Optional

from wifit3.dot11.wsc.identity import device_type_label, wps_text
from wifit3.dot11.wsc.messages import (
    iter_wsc_tlvs,
    ATTR_AP_SETUP_LOCKED,
    ATTR_CONFIG_METHODS,
    ATTR_MANUFACTURER,
    ATTR_MODEL_NAME,
    ATTR_MODEL_NUMBER,
    ATTR_PRIMARY_DEV_TYPE,
    ATTR_SELECTED_REGISTRAR,
    ATTR_VENDOR_EXTENSION,
    ATTR_VERSION,
    ATTR_WPS_STATE as ATTR_STATE,
    ATTR_DEV_PASSWORD_ID as ATTR_DEVICE_PASSWORD_ID,
    ATTR_DEV_NAME as ATTR_DEVICE_NAME,
)


def _wps_version2(vext: bytes) -> Optional[int]:
    """Pull the WPS 2.0 Version2 value from a WPS Vendor Extension
    attribute. The value is the WFA vendor id (00:37:2A) followed by
    1-byte-id / 1-byte-len subelements; Version2 is subelement 0x00.
    """
    if len(vext) < 3 or vext[:3] != b"\x00\x37\x2a":
        return None
    j = 3
    while j + 2 <= len(vext):
        sub_id = vext[j]
        sub_len = vext[j + 1]
        j += 2
        if j + sub_len > len(vext):
            break
        if sub_id == 0x00 and sub_len >= 1:   # Version2
            return vext[j]
        j += sub_len
    return None


@dataclass(frozen=True, slots=True)
class WpsElement:
    """The WPS vendor element's attributes wifit3 reads; missing attributes keep these defaults."""
    locked: bool = False
    state: Optional[int] = None            # 1 = unconfigured, 2 = configured
    version: Optional[str] = None
    config_methods: int = 0
    device_password_id: Optional[int] = None
    selected_registrar: bool = False
    manufacturer: Optional[str] = None
    model_name: Optional[str] = None
    model_number: Optional[str] = None
    device_name: Optional[str] = None
    device_type: Optional[str] = None


def parse_wps_element(data: bytes) -> WpsElement:
    """Walk the WPS IE's nested big-endian TLVs (``data`` = bytes after
    the OUI + OUI-type) and surface the attacker-relevant subset.

    Each TLV is a 2-byte attribute id, 2-byte length, then value.
    """
    locked = False
    state: Optional[int] = None
    config_methods = 0
    device_password_id: Optional[int] = None
    selected_registrar = False
    manufacturer: Optional[str] = None
    model_name: Optional[str] = None
    model_number: Optional[str] = None
    device_name: Optional[str] = None
    device_type: Optional[str] = None
    version1 = False
    version2 = 0
    for attr, val in iter_wsc_tlvs(data):
        ln = len(val)
        if attr == ATTR_AP_SETUP_LOCKED and ln >= 1:
            locked = val[0] == 0x01
        elif attr == ATTR_STATE and ln >= 1:
            state = val[0]
        elif attr == ATTR_CONFIG_METHODS and ln >= 2:
            config_methods = (val[0] << 8) | val[1]
        elif attr == ATTR_DEVICE_PASSWORD_ID and ln >= 2:
            device_password_id = (val[0] << 8) | val[1]
        elif attr == ATTR_SELECTED_REGISTRAR and ln >= 1:
            selected_registrar = val[0] == 0x01
        elif attr == ATTR_MANUFACTURER:
            text = wps_text(val)
            if text:
                manufacturer = text
        elif attr == ATTR_MODEL_NAME:
            text = wps_text(val)
            if text:
                model_name = text
        elif attr == ATTR_MODEL_NUMBER:
            text = wps_text(val)
            if text:
                model_number = text
        elif attr == ATTR_DEVICE_NAME:
            text = wps_text(val)
            if text:
                device_name = text
        elif attr == ATTR_PRIMARY_DEV_TYPE:
            label = device_type_label(val)
            if label:
                device_type = label
        elif attr == ATTR_VERSION and ln >= 1:
            version1 = True
        elif attr == ATTR_VENDOR_EXTENSION:
            v2 = _wps_version2(val)
            if v2 is not None:
                version2 = v2
    version: Optional[str] = None
    if version2 >= 0x20:
        version = "2.0"
    elif version1:
        version = "1.0"
    return WpsElement(
        locked=locked,
        state=state,
        version=version,
        config_methods=config_methods,
        device_password_id=device_password_id,
        selected_registrar=selected_registrar,
        manufacturer=manufacturer,
        model_name=model_name,
        model_number=model_number,
        device_name=device_name,
        device_type=device_type,
    )
