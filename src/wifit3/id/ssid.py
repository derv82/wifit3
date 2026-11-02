"""SSID naming conventions."""
from __future__ import annotations

import re

_SUFFIX_RE = re.compile(
    r"[\s_-]+(?:(?:2[._]?4|5|6)(?:g(?:hz)?)?|guest|visitor|iot|smart)$", re.IGNORECASE)


def strip_suffix(ssid: str) -> str:
    """ssid without its trailing band and network-type tags: "MyRouter-Guest 5G" -> "MyRouter"."""
    stripped = _SUFFIX_RE.sub("", ssid)
    while stripped != ssid:
        ssid = stripped
        stripped = _SUFFIX_RE.sub("", ssid)
    return ssid
