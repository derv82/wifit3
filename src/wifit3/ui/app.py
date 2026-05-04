from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, RichLog, Static
from textual.containers import Horizontal, Vertical
from textual import work
from typing import Dict
import sys

from ..engine.scanner import Scanner
from ..engine.models import AccessPoint
from ..interface.manager import InterfaceManager
from loguru import logger
import sys

# Remove default sink and add file sink only
logger.remove()
logger.add("debug.log", rotation="10 MB", level="DEBUG")

ASCII_ART = r"""
[bold green]w i f i t 3[/bold green]  [dim green]// wireless auditor[/dim green]
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
        self.interface_manager = InterfaceManager()
        self.scanner = Scanner(callback=self.on_ap_discovered)
        self.ap_cache: Dict[str, AccessPoint] = {}
        self.active_interface = None

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
        log.write("Press [bold green]'s'[/bold green] to start scanning.")
        log.write("Press [bold green]'i'[/bold green] to list local interfaces.")
        # Pre-refresh interfaces
        self.interface_manager.refresh()

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
        if self.scanner.is_running:
            log.write("[yellow]Scanner is already running.[/yellow]")
            return

        # Look for a monitor-capable interface
        if not self.active_interface:
            for iface in self.interface_manager.interfaces:
                if iface.can_monitor():
                    self.active_interface = iface
                    break
        
        if self.active_interface:
            log.write(f"[bold green]Starting Live Scan on {self.active_interface.description}...[/bold green]")
            
            # Temporary: print for terminal debug, and log for TUI debug
            success = self.active_interface.set_monitor(True)
            if success:
                log.write("[cyan]Monitor mode enabled.[/cyan]")
                self.interface_manager.start_hopping(self.active_interface, interval=1.0)
                self.scanner.start(interface=self.active_interface.name)
            else:
                log.write("[bold red]ERROR: WlanHelper failed to set monitor mode.[/bold red]")
                log.write("[red]Check terminal scrollback after quitting for raw error details.[/red]")
                self.run_simulation()
        else:
            log.write("[bold yellow]No monitor-capable hardware found. Starting Simulation...[/bold yellow]")
            self.run_simulation()

    def run_simulation(self):
        self.scanner.is_running = True
        self.run_simulation_task()

    @work(exclusive=True, thread=True)
    def run_simulation_task(self):
        self.scanner.simulate_discovery()

    def action_stop(self):
        self.scanner.stop()
        self.interface_manager.stop_hopping()
        if self.active_interface:
            self.active_interface.set_monitor(False)
            
        log = self.query_one(RichLog)
        log.write("[bold orange3]Scanner stopped.[/bold orange3]")

    def action_interfaces(self):
        log = self.query_one(RichLog)
        self.interface_manager.refresh()
        log.write("[bold cyan]Wireless Interfaces Found:[/bold cyan]")
        for iface in self.interface_manager.interfaces:
            can_mon = "YES" if iface.can_monitor() else "NO"
            log.write(f"- {iface.description} [bold]Monitor: {can_mon}[/bold]")
        
    def action_attack(self):
        log = self.query_one(RichLog)
        log.write("[bold red]Attack logic not yet implemented.[/bold red]")
