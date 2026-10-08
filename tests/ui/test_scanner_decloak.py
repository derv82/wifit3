from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.app import App

from wifit3.campaigns.campaign import Campaign
from wifit3.models import AccessPoint
from wifit3.persist.vault import Vault
from wifit3.ui.ap_table import APTable
from wifit3.ui.screens import scanner as scanner_module
from wifit3.ui.screens.decloak_modal import DecloakCandidateModal
from wifit3.ui.screens.scanner import ScannerView


class _ScannerArray:
    def __init__(self, ap: AccessPoint) -> None:
        self.access_points = {ap.bssid: ap}
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = [1, 6, 11]
        self.members = []
        self.start_calls = 0
        self.stop_calls = 0

    def get_access_points(self, include_eviltwin=True):
        return list(self.access_points.values())

    async def start_hopping(self, channels=None, interval=0.25):
        self.start_calls += 1

    async def stop_hopping(self):
        self.stop_calls += 1


class _ScannerHost(App):
    def __init__(self, array: _ScannerArray) -> None:
        super().__init__()
        self.array = array
        self.pbc_enabled = False
        self.vault = Vault()

    def on_mount(self) -> None:
        self.push_screen(ScannerView())


@pytest.fixture(autouse=True)
def _reset_campaign():
    Campaign.active = None
    yield
    Campaign.active = None


async def test_hidden_selection_opens_the_shared_candidate_modal():
    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    app = _ScannerHost(_ScannerArray(hidden))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner.refresh_table()
        scanner.query_one(APTable).move_cursor(0)
        scanner.action_decloak()
        await pilot.pause(0)
        assert isinstance(app.screen, DecloakCandidateModal)


async def test_sweep_stops_and_restores_hopping(monkeypatch):
    class _FinishedCampaign:
        def __init__(self, array, ap, *, candidates, log):
            self.revealed = "Recovered"
            self.sent = 1
            self.done = True

        def run(self):
            return True

    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    array = _ScannerArray(hidden)
    app = _ScannerHost(array)
    monkeypatch.setattr(scanner_module, "DecloakCampaign", _FinishedCampaign)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        starts_before = array.start_calls
        await scanner._run_decloak(hidden, ["Recovered"])
        assert array.stop_calls >= 1
        assert array.start_calls == starts_before + 1


async def test_modal_callback_rechecks_radio_ownership():
    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    app = _ScannerHost(_ScannerArray(hidden))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner._write_log = MagicMock()
        Campaign.active = MagicMock()

        scanner._on_decloak_candidates(hidden, ["Recovered"])

        assert scanner._decloak_task is None
        assert "another campaign" in scanner._write_log.call_args.args[0]


async def test_failure_still_restores_hopping():
    class _FailingArray(_ScannerArray):
        async def stop_hopping(self):
            self.stop_calls += 1
            raise RuntimeError("stop failed")

    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    array = _FailingArray(hidden)
    app = _ScannerHost(array)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        starts_before = array.start_calls

        await scanner._run_decloak(hidden, ["Recovered"])

        assert array.stop_calls == 1
        assert array.start_calls == starts_before + 1


async def test_pbc_window_is_not_consumed_while_decloak_runs():
    pbc = AccessPoint(
        bssid="aa:bb:cc:dd:ee:ff", ssid="PBC", channel=6, wps=True,
        wps_selected_registrar=True, wps_device_password_id=0x0004,
    )
    app = _ScannerHost(_ScannerArray(pbc))
    app.pbc_enabled = True
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner._invade_pbc = AsyncMock()
        scanner._decloak_task = MagicMock()

        scanner._poll_pbc()
        scanner._decloak_task = None
        scanner._poll_pbc()
        await pilot.pause(0)

        scanner._invade_pbc.assert_awaited_once_with(pbc)


async def test_direct_pbc_deferral_rearms_the_open_window():
    pbc = AccessPoint(
        bssid="aa:bb:cc:dd:ee:ff", ssid="PBC", channel=6, wps=True,
        wps_selected_registrar=True, wps_device_password_id=0x0004,
    )
    app = _ScannerHost(_ScannerArray(pbc))
    app.pbc_enabled = True
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner._write_log = MagicMock()
        scanner._invade_pbc = AsyncMock()
        assert scanner._pbc_watcher.new_windows([pbc]) == [pbc]
        scanner._decloak_task = MagicMock()

        scanner._on_pbc_window(pbc)
        scanner._decloak_task = None
        scanner._poll_pbc()
        await pilot.pause(0)

        scanner._invade_pbc.assert_awaited_once_with(pbc)
