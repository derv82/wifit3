from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widget import Widget
from textual.widgets import Label, Button

from wifit3.models.jobs import JobState, ToolStatus


class JobActionButton(Button):
    def __init__(self, job: JobState):
        super().__init__(classes="job-action-btn")
        self.job_id = job.job_id
        self.sync(job)

    def sync(self, job: JobState) -> None:
        """Match the button's label/variant to the job's current state."""
        self.is_active = job.status in (ToolStatus.RUNNING, ToolStatus.QUEUED)
        self.label = "Kill" if self.is_active else "Clear"
        self.variant = "error" if self.is_active else "default"


class JobTrackerPane(Widget):
    """Active-job list inside the Vault drawer. Rows are updated in place so a progress
    tick never tears the list down and rebuilds it (which was the source of the flicker)."""

    DEFAULT_CSS = """
    JobTrackerPane {
        height: auto;
        min-height: 3;
        max-height: 10;
        overflow-y: auto;
        border-top: solid $primary;
        display: none;
    }
    JobTrackerPane .job-row {
        height: 1;
    }
    JobTrackerPane .job-name {
        width: 1fr;
        height: 1;
    }
    JobTrackerPane .job-status {
        width: 12;
        height: 1;
    }
    JobTrackerPane .job-action-btn {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(id="job-list")

    def on_mount(self) -> None:
        self._rows: dict[str, Horizontal] = {}
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
                self._rows[job.job_id] = self._add_row(container, job)
            else:
                self._refresh_row(row, job)

    def _add_row(self, container: Vertical, job: JobState) -> Horizontal:
        row = Horizontal(
            Label(self._name_markup(job), classes="job-name"),
            Label(job.status.value, classes="job-status"),
            JobActionButton(job),
            classes="job-row",
        )
        container.mount(row)
        return row

    def _refresh_row(self, row: Horizontal, job: JobState) -> None:
        row.query_one(".job-name", Label).update(self._name_markup(job))
        row.query_one(".job-status", Label).update(job.status.value)
        row.query_one(JobActionButton).sync(job)

    def _name_markup(self, job: JobState) -> str:
        return f"[bold]{job.display_name}[/bold] - {job.progress_msg}"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if not isinstance(button, JobActionButton):
            return
        if button.is_active:
            self.app.kill_job(button.job_id)
            self.notify("Killing job.")
        else:
            self.app.clear_job(button.job_id)
