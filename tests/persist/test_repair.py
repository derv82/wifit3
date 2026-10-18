import json

from wifit3.persist.repair import repair_vault


def test_repair_creates_metadata_for_credential_only_aps(tmp_path):
    credential = tmp_path / "Cafe_WiFi_aa-bb-cc-dd-ee-ff_1700000001_wpa_psk.txt"
    credential.write_text(
        "SSID: Café WiFi\nBSSID: aa:bb:cc:dd:ee:ff\nPSK: password1\n",
        encoding="utf-8",
    )

    result = repair_vault()
    metadata = json.loads((tmp_path / "aa-bb-cc-dd-ee-ff.meta").read_text(encoding="utf-8"))

    assert result.errors == ()
    assert result.aps == 1
    assert metadata["ssid"] == "Café WiFi"
    assert metadata["credentials"]["psk"] == "password1"


def test_repair_recovers_utf8_ssid_and_preserves_credentials(tmp_path):
    old_hash = tmp_path / "sanitized_aa-bb-cc-dd-ee-ff_1700000000_handshake.hc22000"
    line = (
        "WPA*01*" + "11" * 16 + "*aabbccddeeff*112233445566*"
        + "Café WiFi / upstairs".encode().hex() + "***"
    )
    old_hash.write_text(line + "\n", encoding="utf-8")
    credential = tmp_path / "saved_aa-bb-cc-dd-ee-ff_1700000001_wpa_psk.txt"
    credential.write_text(
        "SSID: Café WiFi / upstairs\nBSSID: aa:bb:cc:dd:ee:ff\nPSK: password1\n",
        encoding="utf-8",
    )
    metadata = tmp_path / "aa-bb-cc-dd-ee-ff.meta"
    metadata.write_text(
        json.dumps({
            "version": 1,
            "bssid": "aa:bb:cc:dd:ee:ff",
            "credentials": {"psk": "password1"},
        }),
        encoding="utf-8",
    )

    result = repair_vault()

    repaired_hash = tmp_path / "Caf__WiFi___upstairs_aa-bb-cc-dd-ee-ff.hc22000"
    repaired_metadata = json.loads(metadata.read_text(encoding="utf-8"))
    assert result.aps == 1
    assert result.hash_lines == 1
    assert result.backup_dir is not None
    assert (result.backup_dir / old_hash.name).read_text(encoding="utf-8") == line + "\n"
    assert repaired_hash.read_text(encoding="utf-8") == line + "\n"
    assert not old_hash.exists()
    assert credential.read_text(encoding="utf-8").endswith("PSK: password1\n")
    assert repaired_metadata["ssid"] == "Café WiFi / upstairs"
    assert repaired_metadata["credentials"] == {"psk": "password1"}
