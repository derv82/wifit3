import asyncio
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, RichLog, Static
from textual.containers import Horizontal, Vertical
from textual import work
from typing import Dict

from wifit3.wlan.manager import WlanDeviceManager
from wifit3.engine.models import AccessPoint
import logging

logger = logging.getLogger(__name__)

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
    DataTable > .datatable--cursor {
        background: #00FF00;
        color: #000000;
        text-style: bold;
    }
    DataTable > .datatable--header {
        background: #000000;
        color: #00FF00;
        text-style: bold;
    }
    DataTable > .datatable--hover {
        background: #003300;
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
        ("q", "quit", "Quit")
    ]

    def __init__(self):
        super().__init__()
        self.device_manager = WlanDeviceManager()
        self.ap_cache: Dict[str, AccessPoint] = {}
        self.active_interface = None
        self._refresh_timer = None

    def compose(self) -> ComposeResult:
        yield Vertical(Static(ASCII_ART, id="header-area"))
        with Horizontal():
            table = DataTable(cursor_type="row")
            table.add_column("BSSID", key="bssid")
            table.add_column("CH", key="channel")
            table.add_column("PWR", key="signal")
            table.add_column("ENC", key="encryption")
            table.add_column("SSID", key="ssid")
            table.add_column("Beacons", key="beacons")
            yield table
            yield RichLog(highlight=True, markup=True)
        yield Footer()

    async def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("[bold green]Wifit3 initialized.[/bold green]")
        log.write("Press [bold green]'s'[/bold green] to start scanning.")
        log.write("Press [bold green]'i'[/bold green] to list local interfaces.")
        
        # Start the 60FPS UI polling loop
        self._refresh_timer = self.set_interval(1 / 60, self.refresh_dashboard)

    def refresh_dashboard(self) -> None:
        if not self.active_interface:
            return
            
        table = self.query_one(DataTable)
        
        # 60 FPS safe in-place updates
        for ap in self.active_interface.get_access_points():
            if ap.bssid not in self.ap_cache:
                self.ap_cache[ap.bssid] = ap
                table.add_row(
                    ap.bssid,
                    str(ap.channel),
                    f"{ap.signal}dBm",
                    ap.encryption,
                    ap.ssid or "<Hidden>",
                    str(ap.beacons),
                    key=ap.bssid
                )
            else:
                self.ap_cache[ap.bssid] = ap
                table.update_cell(ap.bssid, "channel", str(ap.channel))
                table.update_cell(ap.bssid, "signal", f"{ap.signal}dBm")
                table.update_cell(ap.bssid, "encryption", ap.encryption)
                table.update_cell(ap.bssid, "ssid", ap.ssid or "<Hidden>")
                table.update_cell(ap.bssid, "beacons", str(ap.beacons))
                
        # Simple sorting by signal strength
        table.sort("signal", reverse=True)

    async def action_scan(self):
        log = self.query_one(RichLog)
        if self.active_interface:
            log.write("[yellow]Scanner is already running.[/yellow]")
            return

        log.write("[bold cyan]Discovering interfaces...[/bold cyan]")
        interfaces = await self.device_manager.refresh()
        
        if not interfaces:
            log.write("[bold red]No supported hardware found.[/bold red]")
            return
            
        self.active_interface = interfaces[0]
        log.write(f"[bold green]Starting Live Scan on {self.active_interface.description}...[/bold green]")
        
        # Connect to hardware (HTC/WMI Handshake)
        log.write("[cyan]Connecting to hardware (Monitor Mode)...[/cyan]")
        await self.active_interface.connect()
        
        # Start channel hopping
        log.write("[cyan]Starting channel hopper...[/cyan]")
        await self.active_interface.start_hopping(interval=0.25)
        
        log.write("[bold green]Scanning Active![/bold green]")

    async def action_stop(self):
        log = self.query_one(RichLog)
        if self.active_interface:
            log.write("[bold orange3]Stopping hardware...[/bold orange3]")
            await self.active_interface.close()
            self.active_interface = None
            log.write("[bold orange3]Scanner stopped.[/bold orange3]")

    async def action_interfaces(self):
        log = self.query_one(RichLog)
        log.write("[bold cyan]Refreshing USB Bus...[/bold cyan]")
        interfaces = await self.device_manager.refresh()
        log.write("[bold cyan]Wireless Interfaces Found:[/bold cyan]")
        for iface in interfaces:
            log.write(f"- {iface.name}: {iface.description}")
            
    async def action_quit(self):
        if self.active_interface:
            await self.active_interface.close()
        self.exit()
