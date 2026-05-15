import sys
import asyncio
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, ListView, ListItem, Label, Header, Footer
from textual.containers import Vertical, Center

from wifit3.wlan.manager import WlanDeviceManager

ASCII_ART = r"""
[bold green]
               _    ___   _    _     _______ 
              (_)  / __) (_)  | |   (_______)
 _ _ _  _   _  _ _| |__   _  _| |_       _   
| | | || | | || (_   __) | |(_   _)     | |  
| | | || |_| || | | |    | |  | |_    __| |  
 \___/  \____||_| |_|    |_|   \__)  (_____) 
[/bold green]
[dim green]// Python-Native Wireless Auditor[/dim green]
"""

class SplashView(Screen):
    """The initial splash and device selection screen."""

    BINDINGS = [
        ("q", "app.quit", "Quit")
    ]

    def __init__(self, device_manager: WlanDeviceManager):
        super().__init__()
        self.device_manager = device_manager
        self._refresh_timer = None
        self._last_interfaces = []
        self._is_initializing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="splash-container"):
            with Center():
                yield Static(ASCII_ART, id="ascii-art")
            
            with Center():
                yield Static(self._get_os_warning(), id="os-warning")
            
            with Center():
                yield Label("Scanning for compatible hardware...", id="status-label")
            
            with Center():
                yield ListView(id="device-list")
                
        yield Footer()

    def _get_os_warning(self) -> str:
        if sys.platform == "win32":
            return ("[yellow]Windows Notice:[/yellow] You must install the WinUSB driver\n"
                    "for your wireless card using Zadig before Wifit3 can see it.")
        elif sys.platform == "linux":
            return ("[red]Linux Notice:[/red] Your OS driver is currently controlling the card.\n"
                    "If your card is not listed below, you may need to run:\n"
                    "[bold]sudo rmmod <chipset>[/bold]\n"
                    "We will prompt you before running this automatically.")
        else:
            return "[yellow]OS Notice:[/yellow] Experimental platform. Your mileage may vary."

    async def on_mount(self) -> None:
        # Poll USB bus every second
        self._refresh_timer = self.set_interval(1.0, self.poll_usb)

    async def poll_usb(self) -> None:
        # Don't poll if we're currently initializing a driver
        if self._is_initializing:
            return
            
        interfaces = await self.device_manager.refresh()
        current_names = [iface.name for iface in interfaces]
        
        if current_names == self._last_interfaces:
            return
            
        self._last_interfaces = current_names
        list_view = self.query_one("#device-list", ListView)
        list_view.clear()
        
        if interfaces:
            self.query_one("#status-label", Label).update("[bold green]Select an interface to begin:[/bold green]")
            for iface in interfaces:
                list_view.append(ListItem(Label(f"[{iface.name}] {iface.description}"), name=iface.name))
        else:
            self.query_one("#status-label", Label).update("Scanning for compatible hardware... (0 found)")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        iface_name = event.item.name
        iface = self.device_manager.get_interface(iface_name)
        if iface and not self._is_initializing:
            self._is_initializing = True
            if self._refresh_timer:
                self._refresh_timer.pause()
            
            # Give UI feedback
            self.query_one("#status-label", Label).update(f"[bold yellow]Initializing {iface.name}... This may take a few seconds.[/bold yellow]")
            self.query_one("#device-list", ListView).disabled = True
            
            # Force UI update to process the label and disabled state before blocking
            await asyncio.sleep(0.1)
            
            # Mount the interface (connect)
            await iface.connect()
            
            # Pass the active interface back to the main app and transition
            self.app.active_interface = iface
            self.app.push_screen("scanner")
            self._is_initializing = False
