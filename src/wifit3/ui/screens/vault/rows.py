from __future__ import annotations

import time
from typing import Optional

from textual.content import Content


AUTHORIZED_TOOLTIP = "[$text-success]authorized:[/] PMKID or M3/M4 (the AP accepted this password)"
CHALLENGE_TOOLTIP = "[$text-warning]challenge:[/] No M3/M4 (could be a wrong password)"


def date_text(epoch: Optional[int]) -> str:
    """``today  19:37``, ``Oct 22 19:37``, ``Oct 22 2025``; ``no date`` for None."""
    if epoch is None:
        return "no date"
    moment, now = time.localtime(epoch), time.localtime()
    if moment.tm_year != now.tm_year:
        return time.strftime("%b %d %Y", moment)
    if moment.tm_yday == now.tm_yday:
        return time.strftime("today  %H:%M", moment)
    return time.strftime("%b %d %H:%M", moment)


def verdict_tooltip(authorized: bool) -> str:
    return AUTHORIZED_TOOLTIP if authorized else CHALLENGE_TOOLTIP


def verdict_column(authorized: bool) -> tuple[str, str]:
    """The verdict column's (text, style): ``authorized`` or ``challenge``."""
    if authorized:
        return "authorized", "$text-success"
    return "challenge", "$text-warning"


def row_content(epoch: Optional[int], pair: str, client: str, verdict: tuple[str, str], show_client: bool,
                trailing: list[tuple[str, str]]) -> Content:
    """One history or UNCRACKED row: date, pair, client, verdict, then ``trailing`` (text, style) columns."""
    columns = [(date_text(epoch).ljust(12), "$foreground-muted"), (pair.ljust(5), "")]
    if show_client:
        columns.append((client.ljust(6), "$text-secondary"))
    columns += [(verdict[0].ljust(10), verdict[1]), *trailing]
    return Content(" ").join(Content.styled(text, style) for text, style in columns)
