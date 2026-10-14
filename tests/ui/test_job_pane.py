from wifit3.models.jobs import JobState, ToolStatus
from wifit3.ui.vault.job_pane import JobRow, JobSkipButton


def _job(status=ToolStatus.RUNNING, wordlists=None):
    return JobState(
        job_id="hashcat-test",
        tool_name="hashcat",
        capture_path="test.hc22000",
        status=status,
        progress_msg="Running...",
        config={"resolved_wordlists": wordlists or []},
    )


def test_running_job_shows_active_wordlist_in_detail_column():
    job = _job(wordlists=["one.txt", "two.txt"])
    job.progress_msg = "Running (42.00%) · two.txt"

    assert JobRow(job)._detail_markup(job) == "[dim]two.txt[/dim]"


def test_quit_job_has_terminal_badge_and_detail():
    job = _job(status=ToolStatus.QUIT)
    job.progress_msg = "Quit by user"
    row = JobRow(job)

    assert "QUIT" in row._status_markup(job)
    assert row._detail_markup(job) == "[dim]Quit by user[/dim]"


def test_skip_button_shows_only_for_running_multiple_wordlists():
    button = JobSkipButton(_job(wordlists=["one.txt", "two.txt"]))
    assert button.display is True

    button.sync(_job(wordlists=["one.txt"]))
    assert button.display is False

    button.sync(_job(status=ToolStatus.QUEUED, wordlists=["one.txt", "two.txt"]))
    assert button.display is False
