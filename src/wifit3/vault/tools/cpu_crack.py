import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import Hc22000Line, parse_hc22000
from wifit3.crack.passwords import DEFAULT_PASSWORDS_PATH, read_passwords, ssid_passwords
from wifit3.dot11 import wpa
from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.models.jobs import JobAction, JobState, ToolName
from wifit3.persist.config import Config
from wifit3.vault.exhaustion import (SSID_VARIANTS_IDENTITY, capture_hashline_ids,
                                     wordlist_identity)
from wifit3.vault.tools.cracker import (CrackerTool, WordlistQueue, capture_ssid_variants,
                                        expand_wordlists)


logger = logging.getLogger(__name__)


@dataclass
class CpuCrackQueue(WordlistQueue):
    ssid_passwords_index: Optional[int] = None  # queue entry of the in-memory SSID passwords

    @property
    def on_ssid_passwords(self) -> bool:
        return self.index == self.ssid_passwords_index

    @property
    def entry_count(self) -> int:
        return len(self.wordlists) + (0 if self.ssid_passwords_index is None else 1)

    @property
    def current(self) -> Optional[str]:
        if self.on_ssid_passwords:
            return None
        wordlist_index = self.index
        if self.ssid_passwords_index is not None and self.index > self.ssid_passwords_index:
            wordlist_index -= 1
        return self.wordlists[wordlist_index] if 0 <= wordlist_index < len(self.wordlists) else None

    @property
    def current_name(self) -> str:
        return "SSID passwords" if self.on_ssid_passwords else super().current_name


@dataclass
class _WordlistRun:
    wordlist: Optional[Path]                    # None while trying the SSID passwords
    ssid_passwords: list[str]
    lines_by_essid: dict[bytes, list[Hc22000Line]]
    passwords_tried: int = 0
    passwords_total: int = 0
    key: Optional[str] = None
    exhausted: bool = False
    error: Optional[str] = None
    stop_requested: threading.Event = field(default_factory=threading.Event)
    unpaused: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread = field(init=False)

    def request_stop(self) -> None:
        self.stop_requested.set()
        self.unpaused.set()


def _lines_by_essid(capture_path: str) -> dict[bytes, list[Hc22000Line]]:
    by_essid: dict[bytes, list[Hc22000Line]] = {}
    for raw in Path(capture_path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = parse_hc22000(raw)
        if line is not None:
            by_essid.setdefault(bytes.fromhex(line.essid), []).append(line)
    return by_essid


def _ap_ssid_variants(job: JobState) -> list[str]:
    if not job.config.get("ap_ssid_passwords", True):
        return []
    return capture_ssid_variants(job.capture_path)


def _ssid_passwords(job: JobState, ap_ssid_variants: list[str]) -> list[str]:
    probed_ssid_passwords = ssid_passwords(job.config.get("extra_passwords", []))
    return list(dict.fromkeys(ap_ssid_variants + probed_ssid_passwords))


class CpuCrackTool(CrackerTool[CpuCrackQueue]):
    name = ToolName.CPU_CRACK
    label = "CPU Crack"
    description = "Built-in CPU cracker (WPA/WPA2/PMKID)"
    capabilities = ToolCapability.KILLABLE | ToolCapability.SINGLETON
    queue_type = CpuCrackQueue

    def __init__(self) -> None:
        self._runs: Dict[str, _WordlistRun] = {}

    def auto_crack_config(self) -> Optional[dict[str, Any]]:
        settings = Config.cpu_crack
        if not (settings.wordlists or settings.default_passwords or settings.ap_ssid_passwords
                or settings.probed_ssid_passwords):
            return None
        return {"wordlists": list(settings.wordlists),
                "default_passwords": settings.default_passwords,
                "ap_ssid_passwords": settings.ap_ssid_passwords,
                "probed_ssid_passwords": settings.probed_ssid_passwords}

    def launch(self, job: JobState, capture: PersistedCapture) -> None:
        requested = job.config.get("wordlists") or []
        wordlists = expand_wordlists(requested)
        if requested and not wordlists:
            raise ValueError(f"Wordlist not found: {', '.join(requested)}")
        if job.config.get("default_passwords"):
            wordlists.insert(0, str(DEFAULT_PASSWORDS_PATH.resolve()))
        wordlists = list(dict.fromkeys(wordlists))
        queue = CpuCrackQueue(wordlists=wordlists, ssid=capture.ssid or "", bssid=capture.bssid,
                              hashline_ids=capture_hashline_ids(job.capture_path),
                              identities=[wordlist_identity(path) for path in wordlists])
        ap_ssid_variants = _ap_ssid_variants(job)
        if _ssid_passwords(job, ap_ssid_variants):
            queue.ssid_passwords_index = 1 if job.config.get("default_passwords") else 0
            queue.identities.insert(queue.ssid_passwords_index,
                                    SSID_VARIANTS_IDENTITY if ap_ssid_variants else "")
        if not queue.entry_count:
            raise ValueError("No passwords to try: choose a wordlist or a password source")
        self._start_wordlist(job, queue)

    def _start_wordlist(self, job: JobState, queue: CpuCrackQueue) -> None:
        lines_by_essid = _lines_by_essid(job.capture_path)
        if not lines_by_essid:
            raise ValueError(f"No hashlines in {job.capture_path}")
        run = _WordlistRun(
            wordlist=Path(queue.current) if queue.current else None,
            ssid_passwords=(_ssid_passwords(job, _ap_ssid_variants(job))
                            if queue.on_ssid_passwords else []),
            lines_by_essid=lines_by_essid)
        run.unpaused.set()
        run.thread = threading.Thread(target=self._crack, args=(run,), daemon=True,
                                      name=f"cpu_crack {queue.label}")
        self._runs[job.job_id] = run
        queue.write_to(job)
        run.thread.start()
        logger.info(f"cpu_crack running for {queue.label}")

    def _crack(self, run: _WordlistRun) -> None:
        try:
            run.passwords_total = sum(1 for _ in self._passwords(run))
            for psk in self._passwords(run):
                run.unpaused.wait()
                if run.stop_requested.is_set():
                    return
                if self._psk_cracks(psk, run.lines_by_essid):
                    run.key = psk
                    return
                run.passwords_tried += 1
            run.exhausted = True
        except Exception as exc:
            logger.exception(f"cpu_crack failed on {run.wordlist or 'SSID passwords'}")
            run.error = str(exc)

    @staticmethod
    def _passwords(run: _WordlistRun) -> Iterator[str]:
        return read_passwords(run.wordlist) if run.wordlist is not None else iter(run.ssid_passwords)

    @staticmethod
    def _psk_cracks(psk: str, lines_by_essid: dict[bytes, list[Hc22000Line]]) -> bool:
        for essid, lines in lines_by_essid.items():
            pmk = wpa.derive_pmk(psk, essid)
            if any(wpa_psk.psk_matches(pmk, line) for line in lines):
                return True
        return False

    def poll_status(self, job: JobState, assume_dead: bool = False) -> ToolResult:
        queue = CpuCrackQueue.from_job(job)
        run = self._runs.get(job.job_id)
        if run is not None and run.key is not None:
            del self._runs[job.job_id]
            return self._cracked(run.key)
        if run is not None and not assume_dead and run.thread.is_alive():
            percent = run.passwords_tried / max(run.passwords_total, 1) * 100
            state = "Running" if run.unpaused.is_set() else "Paused"
            return self._running(self._progress_msg(state, percent), percent, queue)

        self._runs.pop(job.job_id, None)
        ended = self._wordlist_ended(job, queue, exhausted=run is not None and run.exhausted)
        if ended is not None:
            return ended
        reason = run.error if run is not None else None
        return ToolResult(status=ToolStatus.ERROR, value=reason or "cpu_crack ended without recovering the key")

    def _live_run(self, job: JobState) -> Optional[_WordlistRun]:
        run = self._runs.get(job.job_id)
        return run if run is not None and run.thread.is_alive() else None

    def _request_stop(self, job: JobState, queue: CpuCrackQueue) -> bool:
        run = self._live_run(job)
        if run is None:
            return False
        run.request_stop()
        return True

    def _request_pause(self, job: JobState, queue: CpuCrackQueue) -> bool:
        run = self._live_run(job)
        if run is None:
            return False
        run.unpaused.clear()
        return True

    def _request_resume(self, job: JobState, queue: CpuCrackQueue) -> bool:
        run = self._live_run(job)
        if run is None:
            return False
        run.unpaused.set()
        return True

    def _kill(self, job: JobState) -> None:
        run = self._runs.get(job.job_id)
        if run is not None:
            run.request_stop()
            run.thread.join()

    def _running_actions(self, queue: CpuCrackQueue, progress_msg: str) -> JobAction:
        if queue.stopped or queue.skipping_wordlist:
            return JobAction.STOP
        return self._queue_actions(queue) | self._pause_action(progress_msg)
