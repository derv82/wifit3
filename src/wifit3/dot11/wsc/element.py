"""Decoding of the WPS vendor element (221, 00-50-F2 type 4) that beacons and probe responses carry."""
from typing import Any, Dict, Optional

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

def parse_wps_element(data: bytes) -> Dict[str, Any]:
    """Walk the WPS IE's nested big-endian TLVs (``data`` = bytes after
    the OUI + OUI-type) and surface the attacker-relevant subset.

    Each TLV is a 2-byte attribute id, 2-byte length, then value.
    Missing attributes leave their fields at the model defaults.
    """
    out: Dict[str, Any] = {"wps": True}
    version1 = False
    version2 = 0
    for attr, val in iter_wsc_tlvs(data):
        ln = len(val)
        if attr == ATTR_AP_SETUP_LOCKED and ln >= 1:
            out["wps_locked"] = val[0] == 0x01
        elif attr == ATTR_STATE and ln >= 1:
            out["wps_state"] = val[0]          # 1=unconfigured, 2=configured
        elif attr == ATTR_CONFIG_METHODS and ln >= 2:
            out["wps_config_methods"] = (val[0] << 8) | val[1]
        elif attr == ATTR_DEVICE_PASSWORD_ID and ln >= 2:
            out["wps_device_password_id"] = (val[0] << 8) | val[1]
        elif attr == ATTR_SELECTED_REGISTRAR and ln >= 1:
            out["wps_selected_registrar"] = val[0] == 0x01
        elif attr == ATTR_MANUFACTURER:
            text = wps_text(val)
            if text:
                out["wsc_manufacturer"] = text
        elif attr == ATTR_MODEL_NAME:
            text = wps_text(val)
            if text:
                out["wsc_model_name"] = text
        elif attr == ATTR_MODEL_NUMBER:
            text = wps_text(val)
            if text:
                out["wsc_model_number"] = text
        elif attr == ATTR_DEVICE_NAME:
            text = wps_text(val)
            if text:
                out["wsc_device_name"] = text
        elif attr == ATTR_PRIMARY_DEV_TYPE:
            label = device_type_label(val)
            if label:
                out["wsc_device_type"] = label
        elif attr == ATTR_VERSION and ln >= 1:
            version1 = True
        elif attr == ATTR_VENDOR_EXTENSION:
            v2 = _wps_version2(val)
            if v2 is not None:
                version2 = v2
    if version2 >= 0x20:
        out["wps_version"] = "2.0"
    elif version1:
        out["wps_version"] = "1.0"
    return out
