from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from wifit3.hashtopolis.client import (
    HashtopolisClient,
    HashtopolisConfigError,
    HashtopolisTransientError,
    normalize_url,
)
from wifit3.hashtopolis.ledger import HashtopolisLedger
from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.persist.common import parse_hc22000
from wifit3.persist.config import Config
from wifit3.vault.tools.base import ToolAlreadySubmitted, ToolTransientError, VaultTool

logger = logging.getLogger(__name__)

_UPLOAD_MARKER_PREFIX = "wifit3:"


class HashtopolisTool(VaultTool):
    name = "hashtopolis"
    description = "Upload WPA captures to a Hashtopolis server"
    action_label = "Submit to Hashtopolis"
    requires_config_modal = False
    # The upload is synchronous (one launch does the whole transfer), so there is no process to
    # adopt across restarts and nothing to kill: an empty capability set is the honest description.
    capabilities = ToolCapability.NONE

    def __init__(self, config=Config, client_factory=None, ledger_factory=None) -> None:
        self._config = config
        self._client_factory = client_factory or self._client_from_config
        self._ledger_factory = ledger_factory or (lambda: HashtopolisLedger(self._ledger_path()))

    def can_crack(self, capture: PersistedCapture) -> bool:
        return capture.path.endswith(".hc22000") and self._configured()

    def prepare_submit_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Pin the queued job to the target configured now (sanitized URL, never the raw one),
        so a later URL/group/secrecy change is detected rather than silently redirecting it."""
        config.setdefault("target", {
            "url": _safe_normalize(self._config.hashtopolis_url),
            "access_group_id": self._config.hashtopolis_access_group_id,
            "is_secret": self._config.hashtopolis_trusted_agents_only,
        })
        return config

    def launch(self, capture: PersistedCapture, config: Dict[str, Any]) -> Dict[str, Any]:
        client = self._client_factory(config.get("target"))
        ledger = self._ledger_factory()
        requested = set(config.get("line_digests") or [])
        try:
            return self._upload(client, ledger, capture, requested or None)
        except HashtopolisTransientError as exc:
            raise ToolTransientError(str(exc)) from exc

    def poll_status(self, tracking_data: Dict[str, Any], assume_dead: bool = False) -> ToolResult:
        name = tracking_data.get("hashlist_name") or "Hashtopolis"
        count = tracking_data.get("count") or 0
        warning = tracking_data.get("warning")
        value = f"Uploaded {count} hash(es) to Hashtopolis hashlist '{name}'"
        if warning:
            value += f"; {warning}"
        return ToolResult(status=ToolStatus.SUCCESS, value=value)

    def _upload(self, client: HashtopolisClient, ledger: HashtopolisLedger,
                capture: PersistedCapture, requested: Optional[set]) -> Dict[str, Any]:
        server = client.base_url
        all_lines = _valid_hashlines(capture.path)
        candidate = ([ln for ln in all_lines if line_digest(ln) in requested]
                     if requested is not None else all_lines)
        if not candidate:
            raise ValueError("No valid WPA hashlines to submit")
        access_group_id = client.resolve_access_group()
        submitted = ledger.submitted_digests(server, access_group_id)
        new = [ln for ln in candidate if line_digest(ln) not in submitted]
        if not new:
            raise ToolAlreadySubmitted("Already uploaded to Hashtopolis")
        upload_id = _upload_id(new)
        upload_marker = _UPLOAD_MARKER_PREFIX + upload_id
        name = _hashlist_name(capture, upload_id)
        hashlist_id = client.find_hashlist(upload_marker, access_group_id)
        if hashlist_id is None:
            try:
                hashlist_id = client.create_hashlist(
                    name, new, access_group_id, notes=upload_marker)
            except (HashtopolisTransientError, HashtopolisConfigError, ValueError):
                hashlist_id = client.find_hashlist(upload_marker, access_group_id)
                if hashlist_id is None:
                    raise
        warning = None
        try:
            ledger.record(server, access_group_id, [line_digest(ln) for ln in new])
        except Exception:
            # The upload already succeeded; a ledger write failure (disk or corrupt file) must
            # degrade to a warning, never fail the job.
            logger.exception("Hashtopolis upload confirmed but local upload history was not saved")
            warning = "local upload history could not be saved"
        tracking = {"tool": "hashtopolis", "server": server, "hashlist_id": hashlist_id,
                    "hashlist_name": name, "count": len(new)}
        if warning:
            tracking["warning"] = warning
        return tracking

    def _configured(self) -> bool:
        cfg = self._config
        return bool(cfg.hashtopolis_url and cfg.hashtopolis_token)

    def _client_from_config(self, target: Optional[dict] = None) -> HashtopolisClient:
        cfg = self._config
        if target:
            pinned = target.get("url")
            # The token is always the one configured now; refuse if it no longer belongs to the
            # pinned server, so a server switch cannot send the current token to the old host.
            if pinned and pinned != _safe_normalize(cfg.hashtopolis_url):
                raise HashtopolisConfigError(
                    "Hashtopolis server changed since this upload was queued; not sending the current token")
            url = pinned or cfg.hashtopolis_url
            access_group_id = target.get("access_group_id")
            is_secret = target.get("is_secret", cfg.hashtopolis_trusted_agents_only)
        else:
            url = cfg.hashtopolis_url
            access_group_id = cfg.hashtopolis_access_group_id
            is_secret = cfg.hashtopolis_trusted_agents_only
        # max_retries=0: a launch runs inside the shared poll loop and must fail fast; the per-job
        # backoff in the manager provides the retry spacing.
        return HashtopolisClient(url, token=cfg.hashtopolis_token,
                                 access_group_id=access_group_id, is_secret=is_secret,
                                 max_retries=0)

    def _ledger_path(self) -> Path:
        return Path(self._config.captures_dir) / "hashtopolis.json"


def _safe_normalize(raw: Optional[str]) -> Optional[str]:
    try:
        return normalize_url(raw) if raw else None
    except HashtopolisConfigError:
        return None


def _valid_hashlines(path: str) -> list:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    seen = set()
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not (line.startswith("WPA*01*") or line.startswith("WPA*02*")):
            continue
        if parse_hc22000(line) is None or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return sorted(out)


def line_digest(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()


def _upload_id(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def _hashlist_name(capture: PersistedCapture, upload_id: str) -> str:
    wlan = capture.ssid or capture.bssid or "wifit3"
    return f"{wlan} [wifit3-{upload_id[:12]}]"
