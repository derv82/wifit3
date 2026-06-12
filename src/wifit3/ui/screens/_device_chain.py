"""The Wifit3 ◄──► <link> ◄──► card diagram, shared by the install + access modals.

Both the Windows WinUSB install dialog and the Linux device-access dialog draw the same
three-box chain with the middle (link) box flagged as the missing REQUIRED piece — only the
link label and an optional "+N other supported devices" footnote differ. Keeping it in one
place stops the two modals' visuals from drifting.
"""
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Red shades cycled to make the REQUIRED badge pulse (ping-pong for a smooth throb).
PULSE = ["#6e0000", "#960000", "#c00000", "#ff2a2a", "#c00000", "#960000"]


def chain_diagram(short: str, link_label: str, pulse: str,
                  *, extra_supported: str | None = None) -> Table:
    """The ``Wifit3 ◄──► <link_label> ◄──► <short>`` chain + status row, as a Rich grid.

    Rich handles column alignment so the ✓/✗ land under their boxes. ``pulse`` is the REQUIRED
    badge's current background red. ``extra_supported`` (e.g. "+ 119 other supported devices")
    adds a cyan footnote under the card box — the Linux all-cards hint; omitted on Windows.
    """
    grid = Table.grid(padding=(0, 1))
    for _ in range(5):
        grid.add_column(justify="center", vertical="middle")
    grid.add_row(
        Panel("Wifit3", border_style="green", expand=False, padding=(0, 1)),
        Text("◄──►", style="bold"),
        Panel(link_label, border_style="red", expand=False, padding=(0, 1)),
        Text("◄──►", style="bold"),
        Panel(short, border_style="green", expand=False, padding=(0, 1)),
    )
    grid.add_row(
        Text("✓", style="bold green"),
        Text(""),
        Text(" ✗ REQUIRED ", style=f"bold white on {pulse}"),
        Text(""),
        Text("✓ SUPPORTED", style="bold green"),
    )
    if extra_supported:
        grid.add_row(Text(""), Text(""), Text(""), Text(""),
                     Text(extra_supported, style="cyan"))
    return grid
