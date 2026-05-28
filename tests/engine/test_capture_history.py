"""Tests for the captures/ history loader (synthetic files — no real IDs)."""
from __future__ import annotations

from wifit3.engine.capture_history import load_capture_index, summarize

_BSSID_DASH = "aa-bb-cc-dd-ee-ff"
_BSSID_COLON = "aa:bb:cc:dd:ee:ff"

# Minimal hashlines — only the WPA*TYPE* prefix is inspected.
_HS_LINE = "WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***2\n"
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_WEPKEY_TXT = (
    "SSID:  TestNet\n"
    f"BSSID: {_BSSID_COLON}\n"
    "WEP key (hex):   6162636465\n"
    'WEP key (ASCII): "abcde"\n'
)


def _write(d, name, content):
    (d / name).write_text(content, encoding="utf-8")


class TestLoadCaptureIndex:
    def test_handshake_hc22000(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000.hc22000", _HS_LINE)
        idx = load_capture_index(tmp_path)
        assert _BSSID_COLON in idx
        caps = idx[_BSSID_COLON]
        assert len(caps) == 1 and caps[0].kind == "HS"
        assert caps[0].timestamp == 1700000000

    def test_pmkid_hc22000(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000001.hc22000", _PMKID_LINE)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert [c.kind for c in caps] == ["PMKID"]

    def test_one_file_both_kinds(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000002.hc22000",
               _HS_LINE + _PMKID_LINE)
        kinds = {c.kind for c in load_capture_index(tmp_path)[_BSSID_COLON]}
        assert kinds == {"HS", "PMKID"}

    def test_wep_key_txt(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000003_wepkey.txt", _WEPKEY_TXT)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert len(caps) == 1
        assert caps[0].kind == "WEP" and caps[0].value == "6162636465"

    def test_wps_psk_file(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000006.wps",
               "SSID: TestNet\nBSSID: aa:bb:cc:dd:ee:ff\nPSK: yxws3tik\nmethod: WPS-PBC\n")
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert len(caps) == 1
        assert caps[0].kind == "WPS" and caps[0].value == "yxws3tik"

    def test_pcap_alone_is_ignored(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000004.pcap", "binary-ish")
        assert load_capture_index(tmp_path) == {}

    def test_ssid_with_underscores_parses(self, tmp_path):
        _write(tmp_path, f"Beach_2_4_{_BSSID_DASH}_1700000005.hc22000", _HS_LINE)
        assert _BSSID_COLON in load_capture_index(tmp_path)

    def test_unrecognized_name_ignored(self, tmp_path):
        _write(tmp_path, "cracks.txt", "somekey\n")
        assert load_capture_index(tmp_path) == {}

    def test_missing_dir_is_empty(self, tmp_path):
        assert load_capture_index(tmp_path / "nope") == {}

    def test_sorted_newest_first(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000.hc22000", _HS_LINE)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700009999.hc22000", _PMKID_LINE)
        caps = load_capture_index(tmp_path)[_BSSID_COLON]
        assert [c.timestamp for c in caps] == [1700009999, 1700000000]


class TestSummarize:
    def test_totals(self, tmp_path):
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000.hc22000",
               _HS_LINE + _PMKID_LINE)
        _write(tmp_path, f"Other_11-22-33-44-55-66_1700000001_wepkey.txt", _WEPKEY_TXT)
        _write(tmp_path, f"Pbc_22-33-44-55-66-77_1700000002.wps", "PSK: hunter2\n")
        hs, pmkid, wep, wps = summarize(load_capture_index(tmp_path))
        assert (hs, pmkid, wep, wps) == (1, 1, 1, 1)

    def test_deduped_per_ap(self, tmp_path):
        # Two handshakes for ONE ap -> counts as one handshake, not two.
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700000000.hc22000", _HS_LINE)
        _write(tmp_path, f"TestNet_{_BSSID_DASH}_1700009999.hc22000", _HS_LINE)
        assert summarize(load_capture_index(tmp_path)) == (1, 0, 0, 0)
