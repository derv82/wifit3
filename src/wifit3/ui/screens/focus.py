from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, DataTable, Button, Label, Static
from textual.containers import Vertical, Horizontal, Grid
from textual.binding import Binding

from wifit3.engine.models import AccessPoint

class FocusView(Screen):
    """The Attack/Focus mode for a specific AP."""

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True)
    ]

    def __init__(self):
        super().__init__()
        self.target_ap: AccessPoint = None
        self._refresh_timer = None
        self._known_clients = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        with Vertical(id="focus-container"):
            # Top 1/3: AP Information
            with Horizontal(id="ap-info-panel"):
                yield Vertical(
                    Label("TARGET INFO", classes="panel-title"),
                    Label(id="lbl-ssid", classes="bold-title"),
                    Label(id="lbl-bssid"),
                    Label(id="lbl-channel"),
                    classes="info-box"
                )
                yield Vertical(
                    Label("SECURITY", classes="panel-title"),
                    Label(id="lbl-enc"),
                    Label(id="lbl-pmf"),
                    Label(id="lbl-wpa3"),
                    classes="info-box"
                )

            # Middle 1/3: Client List
            with Vertical(id="client-panel"):
                yield Label("CLIENTS", classes="panel-title")
                client_table = DataTable(cursor_type="row", id="client-table")
                client_table.add_column("[ ]", key="select") # Checkbox column
                client_table.add_column("MAC Address", key="mac")
                client_table.add_column("PWR", key="signal")
                client_table.add_column("Frames", key="packets")
                yield client_table

            # Bottom 1/3: Attack Buttons
            with Vertical(id="attack-panel"):
                yield Label("ATTACKS", classes="panel-title")
                with Horizontal(classes="button-row"):
                    yield Button("Deauth All (Broadcast)", variant="error", id="btn-deauth-all")
                    yield Button("Deauth Selected", variant="warning", id="btn-deauth-sel", disabled=True)
                    yield Button("PMKID Harvest", variant="primary", id="btn-pmkid")
                    
                with Horizontal(classes="button-row"):
                    yield Button("SAE Group Probe", variant="primary", id="btn-sae-probe", disabled=True)
                    yield Button("WPA3 Downgrade", variant="primary", id="btn-wpa3-down", disabled=True)

        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(1 / 10, self.update_ui)
        await self._init_target()

    async def on_screen_resume(self) -> None:
        await self._init_target()

    async def _init_target(self) -> None:
        self.target_ap = getattr(self.app, "target_ap", None)
        if not self.target_ap:
            return
            
        # Reset UI state
        self._known_clients.clear()
        self.query_one("#client-table", DataTable).clear()
        
        # Tune the card to the target channel
        if getattr(self.app, "active_interface", None):
            await self.app.active_interface.set_channel(self.target_ap.channel)
            
        self.update_ui()

    def update_ui(self) -> None:
        if not self.target_ap or not self.is_current:
            return
            
        # Update AP Labels
        self.query_one("#lbl-ssid", Label).update(f"[bold white]{self.target_ap.ssid or '<Hidden>'}[/bold white]")
        self.query_one("#lbl-bssid", Label).update(f"BSSID: {self.target_ap.bssid}")
        self.query_one("#lbl-channel", Label).update(f"Channel: {self.target_ap.channel}")
        
        self.query_one("#lbl-enc", Label).update(f"Encryption: {self.target_ap.encryption}")
        
        pmf_status = "Disabled"
        if self.target_ap.pmf_required: pmf_status = "Required"
        elif self.target_ap.pmf_capable: pmf_status = "Capable (Optional)"
        self.query_one("#lbl-pmf", Label).update(f"PMF: {pmf_status}")
        
        wpa3_status = "N/A"
        btn_sae = self.query_one("#btn-sae-probe", Button)
        btn_down = self.query_one("#btn-wpa3-down", Button)
        
        if self.target_ap.wpa3:
            wpa3_status = "Transition Mode" if self.target_ap.transition_mode else "Pure WPA3-SAE"
            btn_sae.disabled = False
            btn_down.disabled = False
        else:
            btn_sae.disabled = True
            btn_down.disabled = True
            
        self.query_one("#lbl-wpa3", Label).update(f"WPA3: {wpa3_status}")
        
        # Update Clients
        iface = getattr(self.app, "active_interface", None)
        if iface:
            client_table = self.query_one("#client-table", DataTable)
            for mac, client in iface.clients.items():
                if client.bssid == self.target_ap.bssid:
                    if mac not in self._known_clients:
                        self._known_clients.add(mac)
                        client_table.add_row(
                            "[ ]", 
                            mac, 
                            f"{client.signal}dBm", 
                            str(client.packets),
                            key=mac
                        )
                    else:
                        client_table.update_cell(mac, "signal", f"{client.signal}dBm")
                        client_table.update_cell(mac, "packets", str(client.packets))

    async def action_go_back(self) -> None:
        """Return to the scanner and resume channel hopping."""
        if getattr(self.app, "active_interface", None):
            await self.app.active_interface.start_hopping(interval=0.25)
        self.app.pop_screen()
