"""Host detection for the few places where Termux/Android differs from desktop Linux."""
from __future__ import annotations

import os
import sys


def is_termux() -> bool:
    """True inside Termux on Android, whose Python still reports ``sys.platform == "linux"``."""
    if sys.platform == "android":
        return True
    if os.environ.get("TERMUX_VERSION"):
        return True
    return os.environ.get("PREFIX", "").endswith("/com.termux/files/usr")
