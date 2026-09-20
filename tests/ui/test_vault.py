"""Vault drawer overlay and VaultItemView detail pane, driven through a real WifiteApp.
The autouse _captures_to_tmp fixture points Config.captures_dir at tmp_path, and WifiteApp builds its Vault from there.
"""
import pytest
from textual.widgets import Button, DataTable

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault_drawer import VaultDrawer
from wifit3.ui.screens.vault_item import ConfirmModal, _CapturePanel

_HS_LINE = "WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***2\n"
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_WEP_TXT = "SSID: HomeNet\nBSSID: aa:bb:cc:dd:ee:ff\nWEP key (hex):   6162636465\n"

def _write(d, name, content):
    (d / name).write_text(content, encoding="utf-8")

async def _open_vault(app) -> VaultDrawer:
    app.action_toggle_vault()
    return app.screen


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_delete_in_panel_confirms_and_removes(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    target = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt"

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        
        # Force load the first AP
        table = view.query_one("#vault-table")._aps
        if table:
            bssid, (ssid, caps) = next(iter(table.items()))
            view.query_one("VaultItemView").load(bssid, ssid, caps)
            await pilot.pause()
            
        view.query_one(_CapturePanel).query_one(".delete", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        app.screen.query_one("#yes", Button).press()
        await pilot.pause()
        assert not target.exists()
        assert view.query_one("#vault-aps", DataTable).row_count == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_delete_cancelled_keeps_file(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    target = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt"

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        
        # Force load the first AP
        table = view.query_one("#vault-table")._aps
        if table:
            bssid, (ssid, caps) = next(iter(table.items()))
            view.query_one("VaultItemView").load(bssid, ssid, caps)
            await pilot.pause()
            
        view.query_one(_CapturePanel).query_one(".delete", Button).press()
        await pilot.pause()
        app.screen.query_one("#no", Button).press()
        await pilot.pause()
        assert target.exists()
        assert view.query_one("#vault-aps", DataTable).row_count == 1
