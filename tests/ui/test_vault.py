"""VaultView (ui/screens/vault.py) + VaultItemView (vault_item.py): the AP table and the
per-AP detail pane, driven through a real WifiteApp. The autouse _captures_to_tmp fixture
points Config.captures_dir at tmp_path, and WifiteApp builds its Vault from there.
"""
import pytest
from textual.widgets import Button, DataTable

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault import VaultView
from wifit3.ui.screens.vault_item import ConfirmModal, _CapturePanel

_HS_LINE = "WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***2\n"
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_WEP_TXT = "SSID: HomeNet\nBSSID: aa:bb:cc:dd:ee:ff\nWEP key (hex):   6162636465\n"

def _write(d, name, content):
    (d / name).write_text(content, encoding="utf-8")

async def _open_vault(app) -> VaultView:
    app.push_screen("vault")
    return app.screen


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_duplicate_essid_disambiguated_by_bssid(tmp_path):
    _write(tmp_path, "Net_aa-bb-cc-dd-ee-01_1000_handshake.hc22000", _HS_LINE)
    _write(tmp_path, "Net_aa-bb-cc-dd-ee-02_1000_handshake.hc22000", _HS_LINE)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        table = view.query_one("#vault-aps", DataTable)
        names = [table.get_row_at(i)[0] for i in range(table.row_count)]
        assert len(names) == 2 and all(n.startswith("Net (") for n in names)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_delete_in_panel_confirms_and_removes(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    target = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt"

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
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
        view.query_one(_CapturePanel).query_one(".delete", Button).press()
        await pilot.pause()
        app.screen.query_one("#no", Button).press()
        await pilot.pause()
        assert target.exists()
        assert view.query_one("#vault-aps", DataTable).row_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_consolidate_button_in_hs_panel_when_legacy(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000", _HS_LINE)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        panel = view.query_one(_CapturePanel)   # HANDSHAKE / PMKID
        assert len(panel.query(".consolidate")) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_aggregate_file_appears_once_and_counts_records(tmp_path):
    _write(tmp_path, "Agg_11-22-33-44-55-66.hc22000", _HS_LINE + _PMKID_LINE + _PMKID_LINE)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        panel = view.query_one(_CapturePanel)               # HANDSHAKE / PMKID
        assert panel.border_title.endswith("(1)")            # one file, not two entries
        assert view.query_one("#vault-aps", DataTable).get_row_at(0)[1] == "3"  # 1 HS + 2 PMKID
