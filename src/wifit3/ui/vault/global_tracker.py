from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label

from wifit3.models.jobs import ToolStatus

class GlobalJobTracker(Widget):
    """Sits above the footer and shows running jobs."""
    
    DEFAULT_CSS = """
    GlobalJobTracker {
        height: 1;
        background: $primary;
        color: $text;
        content-align: center middle;
        display: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("No active jobs.", id="job-tracker-label")

    def on_mount(self) -> None:
        if not hasattr(self.app, "active_jobs"):
            return
        self.watch(self.app, "active_jobs", self._update_tracker)
        if hasattr(self.app, "vault_open"):
            self.watch(self.app, "vault_open", self._update_tracker)
        self._update_tracker()

    def _update_tracker(self, *args) -> None:
        """Called automatically when self.app.active_jobs or vault_open is updated."""
        try:
            label = self.query_one("#job-tracker-label", Label)
            jobs = getattr(self.app, "active_jobs", [])
            vault_open = getattr(self.app, "vault_open", False)
        except Exception:
            return
            
        if not jobs or vault_open:
            self.display = False
            label.update("No active jobs.")
            return

        self.display = True
        running = [j for j in jobs if j.status == ToolStatus.RUNNING]
        queued = [j for j in jobs if j.status == ToolStatus.QUEUED]
        
        parts = []
        if running:
            parts.append(f"Cracking {len(running)} capture{'s' if len(running) != 1 else ''}: {running[0].progress_msg}")
        if queued:
            parts.append(f"({len(queued)} queued)")
            
        label.update(" ".join(parts))
