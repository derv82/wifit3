from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, RichLog, Static
from textual.containers import Horizontal, Vertical
from textual import work
from typing import Dict
import sys

from ..engine.scanner import Scanner
from ..engine.models import AccessPoint

ASCII_ART = r"""
[bold green]
           _  __ _  _    _____ 
          (_)/ _(_)| |  |____ |
 __      ___| |_  _| |_     / /
 \ \ /\ / / |  _|| | __|    \ \\
  \ V  V /| | |  | | |_ .___/ /
   \_/\_/ |_|_|  |_|\__||____/ 
[/bold green]
"""

class WifiteApp(App):
    """wifit3 TUI."""

    TITLE = "wifit3 - Wireless Auditor"
    CSS = """
    Screen {
        background: #000000;
    }
    #header-area {
        height: auto;
        border-bottom: double green;
        content-align: center middle;
    }
    DataTable {
        width: 70%;
        border-right: solid green;
        background: #000000;
        color: #00FF00;
    }
    RichLog {
        width: 30%;
        background: #000000;
    }
    """

    BINDINGS = [
        ("s", "scan", "Start Scan"),
        ("x", "stop", "Stop Scan"),
        ("i", "interfaces", "List Interfaces"),
        ("a", "attack", "Attack"),
        ("q", "quit", "Quit")
    ]

    def __init__(self):
        super().__init__()
        self.scanner = Scanner(callback=self.on_ap_discovered)
        self.ap_cache: Dict[str, AccessPoint] = {}

    def compose(self) -> ComposeResult:
        yield Vertical(Static(ASCII_ART, id="header-area"))
        with Horizontal():
            table = DataTable()
            table.add_column("BSSID", key="bssid")
            table.add_column("SSID", key="ssid")
            table.add_column("Signal", key="signal")
            table.add_column("Beacons", key="beacons")
            yield table
            yield RichLog(highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("[bold cyan]Wifit3 initialized.[/bold cyan]")
        log.write("Press [bold green]'s'[/bold green] for simulation mode.")
        log.write("Press [bold green]'i'[/bold green] to list local interfaces.")

    def on_ap_discovered(self, ap: AccessPoint) -> None:
        self.call_from_thread(self.update_table, ap)

    def update_table(self, ap: AccessPoint) -> None:
        table = self.query_one(DataTable)
        # Use the BSSID string directly as the key
        if ap.bssid not in self.ap_cache:
            self.ap_cache[ap.bssid] = ap
            table.add_row(
                ap.bssid, 
                ap.ssid or "<Hidden>", 
                f"{ap.signal}dBm", 
                str(ap.beacons), 
                key=ap.bssid
            )
        else:
            self.ap_cache[ap.bssid] = ap
            # update_cell needs the row_key and column_key
            table.update_cell(ap.bssid, "ssid", ap.ssid or "<Hidden>")
            table.update_cell(ap.bssid, "signal", f"{ap.signal}dBm")
            table.update_cell(ap.bssid, "beacons", str(ap.beacons))

    def action_scan(self):
        log = self.query_one(RichLog)
        if not self.scanner.is_running:
            log.write("[bold green]Starting Scanner (Simulation)...[/bold green]")
            self.scanner.is_running = True
            self.run_simulation()
        else:
            log.write("[yellow]Scanner is already running.[/yellow]")

    @work(exclusive=True, thread=True)
    def run_simulation(self):
        self.scanner.simulate_discovery()

    def action_stop(self):
        self.scanner.stop()
        log = self.query_one(RichLog)
        log.write("[bold orange3]Scanner stopped.[/bold orange3]")

    def action_interfaces(self):
        log = self.query_one(RichLog)
        if sys.platform == "win32":
            from ..interface.manager import get_windows_interfaces
            ifaces = get_windows_interfaces()
            log.write("[bold cyan]Wireless Interfaces Found:[/bold cyan]")
            for iface in ifaces:
                log.write(f"- {iface['description']}")
        else:
            log.write("[yellow]Interface discovery only implemented for Windows currently.[/yellow]")
        
    def action_attack(self):
        log = self.query_one(RichLog)
        log.write("[bold red]Attack logic not yet implemented.[/bold red]")
