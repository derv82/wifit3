from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widget import Widget
from textual.widgets import Label, Button

from wifit3.models.jobs import JobState, ToolStatus

class JobTrackerPane(Widget):
    """Detailed pane inside the Vault drawer listing active jobs."""

    DEFAULT_CSS = """
    JobTrackerPane {
        height: auto;
        max-height: 12;
        border-top: solid $primary;
        display: none;
    }
    .job-row {
        height: 1;
        margin-bottom: 1;
    }
    .job-name {
        width: 1fr;
    }
    .job-status {
        width: 15;
    }
    .job-actions {
        width: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(id="job-list")

    def on_mount(self) -> None:
        if not hasattr(self.app, "active_jobs"):
            return
        self.watch(self.app, "active_jobs", self._update_jobs)
        self._update_jobs(self.app.active_jobs)

    def _update_jobs(self, jobs: list[JobState]) -> None:
        try:
            container = self.query_one("#job-list", Vertical)
        except Exception:
            return

        if not jobs:
            self.display = False
            container.remove_children()
            return

        self.display = True
        
        # Simple rebuild for now
        container.remove_children()
        
        for job in jobs:
            kill_btn = Button("Kill", id=f"kill-{job.job_id}", variant="error", classes="job-kill-btn")
            
            row = Horizontal(
                Label(f"[bold]{job.tool_name}[/bold] - {job.progress_msg}", classes="job-name"),
                Label(job.status.value, classes="job-status"),
                kill_btn,
                classes="job-row"
            )
            container.mount(row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("job-kill-btn"):
            job_id = event.button.id.replace("kill-", "")
            job = self.app.vault.manager.jobs.get(job_id)
            if job and job.status == ToolStatus.RUNNING:
                tool = self.app.vault.manager.tools.get(job.tool_name)
                if tool:
                    tool.kill({'pid': job.pid, 'log_path': job.log_path, 'api_id': job.api_id})
                    self.notify(f"Killed {job.tool_name} job.")
                    # Let the next poll update the status to ERROR/KILLED
