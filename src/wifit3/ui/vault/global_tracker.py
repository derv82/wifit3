from textual import events
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
        self._last_display: bool | None = None
        self._last_label = ""
        if not hasattr(self.app, "active_jobs"):
            return
        self.watch(self.app, "active_jobs", self._update_tracker)
        if hasattr(self.app, "vault_open"):
            self.watch(self.app, "vault_open", self._update_tracker)
        self._update_tracker()

    def on_click(self, event: events.Click) -> None:
        self.app.action_toggle_vault()

    def _update_tracker(self, *args) -> None:
        """Called automatically when self.app.active_jobs or vault_open is updated."""
        try:
            label = self.query_one("#job-tracker-label", Label)
            jobs = getattr(self.app, "active_jobs", [])
            vault_open = getattr(self.app, "vault_open", False)
        except Exception:
            return
            
        if not jobs or vault_open:
            self._set_tracker(False, "No active jobs.", label)
            return

        running = [j for j in jobs if j.status == ToolStatus.RUNNING]
        queued = [j for j in jobs if j.status == ToolStatus.QUEUED]
        
        if not running and not queued:
            self._set_tracker(False, "", label)
            return
            
        parts = []
        if running:
            job = running[0]
            parts.append(f"⏳ {job.display_name}: {job.progress_msg}")
            if len(running) > 1:
                parts.append(f"(+{len(running)-1} more)")
        if queued:
            parts.append(f"({len(queued)} queued)")
            
        self._set_tracker(True, " ".join(parts), label)

    def _set_tracker(self, display: bool, text: str, label: Label) -> None:
        if display != self._last_display:
            self._last_display = display
            self.display = display
        if text != self._last_label:
            self._last_label = text
            label.update(text)
