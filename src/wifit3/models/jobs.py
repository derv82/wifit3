from enum import Enum, IntFlag, auto
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

class ToolCapability(IntFlag):
    NONE = 0
    KILLABLE = auto()
    SINGLETON = auto()      # only one instance of this tool may run at a time
    ADOPTABLE = auto()      # process outlives the app; adopt it on relaunch, don't kill on exit

class JobAction(IntFlag):
    """What a job will accept right now; recomputed on every poll, never persisted."""
    NONE = 0
    STOP = auto()
    SKIP = auto()
    CLEAR = auto()
    PAUSE = auto()
    RESUME = auto()

class ToolStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR"
    STOPPED = "STOPPED"

@dataclass
class JobState:
    """Schema for jobs.json persistence."""
    job_id: str                 # Unique ID (tool_name + capture_path + timestamp)
    tool_name: str
    capture_path: str
    status: ToolStatus
    progress_msg: str           # e.g., "12% (ETA: 1h)" or error reason
    display_name: str = "Unknown Job"           # Human-readable name (e.g. "Hashcat (ASUS)")
    percent: Optional[float] = None             # how far through the current work, once known
    detail: str = ""                            # what the work is on, e.g. "rockyou.txt (2/3)"
    key: Optional[str] = None                   # recovered PSK; None for tools that recover none
    automatic: bool = False                     # submitted by wifit3 on capture, not by the user
    # Tool-specific tracking data (only one of these usually populated)
    pid: Optional[int] = None
    log_path: Optional[str] = None
    api_id: Optional[str] = None
    config: dict[str, Any] = field(default_factory=dict)        # launch request from the tool's modal
    tool_state: dict[str, Any] = field(default_factory=dict)    # tool-owned; no caller reads inside it
    actions: JobAction = JobAction.NONE                         # derived each poll, not persisted

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        del data["actions"]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        """Build a job from its persisted form, ignoring keys this version no longer has."""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["status"] = ToolStatus(data["status"])
        for name in ("config", "tool_state"):
            if kwargs.get(name) is None:
                kwargs[name] = {}
        return cls(**kwargs)

@dataclass
class ToolResult:
    status: ToolStatus
    value: Optional[str] = None # The progress string or error msg
    percent: Optional[float] = None
    detail: str = ""
    key: Optional[str] = None                   # recovered PSK; None for tools that recover none
    actions: JobAction = JobAction.NONE

    def __str__(self) -> str:
        return (f"status={self.status.value}, value={self.value}, percent={self.percent}, "
                f"detail={self.detail}, key={self.key}, actions={self.actions.name}")
