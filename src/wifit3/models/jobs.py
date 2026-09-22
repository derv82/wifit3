from enum import Enum, IntFlag, auto
from dataclasses import dataclass
from typing import Optional

class ToolCapability(IntFlag):
    NONE = 0
    KILLABLE = auto()
    PAUSABLE = auto()
    RESUMABLE = auto()

class ToolStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR"

@dataclass
class JobState:
    """Schema for jobs.json persistence."""
    job_id: str                 # Unique ID (tool_name + capture_path + timestamp)
    tool_name: str
    capture_path: str
    status: ToolStatus
    progress_msg: str           # e.g., "12% (ETA: 1h)" or error reason
    display_name: str = "Unknown Job"           # Human-readable name (e.g. "hashcat (ASUS)")
    # Tool-specific tracking data (only one of these usually populated)
    pid: Optional[int] = None
    log_path: Optional[str] = None
    api_id: Optional[str] = None
    config: Optional[dict] = None

@dataclass
class ToolResult:
    status: ToolStatus
    value: Optional[str] = None # The progress string or error msg
    result_data: Optional[dict] = None # Extra data (e.g. {"key": "0xdeadbeef"})
