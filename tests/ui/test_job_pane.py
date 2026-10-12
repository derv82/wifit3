"""JobTrackerPane rows: a non-killable in-flight upload stays clearable and surfaces its
retry reason, and a successful upload keeps its summary visible in the detail column."""
import pytest
from textual.app import App

from wifit3.models.jobs import JobState, ToolStatus
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.vault.job_pane import JobActionButton, JobRow


class _Host(App):
    def __init__(self):
        super().__init__()
        self.vault = Vault()


def _job(status, *, tool_name="hashtopolis", msg="", config=None):
    return JobState(job_id="ht_cap_1_1", tool_name=tool_name, capture_path="c",
                    status=status, progress_msg=msg, display_name="hashtopolis (AP)", config=config)


@pytest.mark.asyncio
async def test_queued_remote_job_offers_clear_and_shows_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    app = _Host()
    async with app.run_test() as pilot:
        row = JobRow(_job(ToolStatus.QUEUED, msg="Waiting to submit: offline"))
        await app.mount(row)
        await pilot.pause(0)
        button = row.query_one(JobActionButton)
        assert button.display is True
        assert button.can_kill is False
        assert str(button.label) == "Clear"
        assert "Waiting to submit: offline" in row._detail_markup(row._job)


@pytest.mark.asyncio
async def test_running_killable_job_offers_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    app = _Host()
    async with app.run_test() as pilot:
        row = JobRow(_job(ToolStatus.RUNNING, tool_name="hashcat", msg="Running (5%)"))
        await app.mount(row)
        await pilot.pause(0)
        button = row.query_one(JobActionButton)
        assert button.can_kill is True
        assert str(button.label) == "Kill"


@pytest.mark.asyncio
async def test_upload_success_keeps_summary_in_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    app = _Host()
    async with app.run_test():
        row = JobRow(_job(ToolStatus.SUCCESS,
                          msg="Uploaded 2 hash(es) to Hashtopolis hashlist 'AP'"))
        assert "Uploaded 2 hash(es)" in row._detail_markup(row._job)
        assert "PSK:" not in row._detail_markup(row._job)


def test_name_markup_has_no_sequence_suffix():
    row = JobRow(_job(ToolStatus.QUEUED))
    assert "#" not in row._name_markup(row._job)
