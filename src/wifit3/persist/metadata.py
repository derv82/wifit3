from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from wifit3.models import AccessPoint
from wifit3.persist.common import bssid_to_dashed
from wifit3.persist.config import Config


def metadata_path(bssid: str) -> Path:
    return Path(Config.captures_dir) / f"{bssid_to_dashed(bssid)}.meta"


def load_metadata(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_ap_nickname(ap: AccessPoint, nickname: str) -> Path:
    """Set or clear the user-facing nickname without changing the AP's canonical SSID."""
    path = metadata_path(ap.bssid)
    data = load_metadata(path)
    data.update({"version": 1, "bssid": ap.bssid.lower(), "updated_at": int(time.time())})
    if ap.ssid and ap.ssid != "<hidden>":
        data["ssid"] = ap.ssid
    if nickname:
        data["nickname"] = nickname
    else:
        data.pop("nickname", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".meta.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def save_ap_metadata(
    ap: AccessPoint,
    *,
    psk: str | None = None,
    wps_pin: str | None = None,
    wep_key: str | None = None,
    eap_credential: tuple[str, str] | None = None,
    eap_settings: dict[str, str] | None = None,
    include_observed_data: bool = True,
) -> Path:
    """Merge known AP and credential data into its UTF-8 Vault sidecar."""
    path = metadata_path(ap.bssid)
    existing = load_metadata(path)
    credentials = existing.get("credentials")
    if not isinstance(credentials, dict):
        credentials = {}
    if psk:
        credentials["psk"] = psk
    if wps_pin:
        credentials["wps_pin"] = wps_pin
    if wep_key:
        credentials["wep_key"] = wep_key
    if eap_credential:
        username, password = eap_credential
        eap_credentials = credentials.get("eap")
        if not isinstance(eap_credentials, list):
            eap_credentials = []
        entry = {"username": username, "password": password, "combo": f"{username}:{password}"}
        if entry not in eap_credentials:
            eap_credentials.append(entry)
        credentials["eap"] = eap_credentials

    data = {
        **existing,
        "version": 1,
        "bssid": ap.bssid.lower(),
        "updated_at": int(time.time()),
    }
    if ap.ssid and ap.ssid != "<hidden>":
        data["ssid"] = ap.ssid
    fields = {
        "channel": ap.channel,
        "encryption": ap.encryption,
        "akms": ap.akms,
        "akm_suites": ap.akm_suites,
        "pairwise_cipher": ap.pairwise_cipher,
        "wpa3": ap.wpa3,
        "transition_mode": ap.transition_mode,
        "pmf_capable": ap.pmf_capable,
        "pmf_required": ap.pmf_required,
        "beacon_protection": ap.beacon_protection,
        "wps": ap.wps,
        "wps_locked": ap.wps_locked,
        "wps_version": ap.wps_version,
        "wps_config_methods": ap.wps_config_methods,
        "wps_device_password_id": ap.wps_device_password_id,
        "first_seen": ap.first_seen,
        "last_seen": ap.last_seen,
        "decloak_method": ap.decloak_method,
    }
    if include_observed_data:
        for name, value in fields.items():
            if value not in (None, [], "Unknown"):
                data[name] = value
    if credentials:
        data["credentials"] = credentials
    if eap_settings:
        eap = existing.get("eap")
        if not isinstance(eap, dict):
            eap = {}
        eap.update({name: value for name, value in eap_settings.items() if value})
        if eap:
            data["eap"] = eap

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".meta.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
