"""A ``TRACE`` log level (5, just below ``DEBUG``) for wire-level diagnostics.

Python's logging has no TRACE level. We add one for the ``WIFIT3_LOG=trace`` firehose that
logs every USB transfer (opcode / address / value / calling function) — the preamble we
need to see when a card wedges. It sits *below* DEBUG on purpose so a normal
``WIFIT3_LOG=debug`` run stays readable (no per-transfer spam).

Importing this module patches ``logging.Logger.trace`` once, so any logger can call
``logger.trace(...)``. Guard hot paths with ``logger.isEnabledFor(TRACE)`` — when TRACE is
off (every normal run, and the pcap gate) the call is a cheap level compare, zero work.
"""
from __future__ import annotations

import logging

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self: logging.Logger, msg: str, *args, **kwargs) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, msg, args, **kwargs)


# Patch once (idempotent — re-import is a no-op rebind).
logging.Logger.trace = _trace  # type: ignore[attr-defined]


def level_from_env(value: str) -> int:
    """Map a ``WIFIT3_LOG`` string to a level: trace → TRACE, debug/2 → DEBUG, else INFO."""
    v = value.strip().lower()
    if v == "trace":
        return TRACE
    if v in ("debug", "2"):
        return logging.DEBUG
    return logging.INFO
