from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class HashtopolisLedger:
    """Local record of hashline digests already uploaded, keyed by (server, access group)
    so a digest marked done for one group is not wrongly skipped when uploading to another."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.exception(f"Corrupt Hashtopolis ledger {self.path}; starting empty")
            return {}
        if not isinstance(data, dict):
            logger.warning(f"Unexpected Hashtopolis ledger shape in {self.path}; starting empty")
            return {}
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _key(server: str, access_group_id: int) -> str:
        return f"{server}#g{access_group_id}"

    def submitted_digests(self, server: str, access_group_id: int) -> set:
        entry = self._load().get(self._key(server, access_group_id))
        return {str(d) for d in entry} if isinstance(entry, list) else set()

    def record(self, server: str, access_group_id: int, digests: list) -> None:
        key = self._key(server, access_group_id)
        data = self._load()
        existing = data.get(key)
        merged = {str(d) for d in existing} if isinstance(existing, list) else set()
        merged.update(str(d) for d in digests)
        data[key] = sorted(merged)
        self._save(data)
