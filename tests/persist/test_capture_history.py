"""Tests for the captures/ history loader (synthetic files, no real IDs)."""
from __future__ import annotations

from wifit3.persist.capture_history import load_capture_index, summarize

_BSSID_DASH = "aa-bb-cc-dd-ee-ff"
_BSSID_COLON = "aa:bb:cc:dd:ee:ff"

# Minimal hashlines: only the WPA*TYPE* prefix is inspected.
_HS_LINE = "WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***2\n"
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_WEPKEY_TXT = (
    "SSID:  TestNet\n"
    f"BSSID: {_BSSID_COLON}\n"
    "WEP key (hex):   6162636465\n"
    'WEP key (ASCII): "abcde"\n'
)
_WPS_PBC_TXT = (
    "SSID: TestNet\n"
    f"BSSID: {_BSSID_COLON}\n"
    "PSK: yxws3tik\n"
)
_WPS_PIN_TXT = (
    "SSID: TestNet\n"
    f"BSSID: {_BSSID_COLON}\n"
    "PSK: abcdefgh\n"
    "PIN: 12345670\n"
)


def _write(d, name, content):
    (d / name).write_text(content, encoding="utf-8")


class TestLoadCaptureIndex:
    def test_handshake_hc22000(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000_handshake.hc22000", _HS_LINE)
        idx = load_capture_index(tmp_path)
        assert _BSSID_COLON in idx
        caps = idx[_BSSID_COLON]
        assert len(caps) == 1 and caps[0].type == "HS"
        assert caps[0].timestamp == 1700000000

    def test_pmkid_hc22000(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000001_pmkid.hc22000", _PMKID_LINE)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert [c.type for c in caps] == ["PMKID"]

    def test_handshake_and_pmkid_as_separate_files(self, tmp_path):
        # Post-refactor, each type is its own file (no more mixed .hc22000).
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000002_handshake.hc22000", _HS_LINE)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000003_pmkid.hc22000", _PMKID_LINE)
        types = {c.type for c in load_capture_index(tmp_path)[_BSSID_COLON]}
        assert types == {"HS", "PMKID"}

    def test_wep_key_txt(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000004_wep_key.txt", _WEPKEY_TXT)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert len(caps) == 1
        assert caps[0].type == "WEP" and caps[0].value == "6162636465"

    def test_wps_pbc_txt(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000005_wps_pbc.txt", _WPS_PBC_TXT)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert len(caps) == 1
        assert caps[0].type == "WPS" and caps[0].value == "yxws3tik"

    def test_wps_pin_txt(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000006_wps_pin.txt", _WPS_PIN_TXT)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert len(caps) == 1
        assert caps[0].type == "WPS" and caps[0].value == "abcdefgh"

    def test_pcap_companion_is_ignored(self, tmp_path):
        # A handshake.pcap on its own contributes no PersistedCapture. Its
        # hashline sibling carries the verdict.
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000007_handshake.pcap", "binary-ish")
        assert load_capture_index(tmp_path) == {}

    def test_ssid_with_underscores_parses(self, tmp_path):
        _write(tmp_path, f"Beach_2_4_{_BSSID_DASH}_1700000008_handshake.hc22000", _HS_LINE)
        assert _BSSID_COLON in load_capture_index(tmp_path)

    def test_unrecognized_name_ignored(self, tmp_path):
        _write(tmp_path, "cracks.txt", "somekey\n")
        assert load_capture_index(tmp_path) == {}

    def test_legacy_unsuffixed_name_ignored(self, tmp_path):
        # Old-format file (no _<kind> suffix) shouldn't accidentally parse.
        # The migration script converts these; the reader doesn't bridge.
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000009.hc22000", _HS_LINE)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000010_wepkey.txt", _WEPKEY_TXT)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000011.wps", _WPS_PBC_TXT)
        assert load_capture_index(tmp_path) == {}

    def test_missing_dir_is_empty(self, tmp_path):
        assert load_capture_index(tmp_path / "nope") == {}

    def test_sorted_newest_first(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000_handshake.hc22000", _HS_LINE)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700009999_pmkid.hc22000", _PMKID_LINE)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert [c.timestamp for c in caps] == [1700009999, 1700000000]


class TestSummarize:
    def test_totals(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000_handshake.hc22000", _HS_LINE)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000001_pmkid.hc22000", _PMKID_LINE)
        _write(tmp_path, "Other_11-22-33-44-55-66_1700000002_wep_key.txt", _WEPKEY_TXT)
        _write(tmp_path, "Pbc_22-33-44-55-66-77_1700000003_wps_pbc.txt", "PSK: hunter2\n")
        hs, pmkid, wep, wps = summarize(load_capture_index(tmp_path))
        assert (hs, pmkid, wep, wps) == (1, 1, 1, 1)

    def test_deduped_per_ap(self, tmp_path):
        # Two handshakes for ONE ap -> counts as one handshake, not two.
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000_handshake.hc22000", _HS_LINE)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700009999_handshake.hc22000", _HS_LINE)
        assert summarize(load_capture_index(tmp_path)) == (1, 0, 0, 0)
