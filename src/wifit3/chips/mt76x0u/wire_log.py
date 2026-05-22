"""Live in-driver wire logger for mt76x0u.

Activated by setting the env var `WIFIT3_WIRE_LOG_FILE=/path/to/log.txt`
*before* the driver is instantiated. When unset, every method is a no-op
with zero file I/O — overhead is one attribute check per USB call.

Output format matches `scripts/mt76x0u/mt76x0u_wire_dump.py --no-prefix`
line-for-line, so `diff -u kernel.wire.txt ours.wire.txt` works directly.

Why a separate logger from the existing Python `logging` framework?
Because we want a clean, deterministic, line-per-transaction stream that
diffs against the kernel pcap dump — not interleaved with debug/info
messages, timestamps, retry warnings, etc.
"""
from __future__ import annotations

import os
from typing import Optional


class WireLog:
    """Append-only line writer. Becomes a no-op if no log file is set."""

    def __init__(self, path: Optional[str] = None):
        # If path not given explicitly, check the env var.
        if path is None:
            path = os.environ.get("WIFIT3_WIRE_LOG_FILE")
        self.path = path
        self._fh = None
        if self.path:
            # Line buffering so we can tail -f if needed.
            self._fh = open(self.path, "w", buffering=1, encoding="utf-8")

    @property
    def enabled(self) -> bool:
        return self._fh is not None

    def emit(self, line: str) -> None:
        """Append one line to the log. Cheap no-op when disabled."""
        if self._fh is None:
            return
        self._fh.write(line)
        self._fh.write("\n")

    def marker(self, label: str) -> None:
        """Write a phase marker. Used by driver.connect / set_channel etc.
        so the log can be sliced into phases without an external main.log."""
        if self._fh is None:
            return
        self._fh.write(f"# --- {label} ---\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# Module-level singleton. Constructed at import time so the env var is
# consulted once. Driver / transport code calls WIRE_LOG.emit(...) directly.
WIRE_LOG = WireLog()
