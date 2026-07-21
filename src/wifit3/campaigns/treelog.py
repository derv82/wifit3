"""Tree-connector prefixes for the attack / scanner event logs.

Shared anywhere a bounded phase renders a small tree: a HEADER line, then ``├─`` steps, then
a terminal ``└─`` leaf. Every group ends with a terminal line the daemon's control flow
reaches, so the ``└`` is always correct without buffering or look-ahead, which matters since
RichLog is append-only. These return Rich markup; the glyph carries status (green ✓ / red ╳).
"""


def header(msg: str, color: str = "green") -> str:
    """A group header ( ● ): root line above the ├─/└─ children (leading space aligns the bullet)."""
    return f" [{color}]●[/{color}] {msg}"


def branch(msg: str) -> str:
    """A non-terminal step under the current header (├─►)."""
    return f" [dim]├─►[/dim] {msg}"


def branch_ok(msg: str) -> str:
    """A non-terminal step that succeeded (├─✓); the group continues below it."""
    return f" [dim]├─[/dim][green]✓[/green] {msg}"


def leaf_ok(msg: str) -> str:
    """The terminal success line that closes the current group (└─✓)."""
    return f" [dim]└─[/dim][green]✓[/green] {msg}"


def leaf_fail(msg: str) -> str:
    """The terminal failure / give-up line that closes the current group (└─╳)."""
    return f" [dim]└─[/dim][red]╳[/red] {msg}"


def leaf_warn(msg: str) -> str:
    """A terminal warning line closing the group (└─⚠) for inconclusive outcomes: neither
    success nor clean failure (e.g. SAE probe rate-limited / PMF / off-channel)."""
    return f" [dim]└─[/dim][yellow]⚠ [/yellow] {msg}"


def leaf(msg: str) -> str:
    """A neutral informational terminal line closing the group (└─►), e.g. copy/save hints
    under a recovered key."""
    return f" [dim]└─►[/dim] {msg}"
