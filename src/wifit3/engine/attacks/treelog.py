"""Tree-connector prefixes for the attack / scanner event logs.

Shared across WEP (ChopChop, Fragmentation, …), the WPA-family attacks
(Deauth, PMKID, SAE) and the Scanner — anywhere a bounded phase wants to
render a small tree: a plain HEADER line, then ``├─`` step lines, then a
terminal ``└─`` leaf.

Because every group ends with a terminal line the daemon's own control flow
reaches (success or give-up), the ``└`` connector is always correct *without*
buffering or look-ahead — which matters, since RichLog is append-only and we
never rewrite past lines. A group is scoped to one daemon-phase; a hand-off
(chop → replay → crack) just starts a fresh group rather than one mega-tree.

These return Rich-markup strings; the caller passes them to the same ``_log``
callback as any other line. The message keeps its own colour; the connector
glyph carries the status (green ✓ / red ╳).
"""


def branch(msg: str) -> str:
    """A non-terminal step under the current header (├─►)."""
    return f" [dim]├─►[/dim] {msg}"


def branch_ok(msg: str) -> str:
    """A non-terminal step that succeeded (├─✓). The group continues below it
    (e.g. a recovered value followed by a └─► save hint)."""
    return f" [dim]├─[/dim][green]✓[/green] {msg}"


def branch_fail(msg: str) -> str:
    """A non-terminal step that failed (├─╳), followed by more lines (e.g. a
    headline failure followed by ├─►/└─► reasons)."""
    return f" [dim]├─[/dim][red]╳[/red] {msg}"


def branch_dim(msg: str) -> str:
    """A non-terminal, inert item (├──) — neither an action nor a status, just
    an enumerated entry (e.g. an SAE group the AP doesn't support)."""
    return f" [dim]├── {msg}[/dim]"


def leaf_ok(msg: str) -> str:
    """The terminal success line that closes the current group (└─✓)."""
    return f" [dim]└─[/dim][green]✓[/green] {msg}"


def leaf_fail(msg: str) -> str:
    """The terminal failure / give-up line that closes the current group (└─╳)."""
    return f" [dim]└─[/dim][red]╳[/red] {msg}"


def leaf_warn(msg: str) -> str:
    """A terminal warning line that closes the group (└─⚠).

    For inconclusive outcomes — neither success nor a clean failure, e.g. the
    SAE probe couldn't get definitive results (rate-limited / PMF / off-channel)."""
    return f" [dim]└─[/dim][yellow]⚠[/yellow] {msg}"


def leaf(msg: str) -> str:
    """A neutral (informational) terminal line that closes the group (└─►).

    For the last of a set of plain sub-items, e.g. the copy/save hints under a
    recovered key — neither a success nor a failure."""
    return f" [dim]└─►[/dim] {msg}"
