import contextlib
import threading

import pytest

from wifit3.models import AccessPoint
from wifit3.persist import ap_state
from wifit3.persist.vault import Vault
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault_drawer import VaultDrawer
from textual.worker import WorkerCancelled, WorkerFailed

from tests.ui.wait_until import wait_until


def _write_two_aps() -> None:
    vault = Vault()
    vault.save_wpa_psk("HomeNet", "aa:bb:cc:dd:ee:ff", "password1", manual=True)
    vault.save_wep_key(AccessPoint(bssid="aa:bb:cc:dd:ee:01", ssid="OldNet"), b"abcde", manual=True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_vault_loads_its_selected_ap_then_preloads_the_rest_off_the_ui_thread(mocker):
    _write_two_aps()
    loads: list[tuple[str, bool]] = []
    load_ap_state = ap_state.load_ap_state

    def record_load_and_fail_the_last(bssid, captures):
        loads.append((bssid, threading.current_thread() is threading.main_thread()))
        if len(loads) == 2:
            raise OSError("unreadable")
        return load_ap_state(bssid, captures)
    mocker.patch.object(ap_state, "load_ap_state", side_effect=record_load_and_fail_the_last)

    app = WifiteApp()
    async with app.run_test() as pilot:
        await pilot.pause(0)
        assert loads == []
        drawer = VaultDrawer()
        await app.push_screen(drawer)
        await wait_until(pilot, lambda: drawer.has_class("open") and len(loads) == 2)
        with contextlib.suppress(WorkerFailed):
            await app.workers.wait_for_complete()
        assert app.is_running

    assert loads == [("aa:bb:cc:dd:ee:ff", True), ("aa:bb:cc:dd:ee:01", False)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_cancelling_vault_preload_stops_before_the_next_ap(mocker):
    _write_two_aps()
    app = WifiteApp()
    app.vault.ap_state("aa:bb:cc:dd:ee:ff")
    loads: list[str] = []
    load_ap_state = ap_state.load_ap_state

    def load_then_cancel(bssid, captures):
        loads.append(bssid)
        app.call_from_thread(app.workers.cancel_all)
        return load_ap_state(bssid, captures)
    mocker.patch.object(ap_state, "load_ap_state", side_effect=load_then_cancel)

    async with app.run_test() as pilot:
        drawer = VaultDrawer()
        await app.push_screen(drawer)
        await wait_until(pilot, lambda: drawer.has_class("open") and bool(loads))
        with contextlib.suppress(WorkerCancelled):
            await app.workers.wait_for_complete()

    assert loads == ["aa:bb:cc:dd:ee:01"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_an_unhandled_exception_in_a_timer_is_logged(caplog):
    def explode():
        raise RuntimeError("tick failed")

    app = WifiteApp()
    with pytest.raises(RuntimeError, match="tick failed"):
        async with app.run_test(size=(80, 24)) as pilot:
            app.set_timer(0.01, explode)
            await pilot.pause(0.1)
    logged = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert logged and logged[0].exc_info[1].args == ("tick failed",)
