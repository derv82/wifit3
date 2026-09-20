from abc import ABC, abstractmethod
from typing import Dict, Any

from wifit3.models import PersistedCapture, ToolCapability, ToolResult

class VaultTool(ABC):
    name: str
    capabilities: ToolCapability

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
    def poll_status(self, tracking_data: Dict[str, Any]) -> ToolResult:
        """Tails the physical log file, or queries remote API (via urllib)."""
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
