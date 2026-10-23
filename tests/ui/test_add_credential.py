"""Validation of a credential typed into the Vault's Add Credential modal."""
import pytest

from wifit3.models import CaptureType
from wifit3.ui.vault.modals.add_credential import credential_error

_BSSID = "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize("kind, value", [
    (CaptureType.WPA_PSK, "12345678"),
    (CaptureType.WPA_PSK, "ü" * 31),
    (CaptureType.WPA_PSK, "0123456789abcdef" * 4),
    (CaptureType.WPS_PIN, "1234"),
    (CaptureType.WPS_PIN, "12345670"),
    (CaptureType.WEP, "abcde"),
    (CaptureType.WEP, "abcdefghijklm"),
    (CaptureType.WEP, "0a1b2c3d4e"),
    (CaptureType.WEP, "0a1b2c3d4e" * 2 + "0a1b2c"),
])
def test_valid_credentials(kind, value):
    assert credential_error("HomeNet", _BSSID, kind, value) is None


@pytest.mark.parametrize("ssid, bssid, kind, value", [
    ("", _BSSID, CaptureType.WPA_PSK, "12345678"),
    ("x" * 33, _BSSID, CaptureType.WPA_PSK, "12345678"),
    ("HomeNet", "aa-bb-cc-dd-ee-ff", CaptureType.WPA_PSK, "12345678"),
    ("HomeNet", _BSSID, CaptureType.WPA_PSK, "1234567"),
    ("HomeNet", _BSSID, CaptureType.WPA_PSK, "ü" * 32),
    ("HomeNet", _BSSID, CaptureType.WPA_PSK, "g" * 64),
    ("HomeNet", _BSSID, CaptureType.WPS_PIN, "123456"),
    ("HomeNet", _BSSID, CaptureType.WPS_PIN, "١٢٣٤"),
    ("HomeNet", _BSSID, CaptureType.WEP, "abcdef"),
    ("HomeNet", _BSSID, CaptureType.WEP, "abcdefghij"),
])
def test_invalid_credentials(ssid, bssid, kind, value):
    assert credential_error(ssid, bssid, kind, value) is not None
