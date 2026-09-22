import re

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widget import Widget
from textual.widgets import Label, Button, ProgressBar

from wifit3.models.jobs import JobState, ToolStatus

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# One colour per status; black text on a solid fill reads as a badge.
_STATUS_STYLE = {
    ToolStatus.QUEUED: "black bold on white",
    ToolStatus.RUNNING: "black bold on yellow",
    ToolStatus.SUCCESS: "black bold on lightgreen",
    ToolStatus.FAILURE: "black bold on orange",
    ToolStatus.ERROR: "black bold on red",
}

# Badge text override; a wordlist miss (FAILURE) reads better as "NOT FOUND".
_STATUS_LABEL = {
    ToolStatus.FAILURE: "NOT FOUND",
}


class JobActionButton(Button):
    def __init__(self, job: JobState):
        super().__init__(classes="job-action-btn")
        self.job_id = job.job_id
        self.sync(job)

    def sync(self, job: JobState) -> None:
        """Match the button's label + colour to the job's current state (kept flat, no fill)."""
        self.is_active = job.status in (ToolStatus.RUNNING, ToolStatus.QUEUED)
        self.label = "Kill" if self.is_active else "Clear"
        self.set_class(self.is_active, "kill")
        self.set_class(not self.is_active, "clear")


class JobRow(Horizontal):
    """One job on a single line: name, status badge, detail, progress bar, action button."""

    def __init__(self, job: JobState):
        super().__init__(classes="job-row")
        self.job_id = job.job_id
        self._job = job

    def compose(self) -> ComposeResult:
        yield Label(self._name_markup(self._job), classes="job-name")
        yield ProgressBar(total=100, show_eta=False, classes="job-bar")
        yield Label(self._status_markup(self._job), classes="job-status")
        yield Label(self._detail_markup(self._job), classes="job-detail")
        yield JobActionButton(self._job)

    def on_mount(self) -> None:
        self.sync(self._job)

    def sync(self, job: JobState) -> None:
        self._job = job
        self.query_one(".job-name", Label).update(self._name_markup(job))
        self.query_one(".job-status", Label).update(self._status_markup(job))
        self.query_one(".job-detail", Label).update(self._detail_markup(job))
        self.query_one(ProgressBar).update(total=100, progress=self._percent(job))
        self.query_one(JobActionButton).sync(job)

    def _name_markup(self, job: JobState) -> str:
        name = job.display_name
        if len(name) > 25:
            name = name[:24] + "…"
        return f"[bold]{escape(name)}[/bold]"

    def _status_markup(self, job: JobState) -> str:
        style = _STATUS_STYLE.get(job.status, "black bold on white")
        label = _STATUS_LABEL.get(job.status, job.status.value)
        return f"[{style}] {label} [/]"

    def _detail_markup(self, job: JobState) -> str:
        if job.status == ToolStatus.SUCCESS:
            key = self._cracked_key(job.progress_msg)
            return f"PSK: [black bold on lightgreen] {escape(key)} [/]" if key else ""
        if job.status in (ToolStatus.FAILURE, ToolStatus.ERROR):
            return f"[dim]{escape(job.progress_msg)}[/dim]"
        return ""

    def _cracked_key(self, msg: str) -> str:
        return msg.split("Key:", 1)[1].strip() if msg and "Key:" in msg else ""

    def _percent(self, job: JobState) -> float:
        if job.status == ToolStatus.SUCCESS:
            return 100.0
        m = _PERCENT_RE.search(job.progress_msg or "")
        return float(m.group(1)) if m else 0.0


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
    JobTrackerPane .job-name { width: 25; }
    JobTrackerPane .job-bar { width: 18; height: 1; margin: 0 1; }
    JobTrackerPane .job-status { width: 11; margin-right: 1; }
    JobTrackerPane .job-detail { width: 1fr; }
    JobTrackerPane .job-action-btn {
        height: 1; min-height: 1; min-width: 8;
        border: none; background: $background; color: $foreground;
    }
    JobTrackerPane .job-action-btn.kill { color: $error; }
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
        if not isinstance(button, JobActionButton):
            return
        if button.is_active:
            self.app.kill_job(button.job_id)
            self.notify("Killing job.")
        else:
            self.app.clear_job(button.job_id)
