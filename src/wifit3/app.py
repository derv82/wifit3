from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Tree, RichLog
from textual.containers import Horizontal

class WifiteApp(App):
    """wifit3 TUI Mockup."""

    TITLE = "wifit3 - Wireless Auditor"

    BINDINGS = [
        ("s", "scan", "Scan"),
        ("a", "attack", "Attack"),
        ("q", "quit", "Quit")
    ]

    CSS = """
    Tree {
        width: 30%;
        border-right: solid green;
    }
    RichLog {
        width: 70%;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            tree = Tree("Access Points")
            tree.root.expand()
            yield tree
            yield RichLog(highlight=True, markup=True)
        yield Footer()

    def action_scan(self):
        """Mock behavior for the 'scan' keybinding."""
        log = self.query_one(RichLog)
        log.write("[bold green]Scanning for access points...[/bold green]")
        
    def action_attack(self):
        """Mock behavior for the 'attack' keybinding."""
        log = self.query_one(RichLog)
        log.write("[bold red]Initiating attack sequence...[/bold red]")
