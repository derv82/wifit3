"""The Scanner logs an automatic job's recovered key and the auto-crack preference state."""
import pytest

from wifit3.models.jobs import JobState, ToolStatus
from wifit3.persist.config import Config
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.scanner import ScannerView


async def _scanner(app: WifiteApp, pilot) -> ScannerView:
    app.push_screen("scanner")
    await pilot.pause(0)
    assert isinstance(app.screen, ScannerView)
    return app.screen


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_logs_the_key_of_a_job_cracked_message():
    app = WifiteApp()
    async with app.run_test() as pilot:
        scanner = await _scanner(app, pilot)
        lines: list = []
        scanner._write_log = lines.append
        job = JobState(job_id="j1", tool_name="cpu_crack", capture_path="x",
                       status=ToolStatus.SUCCESS, progress_msg="Cracked", automatic=True,
                       display_name="CPU Crack (HomeNet)", key="password1")

        scanner.post_message(WifiteApp.JobCracked(job))
        await pilot.pause(0)

        assert len(lines) == 1
        assert '"password1"' in lines[0].plain and "CPU Crack (HomeNet)" in lines[0].plain


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_logs_auto_crack_status_only_when_it_changes(monkeypatch):
    app = WifiteApp()
    async with app.run_test() as pilot:
        scanner = await _scanner(app, pilot)
        lines: list[str] = []
        scanner._write_log = lines.append

        scanner._log_auto_crack_status_when_changed()
        scanner._log_auto_crack_status_when_changed()
        assert len(lines) == 2 and "enabled" in lines[0]

        monkeypatch.setattr(Config, "auto_crack", False)
        scanner._log_auto_crack_status_when_changed()
        assert len(lines) == 3 and "disabled" in lines[2]
