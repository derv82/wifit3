from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widget import Widget
from textual.widgets import Label, Button, ProgressBar

from wifit3.models.jobs import JobAction, JobState, ToolStatus
from wifit3.persist.ap_state import PasswordStatus
from wifit3.ui.colors import KEY_CHIP_STYLE

# One colour per status; black text on a solid fill reads as a badge.
_JOB_NAME_W = 25
_JOB_BAR_W = 11
_JOB_PERCENT_W = 7
_JOB_STATUS_W = 11
_JOB_DETAIL_MIN_W = 16
_FULL_BUTTON_W = 12
_COLUMNS_WITHOUT_BAR = _JOB_NAME_W + _JOB_PERCENT_W + 1 + _JOB_STATUS_W + 1
_COLUMNS_WITH_BAR = _COLUMNS_WITHOUT_BAR + 1 + _JOB_BAR_W

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
        self.stops = False
        self._state: tuple[bool, bool] | None = None
        self.sync(job)

    def sync(self, job: JobState) -> None:
        """Match the button's label + colour to the job's current state."""
        state = (JobAction.STOP in job.actions, JobAction.CLEAR in job.actions)
        if state == self._state:
            return
        self._state = state
        self.stops, clears = state
        self.display = self.stops or clears
        self.label = "Stop" if self.stops else "Clear"
        self.variant = "error" if self.stops else "default"


class JobPauseButton(Button):
    def __init__(self, job: JobState):
        super().__init__(classes="job-pause-btn", compact=True)
        self.job_id = job.job_id
        self.resumes = False
        self._state: tuple[bool, bool] | None = None
        self.sync(job)

    def sync(self, job: JobState) -> None:
        state = (JobAction.RESUME in job.actions, JobAction.PAUSE in job.actions)
        if state == self._state:
            return
        self._state = state
        self.resumes, pauses = state
        self.display = self.resumes or pauses
        self.label = "Resume" if self.resumes else "Pause"


class JobSkipButton(Button):
    def __init__(self, job: JobState):
        super().__init__("Skip", classes="job-skip-btn", compact=True, variant="warning")
        self.job_id = job.job_id
        self._visible: bool | None = None
        self.sync(job)

    def sync(self, job: JobState) -> None:
        visible = JobAction.SKIP in job.actions
        if visible == self._visible:
            return
        self._visible = visible
        self.display = visible


class JobRow(Horizontal):
    """One job on a single line: name, progress bar, status badge, detail, skip + action buttons."""

    def __init__(self, job: JobState, compact_name: bool = False):
        super().__init__(classes="job-row")
        self.job_id = job.job_id
        self._job = job
        self._compact_name = compact_name
        self._labels: dict[str, str] = {}
        self._percent: float | None = None

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
        self._update_label(".job-name", self._name_markup(job))
        self._update_label(".job-status", self._status_markup(job))
        self._update_label(".job-detail", self._detail_markup(job))
        percent = job.percent or 0.0
        if percent != self._percent:
            self._percent = percent
            self.query_one(ProgressBar).update(total=100, progress=percent)
        self._update_label(".job-percent", self._percent_text(job))
        self.query_one(JobPauseButton).sync(job)
        self.query_one(JobSkipButton).sync(job)
        self.query_one(JobActionButton).sync(job)

    def set_compact_name(self, compact_name: bool) -> None:
        if self._compact_name == compact_name:
            return
        self._compact_name = compact_name
        if self.is_attached:
            self._update_label(".job-name", self._name_markup(self._job))

    def _update_label(self, selector: str, value: str) -> None:
        if value == self._labels.get(selector):
            return
        self._labels[selector] = value
        self.query_one(selector, Label).update(value)

    def _name_markup(self, job: JobState) -> str:
        name = job.display_name
        if self._compact_name and " (" in name and name.endswith(")"):
            name = name.rsplit(" (", 1)[1][:-1]
            if len(name) > 13:
                name = name[:12] + "…"
        elif len(name) > 25:
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
            status = self.app.vault.manager.key_status(job) or PasswordStatus.CURRENT
            label = "" if status is PasswordStatus.CURRENT else f" [dim]{status.value}[/dim]"
            return f"PSK: [{KEY_CHIP_STYLE[status]}] {escape(job.key)} [/]{label}"
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
    JobTrackerPane .job-name { width: %(name)d; text-align: right; }
    JobTrackerPane .job-bar { width: %(bar)d; height: 1; margin-left: 1; }
    JobTrackerPane .job-bar Bar { width: 1fr; }
    JobTrackerPane .job-bar Bar > .bar--bar { background: $panel; }
    JobTrackerPane .job-percent { width: %(percent)d; margin-right: 1; text-align: right; }
    JobTrackerPane .job-status { width: %(status)d; margin-right: 1; }
    JobTrackerPane .job-detail { width: 1fr; min-width: 0; overflow-x: hidden; }
    JobTrackerPane .job-pause-btn { min-width: 10; margin-right: 1; }
    JobTrackerPane .job-skip-btn { min-width: 8; margin-right: 1; }
    JobTrackerPane .job-action-btn { min-width: 8; }
    """ % {"name": _JOB_NAME_W, "bar": _JOB_BAR_W, "percent": _JOB_PERCENT_W, "status": _JOB_STATUS_W}

    def compose(self) -> ComposeResult:
        yield Vertical(id="job-list")

    def on_mount(self) -> None:
        self._rows: dict[str, JobRow] = {}
        self._jobs: list[JobState] = []
        self._layout_inputs: tuple = ()
        if not hasattr(self.app, "active_jobs"):
            return
        self.watch(self.app, "active_jobs", self._sync)

    def on_resize(self) -> None:
        self._sync_layout()

    def _sync(self, jobs: list[JobState]) -> None:
        try:
            container = self.query_one("#job-list", Vertical)
        except Exception:
            return

        jobs = jobs or []
        self._jobs = jobs
        self.display = bool(jobs)
        self._sync_layout()

        present = {job.job_id for job in jobs}
        for job_id in list(self._rows):
            if job_id not in present:
                self._rows.pop(job_id).remove()

        for job in jobs:
            row = self._rows.get(job.job_id)
            if row is None:
                row = JobRow(job, compact_name=self.has_class("-compact-name"))
                self._rows[job.job_id] = row
                container.mount(row)
            else:
                row.sync(job)

    def _sync_layout(self) -> None:
        inputs = (self.size.width, tuple(frozenset(job.actions) for job in self._jobs))
        if inputs == self._layout_inputs:
            return
        self._layout_inputs = inputs
        normal_required = _COLUMNS_WITH_BAR + _JOB_DETAIL_MIN_W
        compact_required = _COLUMNS_WITH_BAR + _JOB_DETAIL_MIN_W
        compact_name_required = _COLUMNS_WITHOUT_BAR + _JOB_DETAIL_MIN_W
        for job in self._jobs:
            labels: list[tuple[str, int]] = []
            if JobAction.PAUSE in job.actions or JobAction.RESUME in job.actions:
                labels.append(("Resume" if JobAction.RESUME in job.actions else "Pause", 1))
            if JobAction.SKIP in job.actions:
                labels.append(("Skip", 1))
            if JobAction.STOP in job.actions or JobAction.CLEAR in job.actions:
                labels.append(("Stop" if JobAction.STOP in job.actions else "Clear", 0))
            normal_required = max(
                normal_required,
                _COLUMNS_WITH_BAR + _JOB_DETAIL_MIN_W
                + sum(_FULL_BUTTON_W + margin for _, margin in labels),
            )
            compact_action_columns = sum(len(label) + 4 + margin for label, margin in labels)
            compact_required = max(
                compact_required, _COLUMNS_WITH_BAR + _JOB_DETAIL_MIN_W + compact_action_columns)
            compact_name_required = max(
                compact_name_required, _COLUMNS_WITHOUT_BAR + _JOB_DETAIL_MIN_W + compact_action_columns)
        compact_actions = self.size.width < normal_required
        hide_progress = compact_actions and self.size.width < compact_required
        compact_name = hide_progress and self.size.width < compact_name_required
        self.set_class(compact_actions, "-compact-actions")
        self.set_class(hide_progress, "-hide-progress")
        self.set_class(compact_name, "-compact-name")
        for row in self._rows.values():
            row.set_compact_name(compact_name)

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
