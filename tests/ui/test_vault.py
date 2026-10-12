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
async def test_toast_on_job_completion(tmp_path):
    from unittest.mock import patch
    from wifit3.models.jobs import JobState, ToolStatus

    def make(status, msg):
        return JobState(job_id="j1", tool_name="hashcat", capture_path="x",
                        status=status, progress_msg=msg, display_name="hashcat (ASUS)")

    app = WifiteApp()
    async with app.run_test():
        job = make(ToolStatus.RUNNING, "Running (5%)")
        app.active_jobs = [job]
        with patch.object(app, "notify") as notify:
            app._notify_completions()          # first sight: record RUNNING, no toast
            notify.assert_not_called()
            job.status = ToolStatus.SUCCESS
            job.progress_msg = "Cracked! Key: 0xdeadbeef"
            app._notify_completions()          # RUNNING -> SUCCESS: toast
            assert notify.call_count == 1
            args, kwargs = notify.call_args
            assert "PSK: 0xdeadbeef" in args[0]
            assert kwargs["title"] == "hashcat (ASUS)"

        # A job already terminal when the app started is not announced.
        app.active_jobs = [JobState(job_id="j2", tool_name="hashcat", capture_path="y",
                                    status=ToolStatus.SUCCESS, progress_msg="Cracked! Key: k",
                                    display_name="hashcat (X)")]
        app._job_status["j2"] = ToolStatus.SUCCESS
        with patch.object(app, "notify") as notify2:
            app._notify_completions()
            notify2.assert_not_called()

        # A newly submitted job which fails on its first poll is announced.
        app.active_jobs = [JobState(job_id="j3", tool_name="hashtopolis", capture_path="z",
                                    status=ToolStatus.ERROR, progress_msg="Authentication failed",
                                    display_name="hashtopolis (X)")]
        with patch.object(app, "notify") as notify3:
            app._notify_completions()
            assert notify3.call_count == 1
            assert "Authentication failed" in notify3.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_auto_submit_success_job_is_cleared_after_toast(tmp_path):
    from unittest.mock import patch
    from wifit3.models.jobs import JobState, ToolStatus
    app = WifiteApp()
    async with app.run_test():
        job = JobState(job_id="ht_auto_1", tool_name="hashtopolis", capture_path="x",
                       status=ToolStatus.SUCCESS, display_name="hashtopolis (AP)",
                       progress_msg="Uploaded 1 hash(es) to Hashtopolis hashlist 'AP'",
                       config={"auto_clear": True})
        app.vault.manager.jobs[job.job_id] = job
        app.active_jobs = [job]
        with patch.object(app, "notify") as notify:
            app._notify_completions()
            assert notify.call_count == 1
        assert job.job_id not in app.vault.manager.jobs
        assert all(j.job_id != job.job_id for j in app.active_jobs)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_manual_success_job_is_retained(tmp_path):
    from unittest.mock import patch
    from wifit3.models.jobs import JobState, ToolStatus
    app = WifiteApp()
    async with app.run_test():
        job = JobState(job_id="ht_manual_1", tool_name="hashtopolis", capture_path="x",
                       status=ToolStatus.SUCCESS, display_name="hashtopolis (AP)",
                       progress_msg="Uploaded 1 hash(es) to Hashtopolis hashlist 'AP'")
        app.vault.manager.jobs[job.job_id] = job
        app.active_jobs = [job]
        with patch.object(app, "notify"):
            app._notify_completions()
        assert job.job_id in app.vault.manager.jobs


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_remote_success_toast_shows_progress_not_key(tmp_path):
    from unittest.mock import patch
    from wifit3.models.jobs import JobState, ToolStatus
    app = WifiteApp()
    async with app.run_test():
        job = JobState(job_id="h1", tool_name="hashtopolis", capture_path="x",
                       status=ToolStatus.RUNNING, progress_msg="Starting...",
                       display_name="hashtopolis (AP)")
        app.active_jobs = [job]
        with patch.object(app, "notify") as notify:
            app._notify_completions()
            job.status = ToolStatus.SUCCESS
            job.progress_msg = "Uploaded 1 hash(es) to Hashtopolis hashlist 'AP'"
            app._notify_completions()
            assert notify.call_count == 1
            args, _ = notify.call_args
            assert "Uploaded 1 hash(es)" in args[0] and "PSK:" not in args[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_open_vault_focuses_ap_list(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        assert app.focused is view.query_one("#vault-aps", DataTable)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_selection_restored_after_reload(tmp_path):
    _write(tmp_path, "Alpha_aa-bb-cc-dd-ee-ff_1000_wep_key.txt",
           "SSID: Alpha\nBSSID: aa:bb:cc:dd:ee:ff\nWEP key (hex): 6162\n")
    _write(tmp_path, "Bravo_11-22-33-44-55-66_1000_wep_key.txt",
           "SSID: Bravo\nBSSID: 11:22:33:44:55:66\nWEP key (hex): 6364\n")
    app = WifiteApp()
    async with app.run_test() as pilot:
        view = await _open_vault(app)
        await pilot.pause()
        table = view.query_one("#vault-aps", DataTable)
        assert table.row_count == 2
        table.move_cursor(row=1)
        await pilot.pause()
        before = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        view.query_one("#vault-table").reload_table()
        await pilot.pause()
        after = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        assert after == before


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
