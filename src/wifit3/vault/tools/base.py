from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Sequence

from wifit3.crack.hc22000_format import Hc22000Line, read_hc22000
from wifit3.models import PersistedCapture, ToolCapability, ToolName, ToolResult
from wifit3.models.jobs import JobAction, JobState
from wifit3.persist.config import Config


def jobs_dir() -> Path:
    """Each job's own files, named ``<job_id>.<suffix>``."""
    return Path(Config.captures_dir) / "jobs"


def job_file(job_id: str, suffix: str) -> Path:
    return jobs_dir() / f"{job_id}{suffix}"


def require_hashlines(path: str) -> list[tuple[str, Hc22000Line]]:
    """The file's hashlines; ValueError when it is missing or holds none."""
    lines = read_hc22000(path)
    if not lines:
        raise ValueError(f"No hashlines in {path}")
    return lines


class VaultTool(ABC):
    name: ToolName
    label: str                  # what the UI calls the tool, e.g. "Hashcat"
    capabilities: ToolCapability

    @abstractmethod
    def can_crack(self, lines: Sequence[Hc22000Line]) -> bool:
        """Whether this tool can attempt to crack these hashlines."""
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

    def auto_crack_config(self) -> Optional[dict[str, Any]]:
        """The ``job.config`` auto-crack runs with, from saved settings; None until configured."""
        return None

    def apply(self, action: JobAction, job: JobState) -> bool:
        """Carry out one of the ``actions`` the last poll offered; whether it took effect."""
        return False
