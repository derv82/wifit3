"""Error types wifit3 surfaces to the user (in the TUI), not via bare exceptions or logs."""
from __future__ import annotations

import traceback


class WifiteFatalError(Exception):
    """An unrecoverable condition the user must fix before wifit3 can run (e.g. no USB backend).

    Carries a short ``title`` and a multi-line, actionable ``message``; ``trace`` formats the full
    exception chain as plain, pasteable text (no Textual markup) for the fatal-error modal. Raise
    it ``from`` the underlying cause so ``trace`` keeps the real error (e.g. pyusb's NoBackendError).
    """

    def __init__(self, title: str, message: str) -> None:
        self.title = title
        self.message = message
        super().__init__(f"{title}: {message}")

    @property
    def trace(self) -> str:
        return "".join(traceback.format_exception(type(self), self, self.__traceback__))
