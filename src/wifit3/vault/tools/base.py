from abc import ABC, abstractmethod

from wifit3.models import PersistedCapture, ToolCapability, ToolResult
from wifit3.models.jobs import JobAction, JobState

class VaultTool(ABC):
    name: str
    label: str                  # what the UI calls the tool, e.g. "Hashcat"
    capabilities: ToolCapability

    @abstractmethod
    def can_crack(self, capture: PersistedCapture) -> bool:
        """Return True if this tool can attempt to crack the given capture."""
        pass

    @abstractmethod
    def launch(self, job: JobState, capture: PersistedCapture) -> None:
        """Start the local process (detached, writing to log) or API request for ``job``, reading
        the user's request from ``job.config``. Records how to track the work on the job itself:
        ``pid``/``log_path``/``api_id`` plus anything the tool needs in ``job.tool_state``."""
        pass

    @abstractmethod
    def poll_status(self, job: JobState, assume_dead: bool = False) -> ToolResult:
        """When ``assume_dead`` is set the process is known gone. The result's ``actions``
        are what the job will accept until the next poll."""
        pass

    def apply(self, action: JobAction, job: JobState) -> bool:
        """Carry out one of the ``actions`` the last poll offered; whether it took effect."""
        return False
