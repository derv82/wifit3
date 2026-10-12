from abc import ABC, abstractmethod
from typing import Dict, Any

from wifit3.models import PersistedCapture, ToolCapability, ToolResult


class ToolTransientError(Exception):
    pass


class ToolAlreadySubmitted(Exception):
    pass


class VaultTool(ABC):
    name: str
    capabilities: ToolCapability
    action_label: str | None = None
    requires_config_modal: bool = True

    def prepare_submit_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Hook to enrich a job's config at submit time (e.g. pin a remote target). Default: as-is."""
        return config

    @abstractmethod
    def can_crack(self, capture: PersistedCapture) -> bool:
        """Return True if this tool can attempt to crack the given capture."""
        pass

    @abstractmethod
    def launch(self, capture: PersistedCapture, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes local process (detached, writing to log) or API request.
        Returns tool-specific tracking data (e.g. {'pid': 123, 'log_path': '...'})
        """
        pass

    @abstractmethod
    def poll_status(self, tracking_data: Dict[str, Any], assume_dead: bool = False) -> ToolResult:
        """Tails the physical log file, or queries remote API (via urllib). When
        ``assume_dead`` is set the process is known gone; resolve final status from output."""
        pass

    def kill(self, tracking_data: Dict[str, Any]) -> None:
        """Kill the running job if KILLABLE."""
        pass

    def pause(self, tracking_data: Dict[str, Any]) -> None:
        """Pause the running job if PAUSABLE."""
        pass

    def resume(self, tracking_data: Dict[str, Any]) -> None:
        """Resume the paused job if RESUMABLE."""
        pass
