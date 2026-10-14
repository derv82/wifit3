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


def test_running_job_without_hashcat_status_shows_starting_badge():
    job = _job(wordlists=["one.txt", "two.txt"])
    row = JobRow(job)

    assert "STARTING" in row._status_markup(job)
    assert JobSkipButton(job).display is False


def test_running_job_with_progress_shows_running_badge():
    job = _job()
    job.progress_msg = "Running (0.00%)"

    badge = JobRow(job)._status_markup(job)

    assert "RUNNING" in badge
    assert "STARTING" not in badge


def test_running_job_with_quit_requested_shows_quitting_badge():
    job = _job()
    job.config["quit_requested"] = True

    badge = JobRow(job)._status_markup(job)

    assert "QUITTING" in badge
    assert "RUNNING" not in badge


def test_quit_job_has_terminal_badge_and_detail():
    job = _job(status=ToolStatus.QUIT)
    job.progress_msg = "Quit by user"
    row = JobRow(job)

    assert "QUIT" in row._status_markup(job)
    assert row._detail_markup(job) == "[dim]Quit by user[/dim]"


def test_force_killed_job_has_red_terminal_badge():
    job = _job(status=ToolStatus.KILLED)
    job.progress_msg = "Force-killed by user"
    row = JobRow(job)

    assert "KILLED" in row._status_markup(job)
    assert "on red" in row._status_markup(job)
    assert row._detail_markup(job) == "[dim]Force-killed by user[/dim]"


def test_skip_button_shows_only_for_running_multiple_wordlists():
    running = _job(wordlists=["one.txt", "two.txt"])
    running.progress_msg = "Running (1.00%) · one.txt (1/2)"
    button = JobSkipButton(running)
    assert button.display is True

    one_wordlist = _job(wordlists=["one.txt"])
    one_wordlist.progress_msg = "Running (1.00%) · one.txt"
    button.sync(one_wordlist)
    assert button.display is False

    queued = _job(status=ToolStatus.QUEUED, wordlists=["one.txt", "two.txt"])
    queued.progress_msg = "Running (1.00%) · one.txt (1/2)"
    button.sync(queued)
    assert button.display is False
