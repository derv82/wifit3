from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Label

from wifit3.models.jobs import JobAction, JobState, ToolStatus
from wifit3.ui.vault.job_pane import (
    JobActionButton, JobPauseButton, JobRow, JobSkipButton, JobTrackerPane,
)


def _job(status=ToolStatus.RUNNING, actions=JobAction.NONE, progress_msg="Running...",
         percent=None, detail="", key=None):
    return JobState(
        job_id="hashcat-test",
        tool_name="hashcat",
        capture_path="test.hc22000",
        status=status,
        progress_msg=progress_msg,
        percent=percent,
        detail=detail,
        key=key,
        actions=actions,
    )


def test_running_job_shows_active_wordlist_in_detail_column():
    job = _job(progress_msg="Running (42.00%) · two.txt (2/2)", percent=42.0,
               detail="two.txt (2/2)")

    assert JobRow(job)._detail_markup(job) == "[dim]two.txt (2/2)[/dim]"


def test_a_paused_job_reads_as_paused():
    job = _job(actions=JobAction.STOP | JobAction.RESUME, progress_msg="Paused (12.00%)",
               percent=12.0)

    assert "PAUSED" in JobRow(job)._status_markup(job)


def test_a_cracked_job_shows_its_key():
    job = _job(status=ToolStatus.SUCCESS, actions=JobAction.CLEAR,
               progress_msg="Cracked! Key: hunter2", percent=100.0, key="hunter2")
    row = JobRow(job)

    assert "SUCCESS" in row._status_markup(job)
    assert "hunter2" in row._detail_markup(job)


def test_a_cracked_job_saved_without_its_key_shows_the_status_line():
    job = _job(status=ToolStatus.SUCCESS, actions=JobAction.CLEAR,
               progress_msg="Cracked! Key: hunter2")

    assert JobRow(job)._detail_markup(job) == "[dim]Cracked! Key: hunter2[/dim]"


def test_running_job_without_tool_status_shows_starting_badge():
    job = _job()
    row = JobRow(job)

    assert "STARTING" in row._status_markup(job)


def test_running_job_with_progress_shows_running_badge():
    job = _job(progress_msg="Running (0.00%)", percent=0.0)

    badge = JobRow(job)._status_markup(job)

    assert "RUNNING" in badge
    assert "STARTING" not in badge


def test_stopped_job_has_terminal_badge_and_detail():
    job = _job(status=ToolStatus.STOPPED, actions=JobAction.CLEAR,
               progress_msg="Stopped by user")
    row = JobRow(job)

    assert "STOPPED" in row._status_markup(job)
    assert row._detail_markup(job) == "[dim]Stopped by user[/dim]"


def test_exhausted_job_reads_as_not_found():
    job = _job(status=ToolStatus.FAILURE, actions=JobAction.CLEAR, progress_msg="3 wordlists")
    row = JobRow(job)

    assert "NOT FOUND" in row._status_markup(job)
    assert row._detail_markup(job) == "[dim]3 wordlists[/dim]"


def test_skip_button_follows_the_jobs_offered_actions():
    skippable = _job(actions=JobAction.STOP | JobAction.SKIP)
    button = JobSkipButton(skippable)
    assert button.display is True

    button.sync(_job(actions=JobAction.STOP))
    assert button.display is False

    button.sync(_job(status=ToolStatus.QUEUED, actions=JobAction.CLEAR))
    assert button.display is False


def test_pause_button_offers_resume_to_a_paused_job_and_nothing_to_an_unreachable_one():
    button = JobPauseButton(_job(actions=JobAction.STOP | JobAction.PAUSE))
    assert button.display is True
    assert str(button.label) == "Pause"

    button.sync(_job(actions=JobAction.STOP | JobAction.RESUME))
    assert button.resumes is True
    assert str(button.label) == "Resume"

    button.sync(_job(actions=JobAction.STOP))
    assert button.display is False


class JobPaneApp(App):
    """Just the tracker pane, with the app-wide button minimum used by Wifit3."""

    CSS = """
    Button { min-width: 12; margin-right: 1; }
    JobTrackerPane.-compact-actions .job-pause-btn,
    JobTrackerPane.-compact-actions .job-skip-btn,
    JobTrackerPane.-compact-actions .job-action-btn { min-width: 0; width: auto; }
    JobTrackerPane.-hide-progress .job-bar { display: none; }
    JobTrackerPane.-compact-name .job-name { width: 13; }
    """
    active_jobs: reactive[list[JobState]] = reactive([], always_update=True)

    def compose(self) -> ComposeResult:
        yield JobTrackerPane()


async def test_the_bar_fills_its_box_and_the_percentage_has_two_decimals():
    app = JobPaneApp()
    async with app.run_test(size=(120, 24)) as pilot:
        app.active_jobs = [_job(actions=JobAction.STOP,
                                progress_msg="Running (0.29%) \u00b7 rockyou.txt",
                                percent=0.29, detail="rockyou.txt")]
        await pilot.pause()

        assert app.query_one("#bar").size.width == 11
        assert str(app.query_one(".job-percent", Label).render()) == "0.29%"
        assert app.query_one(".job-name", Label).styles.text_align == "right"


def test_compact_name_hides_the_cracking_tool_prefix():
    job = _job()
    job.display_name = "Hashcat (HomeNet)"
    row = JobRow(job, compact_name=True)

    assert row._name_markup(job) == "[bold]HomeNet[/bold]"
    job.display_name = "Hashcat (VeryLongAccessPointName)"
    assert row._name_markup(job) == "[bold]VeryLongAcce…[/bold]"


async def test_job_buttons_shrink_when_detail_would_have_less_than_16_columns():
    job = _job(
        actions=JobAction.STOP | JobAction.PAUSE | JobAction.SKIP,
        percent=42.0,
        detail="a-very-long-wordlist-name.txt",
    )

    narrow_app = JobPaneApp()
    async with narrow_app.run_test(size=(110, 24)) as pilot:
        narrow_app.active_jobs = [job]
        await pilot.pause()
        narrow_row = narrow_app.query_one(JobRow)
        narrow_pane = narrow_app.query_one(JobTrackerPane)
        assert narrow_pane.has_class("-compact-actions")
        assert not narrow_pane.has_class("-hide-progress")
        assert narrow_row.query_one(".job-bar").display is True
        assert all(narrow_row.query_one(button_type).size.width < 12 for button_type in (
            JobPauseButton, JobSkipButton, JobActionButton))

    compressed_app = JobPaneApp()
    async with compressed_app.run_test(size=(95, 24)) as pilot:
        compressed_app.active_jobs = [job]
        await pilot.pause()
        compressed_row = compressed_app.query_one(JobRow)
        compressed_pane = compressed_app.query_one(JobTrackerPane)
        assert compressed_pane.has_class("-compact-actions")
        assert compressed_pane.has_class("-hide-progress")
        assert not compressed_pane.has_class("-compact-name")
        assert compressed_row.query_one(".job-bar").display is False
        assert compressed_row.query_one(".job-percent").display is True

    name_compact_app = JobPaneApp()
    async with name_compact_app.run_test(size=(85, 24)) as pilot:
        name_compact_app.active_jobs = [job]
        await pilot.pause()
        name_compact_row = name_compact_app.query_one(JobRow)
        name_compact_pane = name_compact_app.query_one(JobTrackerPane)
        assert name_compact_pane.has_class("-compact-actions")
        assert name_compact_pane.has_class("-hide-progress")
        assert name_compact_pane.has_class("-compact-name")
        assert name_compact_row.query_one(".job-name", Label).size.width == 13
        assert "Hashcat" not in str(name_compact_row.query_one(".job-name", Label).render())

    wide_app = JobPaneApp()
    async with wide_app.run_test(size=(130, 24)) as pilot:
        wide_app.active_jobs = [job]
        await pilot.pause()
        wide_row = wide_app.query_one(JobRow)
        wide_pane = wide_app.query_one(JobTrackerPane)
        assert not wide_pane.has_class("-compact-actions")
        assert not wide_pane.has_class("-hide-progress")
        assert not wide_pane.has_class("-compact-name")
        assert wide_row.query_one(".job-bar").display is True
        assert wide_row.query_one(".job-detail", Label).size.width >= 16
        assert all(wide_row.query_one(button_type).size.width >= 12 for button_type in (
            JobPauseButton, JobSkipButton, JobActionButton))


async def test_most_constrained_job_sets_the_layout_for_every_row():
    many_actions = _job(actions=JobAction.STOP | JobAction.PAUSE | JobAction.SKIP)
    one_action = _job(actions=JobAction.STOP)
    one_action.job_id = "cpu-test"
    app = JobPaneApp()
    async with app.run_test(size=(95, 24)) as pilot:
        app.active_jobs = [many_actions, one_action]
        await pilot.pause()

        pane = app.query_one(JobTrackerPane)
        assert pane.has_class("-compact-actions") and pane.has_class("-hide-progress")
        assert all(not bar.display for bar in pane.query(".job-bar"))
        assert all(button.size.width < 12 for button in pane.query(JobActionButton))


def test_action_button_stops_a_running_job_and_clears_a_finished_one():
    running = _job(actions=JobAction.STOP)
    button = JobActionButton(running)
    assert button.stops is True
    assert str(button.label) == "Stop"

    button.sync(_job(status=ToolStatus.SUCCESS, actions=JobAction.CLEAR))
    assert button.stops is False
    assert str(button.label) == "Clear"


def test_action_button_is_hidden_while_a_job_offers_nothing():
    button = JobActionButton(_job(actions=JobAction.NONE))

    assert button.display is False
