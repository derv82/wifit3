"""Tests for the one-off captures/ migrator."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the script as a module (top-level scripts/ isn't a package).
_SPEC = importlib.util.spec_from_file_location(
    "migrate_captures",
    Path(__file__).resolve().parents[2] / "scripts" / "migrate_captures.py",
)
migrate_captures = importlib.util.module_from_spec(_SPEC)
sys.modules["migrate_captures"] = migrate_captures
_SPEC.loader.exec_module(migrate_captures)
build_plan = migrate_captures.build_plan
apply_plan = migrate_captures.apply_plan


_BSSID = "aa-bb-cc-dd-ee-ff"
_STEM = f"TestNet_{_BSSID}_1700000000"

_HS_LINE = "WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***2\n"
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_WEPKEY_TXT = (
    "SSID:  TestNet\n"
    "BSSID: aa:bb:cc:dd:ee:ff\n"
    "WEP key (hex):   6162636465\n"
    'WEP key (ASCII): "abcde"\n'
)
_WPS_PBC_BODY = (
    "SSID: TestNet\n"
    "BSSID: aa:bb:cc:dd:ee:ff\n"
    "PSK: yxws3tik\n"
    "method: WPS-PBC\n"
)
_WPS_PIN_BODY = (
    "SSID: TestNet\n"
    "BSSID: aa:bb:cc:dd:ee:ff\n"
    "PSK: abcdefgh\n"
    "method: WPS-PIN\n"
    "PIN: 12345670\n"
)


def _write(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


# ---- wepkey → wep_key ------------------------------------------------------

def test_wepkey_renamed(tmp_path):
    _write(tmp_path, f"{_STEM}_wepkey.txt", _WEPKEY_TXT)
    plan = build_plan(tmp_path)
    apply_plan(plan)
    assert not (tmp_path / f"{_STEM}_wepkey.txt").exists()
    new = tmp_path / f"{_STEM}_wep_key.txt"
    assert new.exists()
    # Body unchanged
    assert new.read_text(encoding="utf-8") == _WEPKEY_TXT


# ---- .wps → _wps_pbc.txt / _wps_pin.txt ------------------------------------

def test_wps_pbc_migrated(tmp_path):
    src = _write(tmp_path, f"{_STEM}.wps", _WPS_PBC_BODY)
    apply_plan(build_plan(tmp_path))
    assert not src.exists()
    new = tmp_path / f"{_STEM}_wps_pbc.txt"
    assert new.exists()
    body = new.read_text(encoding="utf-8")
    assert "PSK: yxws3tik" in body
    assert "method:" not in body


def test_wps_pin_migrated_preserves_pin(tmp_path):
    src = _write(tmp_path, f"{_STEM}.wps", _WPS_PIN_BODY)
    apply_plan(build_plan(tmp_path))
    assert not src.exists()
    new = tmp_path / f"{_STEM}_wps_pin.txt"
    assert new.exists()
    body = new.read_text(encoding="utf-8")
    assert "PSK: abcdefgh" in body
    assert "PIN: 12345670" in body
    assert "method:" not in body


def test_wps_without_method_is_skipped(tmp_path):
    _write(tmp_path, f"{_STEM}.wps", "SSID: X\nBSSID: aa:bb:cc:dd:ee:ff\nPSK: psk\n")
    plan = build_plan(tmp_path)
    assert plan.rewrites == []
    assert any("no 'method:'" in reason for _, reason in plan.skips)


# ---- .hc22000 split --------------------------------------------------------

def test_hc22000_handshake_only_renamed(tmp_path):
    src = _write(tmp_path, f"{_STEM}.hc22000", _HS_LINE)
    apply_plan(build_plan(tmp_path))
    assert not src.exists()
    assert (tmp_path / f"{_STEM}_handshake.hc22000").exists()
    assert not (tmp_path / f"{_STEM}_pmkid.hc22000").exists()


def test_hc22000_pmkid_only_renamed(tmp_path):
    src = _write(tmp_path, f"{_STEM}.hc22000", _PMKID_LINE)
    apply_plan(build_plan(tmp_path))
    assert not src.exists()
    assert (tmp_path / f"{_STEM}_pmkid.hc22000").exists()


def test_hc22000_mixed_splits(tmp_path):
    src = _write(tmp_path, f"{_STEM}.hc22000", _HS_LINE + _PMKID_LINE)
    apply_plan(build_plan(tmp_path))
    assert not src.exists()
    hs_path = tmp_path / f"{_STEM}_handshake.hc22000"
    pmkid_path = tmp_path / f"{_STEM}_pmkid.hc22000"
    assert hs_path.exists() and pmkid_path.exists()
    hs_body = hs_path.read_text(encoding="utf-8")
    pmkid_body = pmkid_path.read_text(encoding="utf-8")
    assert "WPA*02*" in hs_body and "WPA*01*" not in hs_body
    assert "WPA*01*" in pmkid_body and "WPA*02*" not in pmkid_body


def test_hc22000_empty_is_skipped(tmp_path):
    _write(tmp_path, f"{_STEM}.hc22000", "")
    plan = build_plan(tmp_path)
    assert plan.renames == [] and plan.rewrites == []
    assert any("no WPA" in reason for _, reason in plan.skips)


# ---- .pcap follows its .hc22000 sibling ------------------------------------

def test_pcap_renamed_to_match_handshake_sibling(tmp_path):
    _write(tmp_path, f"{_STEM}.hc22000", _HS_LINE)
    _write(tmp_path, f"{_STEM}.pcap", "x")  # contents don't matter for naming
    apply_plan(build_plan(tmp_path))
    assert (tmp_path / f"{_STEM}_handshake.pcap").exists()
    assert not (tmp_path / f"{_STEM}.pcap").exists()


def test_pcap_copied_to_both_when_sibling_splits(tmp_path):
    _write(tmp_path, f"{_STEM}.hc22000", _HS_LINE + _PMKID_LINE)
    pcap = _write(tmp_path, f"{_STEM}.pcap", "x")
    apply_plan(build_plan(tmp_path))
    assert (tmp_path / f"{_STEM}_handshake.pcap").exists()
    assert (tmp_path / f"{_STEM}_pmkid.pcap").exists()
    assert not pcap.exists()


def test_orphan_pcap_is_skipped(tmp_path):
    # No .hc22000 sibling → kind unknown → skip with warning.
    _write(tmp_path, f"{_STEM}.pcap", "x")
    plan = build_plan(tmp_path)
    assert plan.renames == [] and plan.copies == []
    assert any("no .hc22000 sibling" in reason for _, reason in plan.skips)


# ---- Idempotency + target-collision ---------------------------------------

def test_second_run_is_noop(tmp_path):
    _write(tmp_path, f"{_STEM}_wepkey.txt", _WEPKEY_TXT)
    _write(tmp_path, f"{_STEM}.wps", _WPS_PBC_BODY)
    _write(tmp_path, f"{_STEM}.hc22000", _HS_LINE)
    _write(tmp_path, f"{_STEM}.pcap", "x")
    apply_plan(build_plan(tmp_path))
    # Second pass — none of the new-format names match the input patterns,
    # so the plan should be empty.
    plan = build_plan(tmp_path)
    assert plan.renames == []
    assert plan.rewrites == []
    assert plan.copies == []
    assert plan.deletes == []


def test_skips_when_target_exists(tmp_path):
    _write(tmp_path, f"{_STEM}_wepkey.txt", _WEPKEY_TXT)
    # Target already there from a half-run
    _write(tmp_path, f"{_STEM}_wep_key.txt", "pre-existing\n")
    plan = build_plan(tmp_path)
    assert plan.renames == []
    assert any("target exists" in reason for _, reason in plan.skips)


def test_dry_run_leaves_filesystem_untouched(tmp_path):
    _write(tmp_path, f"{_STEM}_wepkey.txt", _WEPKEY_TXT)
    _write(tmp_path, f"{_STEM}.wps", _WPS_PBC_BODY)
    _write(tmp_path, f"{_STEM}.hc22000", _HS_LINE + _PMKID_LINE)
    snapshot = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()}
    build_plan(tmp_path)  # build only — never apply
    after = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()}
    assert snapshot == after
