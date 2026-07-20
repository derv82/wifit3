"""Rich-markup rendering for the active PMKID harvest, as a 4-way-handshake-style tree.

Built from per-line fragments (a ``PMKID · Harvesting from <essid>`` header, the
auth/assoc/M1 action branches, and the terminal verdict) so the SAME builders
serve two emit modes: composed into one list for an atomic dump today, and called
one-at-a-time on live progress events once the harvest becomes a campaign. Pure +
UI-free (testable without a terminal); the screen owns side effects (save, log).

Field ticks (``PMKID✓/✗``) reuse the 4-way handshake's glyphs; the heavy ``╳``
is reserved for the terminal give-up connector (``treelog.leaf_fail``).
"""
from __future__ import annotations

from typing import Optional

from wifit3.campaigns import treelog
from wifit3.campaigns.pmkid import PmkidFail

_M1_DIR = "AP→Client"
_REQ_DIR = "Client→AP"
_EXTRACTED = "[black bold on green] ✓ PMKID Extracted [/black bold on green]"

# reason -> (bold-orange headline, dim parenthetical why).
_FAIL_LEAF = {
    PmkidFail.NO_KDE:       ("No PMKID in M1", "AP does not send PMKID in M1"),
    PmkidFail.NO_RESPONSE:  ("No M1 received", "AP did not respond"),
    PmkidFail.PMF_REQUIRED: ("PMF Required", "AP only associates protected clients"),
    PmkidFail.NO_PSK_AKM:   ("No PSK AKM", "nothing to harvest, e.g. SAE-only"),
}

# Reasons reached only AFTER we transmit Auth+Assoc, so their tree echoes those
# branches. PMF / NO_PSK bail before any TX → header + leaf only.
_AFTER_TX = (PmkidFail.NO_RESPONSE, PmkidFail.NO_KDE)


def header(essid: str) -> str:
    return (f"[bold]PMKID[/bold] [dim]·[/dim] [bold]Harvesting[/bold] from "
            f"[bold cyan]{essid}[/bold cyan]")


def auth_req() -> str:
    return treelog.branch(f"Auth request ({_REQ_DIR})")


def assoc_req() -> str:
    return treelog.branch(f"Assoc. request ({_REQ_DIR})")


def m1(has_pmkid: bool) -> str:
    tick = "[green]✓[/green]" if has_pmkid else "[red]✗[/red]"
    return treelog.branch(f"M1 Message ({_M1_DIR}) PMKID{tick}")


def _fail_leaf(reason: Optional[PmkidFail]) -> str:
    head, why = _FAIL_LEAF.get(reason, ("Harvest failed", ""))
    tail = f" [dim]({why})[/dim]" if why else ""
    return treelog.leaf_fail(f"[bold orange1]{head}[/bold orange1]{tail}")


def verdict_success(save_hint: Optional[str]) -> list[str]:
    """The terminal success line(s) only: the ``✓ PMKID Extracted`` chip closed by
    the ``saved/exists: …`` leaf (or the chip alone as the closing leaf when
    ``save_hint`` is None). Emitted on its own once progress streams live."""
    if save_hint:
        return [treelog.branch(_EXTRACTED), treelog.leaf(save_hint)]
    return [treelog.leaf(_EXTRACTED)]


def verdict_failure(reason: Optional[PmkidFail]) -> list[str]:
    """The terminal give-up line only (bold-orange ``└─╳`` leaf). Emitted on its own
    once the auth/assoc/M1 branches stream live."""
    return [_fail_leaf(reason)]


def render_success(essid: str, save_hint: Optional[str]) -> list[str]:
    """Header → auth → assoc → ``M1 … PMKID✓`` → the success verdict. Atomic-dump
    form; the streaming path emits ``header`` + branches + ``verdict_success``
    separately."""
    return [header(essid), auth_req(), assoc_req(), m1(True)] + verdict_success(save_hint)


def render_failure(essid: str, reason: Optional[PmkidFail]) -> list[str]:
    """Header, the auth/assoc branches (only for reasons reached after TX), an
    ``M1 … PMKID✗`` branch (NO_KDE only, its M1 arrived KDE-less), then the
    failure verdict. Atomic-dump form (see ``verdict_failure`` for the streaming
    terminal line)."""
    lines = [header(essid)]
    if reason in _AFTER_TX:
        lines += [auth_req(), assoc_req()]
    if reason is PmkidFail.NO_KDE:
        lines.append(m1(False))
    return lines + verdict_failure(reason)
