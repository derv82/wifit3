from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widget import Widget
from textual.widgets import Label, Button, ProgressBar

from wifit3.models.jobs import JobAction, JobState, ToolStatus

# One colour per status; black text on a solid fill reads as a badge.
_STATUS_STYLE = {
    ToolStatus.QUEUED: "black bold on white",
    ToolStatus.RUNNING: "black bold on yellow",
    ToolStatus.SUCCESS: "black bold on lightgreen",
    ToolStatus.FAILURE: "black bold on orange",
    ToolStatus.ERROR: "black bold on red",
    ToolStatus.STOPPED: "black bold on bright_white",
}

# Badge text override; a wordlist miss (FAILURE) reads better as "NOT FOUND".
_STATUS_LABEL = {
    ToolStatus.FAILURE: "NOT FOUND",
}


class JobActionButton(Button):
    def __init__(self, job: JobState):
        super().__init__(classes="job-action-btn", compact=True)
        self.job_id = job.job_id
        self.sync(job)

    def sync(self, job: JobState) -> None:
        """Match the button's label + colour to the job's current state."""
        self.stops = JobAction.STOP in job.actions
        self.display = self.stops or JobAction.CLEAR in job.actions
        self.label = "Stop" if self.stops else "Clear"
        self.variant = "error" if self.stops else "default"


class JobPauseButton(Button):
    def __init__(self, job: JobState):
        super().__init__(classes="job-pause-btn", compact=True)
        self.job_id = job.job_id
        self.sync(job)

    def sync(self, job: JobState) -> None:
        self.resumes = JobAction.RESUME in job.actions
        self.display = self.resumes or JobAction.PAUSE in job.actions
        self.label = "Resume" if self.resumes else "Pause"


class JobSkipButton(Button):
    def __init__(self, job: JobState):
        super().__init__("Skip", classes="job-skip-btn", compact=True, variant="warning")
        self.job_id = job.job_id
        self.sync(job)

    def sync(self, job: JobState) -> None:
        self.display = JobAction.SKIP in job.actions


class JobRow(Horizontal):
    """One job on a single line: name, progress bar, status badge, detail, skip + action buttons."""

    def __init__(self, job: JobState):
        super().__init__(classes="job-row")
        self.job_id = job.job_id
        self._job = job

    def compose(self) -> ComposeResult:
        yield Label(self._name_markup(self._job), classes="job-name")
        yield ProgressBar(total=100, show_eta=False, show_percentage=False, classes="job-bar")
        yield Label(self._percent_text(self._job), classes="job-percent")
        yield Label(self._status_markup(self._job), classes="job-status")
        yield Label(self._detail_markup(self._job), classes="job-detail")
        yield JobPauseButton(self._job)
        yield JobSkipButton(self._job)
        yield JobActionButton(self._job)

    def on_mount(self) -> None:
        self.sync(self._job)

    def sync(self, job: JobState) -> None:
        self._job = job
        self.query_one(".job-name", Label).update(self._name_markup(job))
        self.query_one(".job-status", Label).update(self._status_markup(job))
        self.query_one(".job-detail", Label).update(self._detail_markup(job))
        self.query_one(ProgressBar).update(total=100, progress=job.percent or 0.0)
        self.query_one(".job-percent", Label).update(self._percent_text(job))
        self.query_one(JobPauseButton).sync(job)
        self.query_one(JobSkipButton).sync(job)
        self.query_one(JobActionButton).sync(job)

    def _name_markup(self, job: JobState) -> str:
        name = job.display_name
        if len(name) > 25:
            name = name[:24] + "…"
        return f"[bold]{escape(name)}[/bold]"

    def _percent_text(self, job: JobState) -> str:
        return f"{job.percent or 0.0:.2f}%"

    def _status_markup(self, job: JobState) -> str:
        style = _STATUS_STYLE.get(job.status, "black bold on white")
        if job.status == ToolStatus.RUNNING and JobAction.RESUME in job.actions:
            label = "PAUSED"
        elif job.status == ToolStatus.RUNNING and job.percent is None:
            label = "STARTING"
        else:
            label = _STATUS_LABEL.get(job.status, job.status.value)
        return f"[{style}] {label} [/]"

    def _detail_markup(self, job: JobState) -> str:
        if job.status == ToolStatus.SUCCESS and job.key is not None:
            return f"PSK: [black bold on lightgreen] {escape(job.key)} [/]"
        if job.status == ToolStatus.RUNNING:
            return f"[dim]{escape(job.detail)}[/dim]" if job.detail else ""
        if job.status == ToolStatus.QUEUED:
            return ""
        return f"[dim]{escape(job.progress_msg)}[/dim]"


class JobTrackerPane(Widget):
    """Active-job list inside the Vault drawer. Rows are updated in place so a progress
    tick never tears the list down and rebuilds it (which was the source of the flicker)."""

    DEFAULT_CSS = """
    JobTrackerPane {
        height: auto;
        max-height: 8;
        overflow-y: auto;
        border-top: solid $primary;
        display: none;
    }
    JobTrackerPane #job-list { height: auto; }
    JobTrackerPane .job-row { height: 1; }
    JobTrackerPane .job-name { width: 25; text-align: right; }
    JobTrackerPane .job-bar { width: 11; height: 1; margin-left: 1; }
    JobTrackerPane .job-bar Bar { width: 1fr; }
    JobTrackerPane .job-bar Bar > .bar--bar { background: $panel; }
    JobTrackerPane .job-percent { width: 7; margin-right: 1; text-align: right; }
    JobTrackerPane .job-status { width: 11; margin-right: 1; }
    JobTrackerPane .job-detail { width: 1fr; min-width: 0; overflow-x: hidden; }
    JobTrackerPane .job-pause-btn { min-width: 10; margin-right: 1; }
    JobTrackerPane .job-skip-btn { min-width: 8; margin-right: 1; }
    JobTrackerPane .job-action-btn { min-width: 8; }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(id="job-list")

    def on_mount(self) -> None:
        self._rows: dict[str, JobRow] = {}
        if not hasattr(self.app, "active_jobs"):
            return
        self.watch(self.app, "active_jobs", self._sync)

    def _sync(self, jobs: list[JobState]) -> None:
        try:
            container = self.query_one("#job-list", Vertical)
        except Exception:
            return

        jobs = jobs or []
        self.display = bool(jobs)

        present = {job.job_id for job in jobs}
        for job_id in list(self._rows):
            if job_id not in present:
                self._rows.pop(job_id).remove()

        for job in jobs:
            row = self._rows.get(job.job_id)
            if row is None:
                row = JobRow(job)
                self._rows[job.job_id] = row
                container.mount(row)
            else:
                row.sync(job)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if isinstance(button, JobPauseButton):
            if button.resumes:
                self.app.resume_job(button.job_id)
            else:
                self.app.pause_job(button.job_id)
            return
        if isinstance(button, JobSkipButton):
            self.app.skip_job_wordlist(button.job_id)
            return
        if not isinstance(button, JobActionButton):
            return
        if button.stops:
            self.app.stop_job(button.job_id)
        else:
            self.app.clear_job(button.job_id)
