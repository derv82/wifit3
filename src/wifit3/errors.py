"""Error types wifit3 surfaces to the user (in the TUI), not via bare exceptions or logs."""
from __future__ import annotations

import os
import re
import traceback


def _scrub_paths(text: str) -> str:
    """Strip user-identifying absolute paths out of a formatted traceback.

    In-tree frames are trimmed to a wifit3-relative path (``File "...\\wifit3\\..."`` ->
    ``File "wifit3\\..."``, which also shortens them); any path left absolute (stdlib /
    site-packages) has the home directory collapsed to ``~``, so a pasted trace never leaks the
    username.
    """
    text = re.sub(r'(File ")[^"]*?(wifit3[\\/])', r"\1\2", text)
    home = os.path.expanduser("~")
    if home and len(home) > 3 and home != "~":
        text = text.replace(home, "~")
    return text


class BringUpError(Exception):
    """A recoverable failure while bringing up one card's driver (claim, firmware, init, …).

    Per-card and non-fatal — unlike WifiteFatalError: the card is skipped and the user can pick
    another or replug. A driver raises this (instead of logging + returning False) so the splash
    surfaces it in a persistent error label + a toast. ``stage`` names the bring-up phase that
    failed; ``detail`` is the short underlying reason. The UI prepends the card description, so
    don't repeat it here. Raise it ``from`` the underlying cause to keep the real error in logs.
    """

    def __init__(self, stage: str, detail: str = "") -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}" if detail else stage)


class WifiteFatalError(Exception):
    """An unrecoverable condition the user must fix before wifit3 can run (e.g. no USB backend).

    Carries a short ``title`` and a multi-line, actionable ``message``; ``trace`` formats the full
    exception chain as plain, pasteable text (no Textual markup, paths scrubbed of PII) for the
    fatal-error modal. Raise it ``from`` the underlying cause so ``trace`` keeps the real error
    (e.g. pyusb's NoBackendError).
    """

    def __init__(self, title: str, message: str) -> None:
        self.title = title
        self.message = message
        super().__init__(f"{title}: {message}")

    @property
    def trace(self) -> str:
        raw = "".join(traceback.format_exception(type(self), self, self.__traceback__))
        return _scrub_paths(raw)
