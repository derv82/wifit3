import logging
from abc import abstractmethod
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Generic, Optional, Self, Sequence, TypeVar

from wifit3.crack.hc22000_format import Hc22000Line, read_hc22000
from wifit3.crack.passwords import all_ssid_variants
from wifit3.models import ToolResult, ToolStatus
from wifit3.models.jobs import JobAction, JobState
from wifit3.persist.exhaustion import SSID_VARIANTS_IDENTITY, SSID_VARIANTS_NAME, record_exhausted
from wifit3.vault.tools.base import VaultTool

logger = logging.getLogger(__name__)


def capture_ssid_variants(capture_path: str) -> list[str]:
    """``ssid_variants`` of every ESSID in the capture's hashlines, deduped."""
    return all_ssid_variants(line.ssid for _, line in read_hc22000(capture_path))


@dataclass
class WordlistQueue:
    wordlists: list[str] = field(default_factory=list)
    index: int = 0
    stopped: bool = False
    skipping_wordlist: bool = False
    ssid: str = ""                  # the capture's SSID and BSSID, naming its .meta
    bssid: str = ""
    hashline_ids: list[str] = field(default_factory=list)      # the capture's, at launch
    identities: list[str] = field(default_factory=list)        # per queue entry; "" records nothing

    @classmethod
    def from_job(cls, job: JobState) -> Self:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in job.tool_state.items() if k in known})

    def write_to(self, job: JobState) -> None:
        job.tool_state.update(asdict(self))

    @property
    def current(self) -> Optional[str]:
        return self.wordlists[self.index] if 0 <= self.index < len(self.wordlists) else None

    @property
    def entry_count(self) -> int:
        return len(self.wordlists)

    @property
    def has_next(self) -> bool:
        return self.index + 1 < self.entry_count

    @property
    def chosen_wordlists(self) -> list[str]:
        """The wordlists the job was given, without any file wifit3 wrote for it."""
        return self.wordlists

    @property
    def current_name(self) -> str:
        return Path(self.current).name if self.current else ""

    @property
    def label(self) -> str:
        """Current entry (``rockyou.txt``), with its position when there are several:
        ``rockyou.txt (3/16)``."""
        if self.entry_count > 1:
            return f"{self.current_name} ({self.index + 1}/{self.entry_count})"
        return self.current_name


QueueT = TypeVar("QueueT", bound=WordlistQueue)


class CrackerTool(VaultTool, Generic[QueueT]):
    """A tool that tries a job's wordlists one at a time, persisting its place in ``job.tool_state``."""
    queue_type: type[QueueT]

    @abstractmethod
    def _start_wordlist(self, job: JobState, queue: QueueT) -> None:
        """Start cracking the queue's current wordlist."""

    @abstractmethod
    def _request_stop(self, job: JobState, queue: QueueT) -> bool:
        """Ask the running wordlist to end on its own; whether the request was delivered."""

    @abstractmethod
    def _request_pause(self, job: JobState, queue: QueueT) -> bool:
        """Ask the running wordlist to pause; whether the request was delivered."""

    @abstractmethod
    def _request_resume(self, job: JobState, queue: QueueT) -> bool:
        """Ask the paused wordlist to carry on; whether the request was delivered."""

    @abstractmethod
    def _kill(self, job: JobState) -> None:
        """End the running wordlist now, returning once it has exited."""

    @abstractmethod
    def _running_actions(self, queue: QueueT, progress_msg: str) -> JobAction:
        """What the running job accepts, given its latest progress."""

    def can_crack(self, lines: Sequence[Hc22000Line]) -> bool:
        return bool(lines)

    def apply(self, action: JobAction, job: JobState) -> bool:
        queue = self.queue_type.from_job(job)
        if action is JobAction.PAUSE:
            return self._request_pause(job, queue)
        if action is JobAction.RESUME:
            return self._request_resume(job, queue)
        if action is JobAction.SKIP:
            if not queue.has_next:
                return False
            if self._request_stop(job, queue):
                queue.skipping_wordlist = True
                queue.write_to(job)
                return True
            self._kill(job)
            self._start_next_wordlist(job, queue)
            return True
        if action is JobAction.STOP:
            if queue.stopped:
                return True
            queue.stopped = True
            queue.write_to(job)
            if not self._request_stop(job, queue):
                self._kill(job)
            return True
        return False

    def _wordlist_ended(self, job: JobState, queue: QueueT, exhausted: bool) -> Optional[ToolResult]:
        """The result once the running wordlist has exited, starting the next one when due; None
        when it ended without a stop, a skip or exhausting the wordlist."""
        if queue.stopped:
            return ToolResult(status=ToolStatus.STOPPED, value="Stopped by user")
        if exhausted and not queue.skipping_wordlist:
            self._record_exhausted(queue)
        if queue.skipping_wordlist and queue.has_next:
            queue.skipping_wordlist = False
            self._start_next_wordlist(job, queue)
            return self._running("Starting...", None, queue)
        if exhausted and queue.has_next:
            self._start_next_wordlist(job, queue)
            return self._running("Starting...", None, queue)
        if exhausted:
            return self._exhausted(queue)
        return None

    @staticmethod
    def _record_exhausted(queue: WordlistQueue) -> None:
        for identity in filter(None, queue.identities[queue.index:queue.index + 1]):
            name = SSID_VARIANTS_NAME if identity == SSID_VARIANTS_IDENTITY else queue.current_name
            try:
                record_exhausted(queue.ssid or None, queue.bssid, identity, name, queue.hashline_ids)
            except OSError:
                logger.warning(f"Could not record {identity} as exhausted", exc_info=True)

    def _start_next_wordlist(self, job: JobState, queue: QueueT) -> None:
        queue.index += 1
        self._start_wordlist(job, queue)

    @staticmethod
    def _progress_msg(state: str, percent: float) -> str:
        return f"{state} ({percent:.2f}%)"

    def _running(self, progress_msg: str, percent: Optional[float], queue: QueueT) -> ToolResult:
        return ToolResult(status=ToolStatus.RUNNING,
                          value=self._running_msg(progress_msg, queue),
                          percent=percent,
                          detail=queue.label,
                          actions=self._running_actions(queue, progress_msg))

    @staticmethod
    def _cracked(keys: tuple[str, ...]) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, value=f"Cracked! Keys: {', '.join(keys)}", percent=100.0,
                          keys=keys)

    @classmethod
    def _exhausted(cls, queue: WordlistQueue) -> ToolResult:
        return ToolResult(status=ToolStatus.FAILURE, value=cls._exhausted_msg(queue), percent=100.0)

    @staticmethod
    def _queue_actions(queue: WordlistQueue) -> JobAction:
        return (JobAction.STOP | JobAction.SKIP) if queue.has_next else JobAction.STOP

    @staticmethod
    def _pause_action(progress_msg: str) -> JobAction:
        return JobAction.RESUME if progress_msg.startswith("Paused") else JobAction.PAUSE

    @staticmethod
    def _running_msg(progress_msg: str, queue: WordlistQueue) -> str:
        return f"{progress_msg} · {queue.label}" if queue.label else progress_msg

    @staticmethod
    def _exhausted_msg(queue: WordlistQueue) -> str:
        """What the NOT FOUND badge reports: the one wordlist by name, or how many were tried."""
        wordlists = queue.chosen_wordlists
        if not wordlists:
            return "SSID passwords"
        if len(wordlists) == 1:
            return Path(wordlists[0]).name
        return f"{len(wordlists)} wordlists"
