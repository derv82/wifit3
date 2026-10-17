from unittest.mock import patch

import pytest

from wifit3.models.jobs import JobState, ToolStatus
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault_drawer import VaultDrawer
from wifit3.ui.vault.global_tracker import GlobalJobTracker


@pytest.mark.usefixtures("no_usb_devices")
async def test_clicking_the_tracker_opens_the_vault():
    app = WifiteApp()
    with patch.object(WifiteApp, "_poll_jobs"):
        async with app.run_test() as pilot:
            app.active_jobs = [JobState(
                job_id="hashcat-test", tool_name="hashcat", capture_path="test.hc22000",
                status=ToolStatus.RUNNING, progress_msg="Running (0.29%)", percent=0.29,
            )]
            await pilot.pause()

            await pilot.click(GlobalJobTracker)
            await pilot.pause()

            assert isinstance(app.screen, VaultDrawer)
            assert app.vault_open is True
