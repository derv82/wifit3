"""VaultView (ui/screens/vault.py) + VaultItemView (vault_item.py): the AP table and the
per-AP detail pane, driven through a real WifiteApp. The autouse _captures_to_tmp fixture
points Config.captures_dir at tmp_path, and WifiteApp builds its Vault from there.
"""
import pytest
from textual.widgets import Button, DataTable

from wifit3.models import CaptureType, PersistedCapture
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault import VaultView, _count_captures, _count_keys
from wifit3.ui.screens.vault_item import ConfirmModal, VaultItemView, _CapturePanel, _hex_to_ascii

_HS_LINE = "WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***2\n"
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_WEP_TXT = "SSID: HomeNet\nBSSID: aa:bb:cc:dd:ee:ff\nWEP key (hex):   6162636465\n"


def _write(d, name, content):
    (d / name).write_text(content, encoding="utf-8")


def _cap(kind, path, bssid="aa:bb:cc:dd:ee:ff", value=None, record_count=1):
    return PersistedCapture(type=kind, timestamp=1, path=path, bssid=bssid, value=value,
                            record_count=record_count)


# ----- pure helpers ----------------------------------------------------------

def test_hex_to_ascii_printable():
    assert _hex_to_ascii("6162636465") == "abcde"


def test_hex_to_ascii_nonprintable_is_blank():
    assert _hex_to_ascii("00ff") == ""


def test_hex_to_ascii_invalid_is_blank():
    assert _hex_to_ascii("nothex") == ""


def test_count_captures_sums_hashline_records():
    caps = [
        _cap(CaptureType.HS, "agg.hc22000", record_count=1),
        _cap(CaptureType.PMKID, "agg.hc22000", record_count=2),   # aggregate: 1 HS + 2 PMKID
        _cap(CaptureType.HS, "hs.pcap", record_count=0),           # raw .pcap companion
    ]
    assert _count_captures(caps) == 3


def test_count_keys_counts_creds_with_value():
    caps = [
        _cap(CaptureType.WEP, "w", value="deadbeef"),
        _cap(CaptureType.WPS_PBC, "p", value="psk"),
        _cap(CaptureType.HS, "h"),                    # not a key
        _cap(CaptureType.WPS_PIN, "n", value=None),   # no value -> not counted
    ]
    assert _count_keys(caps) == 2


# ----- the screen + widget, end to end ---------------------------------------

async def _open_vault(app) -> VaultView:
    app.push_screen("vault")
    return app.screen


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_ap_table_lists_aps_with_counts(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_handshake.hc22000", _HS_LINE)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_handshake.pcap", "x")
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1001_wep_key.txt", _WEP_TXT)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        assert isinstance(view, VaultView)
        table = view.query_one("#vault-aps", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "HomeNet"
        assert row[1] == "1"   # captures: the hc22000 + pcap handshake dedupe to one
        assert row[2] == "1"   # keys: one WEP key


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_empty_vault_shows_placeholder(tmp_path):
    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        assert view.query_one("#vault-aps", DataTable).row_count == 0
        assert view.query_one("#vault-item-empty")


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
async def test_selecting_ap_populates_detail_widget(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_handshake.hc22000", _HS_LINE)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_handshake.pcap", "x")

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        widget = view.query_one("#vault-item", VaultItemView)
        assert widget.query_one("#vault-item-title")          # AP title chip present
        panels = widget.query(_CapturePanel)
        assert len(panels) == 1                                # only HANDSHAKE / PMKID
        assert panels.first().border_title.endswith("(2)")    # both file variants listed


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
async def test_copy_hex_in_wep_panel(tmp_path, mocker):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        copy = mocker.patch.object(app, "copy_to_clipboard")
        view.query_one(_CapturePanel).query_one(".copy-hex", Button).press()
        await pilot.pause()
        copy.assert_called_once_with("6162636465")


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


_WPS_PIN_TXT = "SSID: HomeNet\nBSSID: aa:bb:cc:dd:ee:ff\nPSK: hunter2\nPIN: 01030365\n"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_wps_pin_panel_shows_psk_and_pin_rows(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wps_pin.txt", _WPS_PIN_TXT)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        panel = view.query_one(_CapturePanel)   # WPS PIN
        assert len(panel.query(".copy-psk")) == 1
        assert len(panel.query(".copy-pin")) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_copy_pin_copies_the_pin(tmp_path, mocker):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wps_pin.txt", _WPS_PIN_TXT)

    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        copy = mocker.patch.object(app, "copy_to_clipboard")
        view.query_one(_CapturePanel).query_one(".copy-pin", Button).press()
        await pilot.pause()
        copy.assert_called_once_with("01030365")


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
