"""Tree-connector prefixes for the WEP attack event log.

A discrete sub-attack (ChopChop, Fragmentation) logs a small tree: a plain
HEADER line, then ``├─►`` step lines, then a terminal ``└─✓`` / ``└─╳`` leaf.

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


def leaf_ok(msg: str) -> str:
    """The terminal success line that closes the current group (└─✓)."""
    return f" [dim]└─[/dim][green]✓[/green] {msg}"


def leaf_fail(msg: str) -> str:
    """The terminal failure / give-up line that closes the current group (└─╳)."""
    return f" [dim]└─[/dim][red]╳[/red] {msg}"
