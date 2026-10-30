import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button

from tests.ui.test_focus_v2_layout import _Host
from tests.wlan.mocks import build_ap, mock_array
from wifit3.persist.vault import Vault
from wifit3.ui.ap_table import APRow, APTable
from wifit3.ui.encryption_format import EncryptionSummary, EncryptionType
from wifit3.ui.screens.channel_filter import ChannelFilterDialog
from wifit3.ui.screens.splash import SplashView

pytestmark = pytest.mark.asyncio(loop_scope="module")


class _TableApp(App):
    CSS = "APTable { height: 1fr; }"

    def compose(self) -> ComposeResult:
        yield APTable(id="t")


class _SplashApp(App):
    def on_mount(self) -> None:
        self.push_screen(SplashView())


class _ChannelFilterApp(App):
    def on_mount(self) -> None:
        self.push_screen(ChannelFilterDialog([1, 6, 11, 36, 40]))


class _WpaHost(_Host):
    def __init__(self) -> None:
        super().__init__()
        self.target_ap = build_ap(wpa3=False)
        self.target_ap.wps = True
        self.array = mock_array(cards=0)
        self.vault = Vault()


@pytest.mark.parametrize("width", [40, 45, 50, 60, 80, 100])
async def test_ap_table_no_horizontal_scroll_across_widths(width: int) -> None:
    app = _TableApp()
    async with app.run_test(size=(width, 24)) as pilot:
        table = app.query_one("#t", APTable)
        table.set_rows([
            APRow(
                bssid="00:11:22:33:44:55",
                ssid="VeryLongNetworkNameForTesting123",
                channel=6,
                signal=-50,
                beacons=100,
                clients=2,
                encryption=EncryptionSummary(
                    EncryptionType.WPA2, "PSK", "CCMP", 0, "WPA2"
                ),
                wps=True,
                chipset="Broadcom",
                identity="Netgear R7000",
            )
        ])
        await pilot.pause()
        assert table.max_scroll_x == 0
        assert table._content_width() <= table.scrollable_content_region.width


@pytest.mark.parametrize("width", [40, 50, 60, 80])
async def test_splash_view_logo_adaptation(width: int) -> None:
    app = _SplashApp()
    async with app.run_test(size=(width, 35)) as pilot:
        await pilot.pause()
        ascii_art = app.screen.query_one("#ascii-art")
        compact_logo = app.screen.query_one("#compact-logo")
        if width < 54:
            assert not ascii_art.display
            assert compact_logo.display
        else:
            assert ascii_art.display


@pytest.mark.parametrize("width", [40, 45, 50, 60, 80, 100])
async def test_focus_v2_controls_unclipped_across_widths(width: int) -> None:
    app = _WpaHost()
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        actions = app.screen.query_one("#actions")
        for button in actions.query(Button):
            if button.display:
                assert button.region.x + button.region.width <= width


@pytest.mark.parametrize("width", [40, 45, 50, 60, 80, 100])
async def test_channel_filter_dialog_fits_viewport(width: int) -> None:
    app = _ChannelFilterApp()
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        dialog = app.screen.query_one("#dialog")
        assert dialog.region.width <= width
        assert dialog.region.x >= 0
