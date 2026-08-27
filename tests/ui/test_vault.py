"""VaultView: the loot-manager screen over captures/. Pure helpers (export_zip, _copy_payload,
open_in_file_manager) tested directly; the screen itself driven through a real WifiteApp so
row selection / remove / copy / reload are exercised end to end against a tmp captures dir.
"""
import sys
import zipfile
from pathlib import Path

import pytest

from wifit3.models import PersistedCapture
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault import VaultView, _copy_payload, export_zip, open_in_file_manager

_HS_LINE = "WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***2\n"


def _write(d: Path, name: str, content: str) -> None:
    (d / name).write_text(content, encoding="utf-8")


# ----- pure helpers ----------------------------------------------------------

def test_copy_payload_prefers_the_stored_value():
    cap = PersistedCapture(type="WEP", timestamp=0, path="x", value="6162636465")
    assert _copy_payload(cap) == "6162636465"


def test_copy_payload_falls_back_to_file_content_for_handshakes(tmp_path):
    p = tmp_path / "hs.hc22000"
    p.write_text(_HS_LINE, encoding="utf-8")
    cap = PersistedCapture(type="HS", timestamp=0, path=str(p))
    assert _copy_payload(cap) == _HS_LINE.strip()


def test_copy_payload_blank_for_unreadable_file():
    cap = PersistedCapture(type="HS", timestamp=0, path="/nonexistent/nope.hc22000")
    assert _copy_payload(cap) == ""


def test_export_zip_bundles_every_file_and_lands_beside_captures_dir(tmp_path):
    captures = tmp_path / "captures"
    captures.mkdir()
    _write(captures, "a_aa-bb-cc-dd-ee-ff_1_handshake.hc22000", _HS_LINE)
    _write(captures, "b_aa-bb-cc-dd-ee-ff_2_pmkid.hc22000", _HS_LINE)

    out = export_zip(captures)

    assert out is not None
    assert out.parent == tmp_path                      # sibling of captures/, never inside it
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {
            "a_aa-bb-cc-dd-ee-ff_1_handshake.hc22000",
            "b_aa-bb-cc-dd-ee-ff_2_pmkid.hc22000",
        }


def test_export_zip_none_when_empty(tmp_path):
    captures = tmp_path / "captures"
    captures.mkdir()
    assert export_zip(captures) is None


def test_export_zip_none_when_dir_missing(tmp_path):
    assert export_zip(tmp_path / "nope") is None


def test_export_zip_never_reexports_a_previous_export(tmp_path):
    """A re-export must not bundle a prior export.zip into the new one: exports always land
    beside captures/, so they were never candidates for inclusion in the first place."""
    captures = tmp_path / "captures"
    captures.mkdir()
    _write(captures, "a_aa-bb-cc-dd-ee-ff_1_handshake.hc22000", _HS_LINE)
    first = export_zip(captures)
    second = export_zip(captures)
    with zipfile.ZipFile(second) as zf:
        assert first.name not in zf.namelist()


def test_open_in_file_manager_uses_xdg_open_on_linux(mocker):
    mocker.patch.object(sys, "platform", "linux")
    popen = mocker.patch("wifit3.ui.screens.vault.subprocess.Popen")
    open_in_file_manager(Path("/tmp/captures"))
    popen.assert_called_once_with(["xdg-open", "/tmp/captures"])


def test_open_in_file_manager_uses_open_on_macos(mocker):
    mocker.patch.object(sys, "platform", "darwin")
    popen = mocker.patch("wifit3.ui.screens.vault.subprocess.Popen")
    open_in_file_manager(Path("/tmp/captures"))
    popen.assert_called_once_with(["open", "/tmp/captures"])


# ----- the screen, end to end -------------------------------------------------

async def _mounted_vault(app, captures_dir: Path) -> VaultView:
    view = VaultView(captures_dir=captures_dir)
    app.install_screen(view, name="vault-under-test")
    await app.push_screen("vault-under-test")
    return view


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_reload_populates_rows_newest_first(tmp_path):
    captures = tmp_path / "captures"
    captures.mkdir()
    _write(captures, "Old_aa-bb-cc-dd-ee-ff_1000_handshake.hc22000", _HS_LINE)
    _write(captures, "New_aa-bb-cc-dd-ee-ff_2000_pmkid.hc22000", _HS_LINE)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _mounted_vault(app, captures)
        await pilot.pause(0)
        table = view.query_one("#vault-table")
        assert table.row_count == 2
        assert table.get_row_at(0)[0] == "New"           # newest first
        assert table.get_row_at(1)[0] == "Old"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_remove_selected_deletes_the_file_and_reloads(tmp_path):
    captures = tmp_path / "captures"
    captures.mkdir()
    _write(captures, "Net_aa-bb-cc-dd-ee-ff_1000_handshake.hc22000", _HS_LINE)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _mounted_vault(app, captures)
        await pilot.pause(0)
        target = captures / "Net_aa-bb-cc-dd-ee-ff_1000_handshake.hc22000"
        assert target.exists()

        view.action_remove_selected()
        await pilot.pause(0)

        assert not target.exists()
        assert view.query_one("#vault-table").row_count == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_copy_selected_copies_the_wep_key_to_the_clipboard(tmp_path, mocker):
    captures = tmp_path / "captures"
    captures.mkdir()
    _write(captures, "Net_aa-bb-cc-dd-ee-ff_1000_wep_key.txt",
          "SSID:  Net\nBSSID: aa:bb:cc:dd:ee:ff\nWEP key (hex):   6162636465\n")

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _mounted_vault(app, captures)
        await pilot.pause(0)
        copy = mocker.patch.object(app, "copy_to_clipboard")
        view.action_copy_selected()
        copy.assert_called_once_with("6162636465")


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_no_selection_actions_are_a_safe_noop(tmp_path, mocker):
    captures = tmp_path / "captures"
    captures.mkdir()
    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _mounted_vault(app, captures)
        await pilot.pause(0)
        copy = mocker.patch.object(app, "copy_to_clipboard")
        view.action_remove_selected()      # must not raise on an empty table
        view.action_copy_selected()
        copy.assert_not_called()
