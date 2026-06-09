import sys
import asyncio
import logging
from pathlib import Path
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, ListView, ListItem, Label, Header, Footer, ProgressBar, Button
from textual.message import Message
from textual.containers import Vertical, Center
from textual import work
from rich.text import Text
from rich.style import Style

from wifit3.setup.windows import install_winusb
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.wlan.manager import WlanDeviceManager

logger = logging.getLogger(__name__)

def _without_bgcolor(style: Style) -> Style:
    """Return a copy of ``style`` with the background color unset.

    Rich styles are immutable and ``+ Style(bgcolor=None)`` is a no-op
    (None means "no override"), so we rebuild preserving every other
    attribute. An unset bgcolor lets Textual composite the actual
    widget/theme background through (true transparency), unlike
    ``"default"`` which resolves to the terminal's hard default (black).
    """
    return Style(
        color=style.color,
        bold=style.bold, dim=style.dim, italic=style.italic,
        underline=style.underline, blink=style.blink, blink2=style.blink2,
        reverse=style.reverse, conceal=style.conceal, strike=style.strike,
        underline2=style.underline2, frame=style.frame,
        encircle=style.encircle, overline=style.overline, link=style.link,
    )


def _make_black_transparent(logo: Text) -> Text:
    """Drop black (0,0,0) backgrounds so the logo inherits the theme
    background instead of painting its own black canvas.

    The art colors each glyph via its background, so only bgcolor matters:
    black-background spans get their bgcolor unset (transparent); every
    other color is left untouched.
    """
    def transparent_if_black(style):
        if not isinstance(style, Style) or style.bgcolor is None:
            return style
        try:
            if tuple(style.bgcolor.get_truecolor()) == (0, 0, 0):
                return _without_bgcolor(style)
        except Exception:
            pass
        return style

    logo.spans = [s._replace(style=transparent_if_black(s.style)) for s in logo.spans]
    return logo

class DriverProgress(Message):
    """Message sent from background threads to update the splash progress."""
    def __init__(self, percentage: float, message: str) -> None:
        super().__init__()
        self.percentage = percentage
        self.message = message

def load_logo() -> Text:
    """Load the ANSI logo from assets."""
    logo_path = Path(__file__).parent.parent / "assets" / "logo_sm.ans"
    try:
        if logo_path.exists():
            return _make_black_transparent(
                Text.from_ansi(logo_path.read_text(encoding="utf-8"))
            )
    except Exception:
        pass
    
    # Fallback
    return Text.from_markup("[bold green]Wifit3[/bold green]\n[dim green]// Wireless Auditor[/dim green]")

LOGO = load_logo()

class SplashView(Screen):
    """The initial splash and device selection screen."""

    BINDINGS = [
        ("q", "app.quit", "Quit")
    ]

    def __init__(self, device_manager: WlanDeviceManager):
        super().__init__()
        self.device_manager = device_manager
        self._refresh_timer = None
        self._last_signature = None
        self._is_initializing = False
        # ListItem name -> UnboundDevice, for the present-but-unbound rows in the picker.
        self._unbound_by_name = {}
        # The present-but-unbound card currently highlighted (drives the Install button).
        self._selected_unbound = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="splash-container"):
            with Center():
                yield Static(LOGO, id="ascii-art")
            
            with Center():
                yield Label("Scanning for compatible hardware...", id="status-label")

            with Center():
                yield ProgressBar(total=100, show_eta=False, id="init-progress")

            with Center():
                yield ListView(id="device-list")

            # OS notice (Zadig / rmmod help) sits BELOW the device list — it's
            # supplementary, and the interface picker is what the user came for. The
            # Install-WinUSB button to its right shows only while a present-but-unbound
            # card is highlighted (see on_list_view_highlighted).
            with Vertical(id="setup-row"):
                with Center(id="notice-row"):
                    yield Static(self._get_os_warning(), id="os-warning")
                with Center(id="install-row"):
                    yield Button("Install WinUSB", id="install-winusb", variant="warning")
                
        yield Footer()

    def _get_os_warning(self) -> str:
        warn = self.app.theme_variables.get("text-warning", "yellow")
        if sys.platform == "win32":
            return (f"[{warn}]Windows Notice:[/{warn}] You must install the WinUSB driver\n"
                    "for your wireless card using Zadig before Wifit3 can see it.")
        elif sys.platform == "linux":
            return ("[red]Linux Notice:[/red] Your OS driver is currently controlling the card.\n"
                    "If your card is not listed below, you may need to run:\n"
                    "[bold]sudo rmmod <chipset>[/bold]\n"
                    "We will prompt you before running this automatically.")
        else:
            return f"[{warn}]OS Notice:[/{warn}] Experimental platform. Your mileage may vary."

    def _selected_unbound_notice(self, u) -> str:
        """Card-specific 'needs driver setup' notice for the highlighted unbound device —
        replaces the generic OS hint while that row is selected. The chipset/card sits on
        its own line so a long name (e.g. 'TP-Link [dkms]') doesn't widen the box past the
        fixed first line. [DEVICE-SETUP.md Tier 0]"""
        warn = self.app.theme_variables.get("text-warning", "orange3")
        card = f"Chipset/Card: [bold]{u.description}[/bold] ({u.vidpid})"
        if sys.platform == "win32":
            return (f"[bold {warn}]Note:[/bold {warn}] This card needs a [bold]WinUSB driver[/bold] "
                    "installed for Wifit3 to use it.\n"
                    f"{card}\n"
                    f"[black bold on {warn}]Install WinUSB[/] overwrites the existing driver "
                    "(reversible).")
        elif sys.platform == "linux":
            return (f"[bold {warn}]Note:[/bold {warn}] This card is held by the kernel driver.\n"
                    f"{card}\n"
                    "Release it with [bold]sudo rmmod <chipset>[/bold] (reversible on replug).")
        return (f"[bold {warn}]Note:[/bold {warn}] This card needs a libusb-class driver.\n{card}")

    async def on_mount(self) -> None:
        self.query_one("#init-progress").display = False
        self.query_one("#install-winusb").display = False  # shown only on an unbound row
        # Poll the USB bus every second for hotplug changes.
        self._refresh_timer = self.set_interval(1.0, self.poll_usb)
        # ...but do the FIRST enumeration as soon as the splash has painted —
        # set_interval waits a full second before its first tick, which was the
        # ~1s "nothing's happening" gap before the device list first appeared.
        self.call_after_refresh(self.poll_usb)

    async def poll_usb(self) -> None:
        # Don't poll if we're currently initializing a driver
        if self._is_initializing:
            return
            
        interfaces = await self.device_manager.refresh()
        unbound = self.device_manager.unbound
        # Re-render only when the set of ready cards OR present-but-unbound cards changes.
        signature = (tuple(i.name for i in interfaces),
                     tuple(u.vidpid for u in unbound))
        if signature == self._last_signature:
            return
        self._last_signature = signature

        list_view = self.query_one("#device-list", ListView)
        list_view.clear()
        self._unbound_by_name = {}

        # Ready cards (selecting one -> connect), then present-but-unbound cards
        # (selecting one -> its card-specific WinUSB notice + Install button, driven by
        # on_list_view_highlighted). [DEVICE-SETUP.md Tier 0]
        for iface in interfaces:
            list_view.append(ListItem(Label(f"[{iface.name}] {iface.description}"), name=iface.name))
        for u in unbound:
            key = f"unbound:{u.vidpid}"
            self._unbound_by_name[key] = u
            list_view.append(
                ListItem(Label(f"[yellow]⚠[/yellow]  {u.description}"), name=key))

        if not interfaces and not unbound:
            self.query_one("#status-label", Label).update("Scanning for compatible hardware... (0 found)")
            self.query_one("#os-warning", Static).update(self._get_os_warning())
            self.query_one("#install-winusb", Button).display = False
            return

        if interfaces:
            self.query_one("#status-label", Label).update("[bold bright_green]Select an interface to begin:[/bold bright_green]")
        else:
            self.query_one("#status-label", Label).update("[bold yellow]Card needs driver setup — select it for instructions[/bold yellow]")
        # Highlight the first row so the user can act immediately; the Highlighted handler
        # sets the warning box + Install button from whatever ends up selected. clear()
        # reset index to None, so this re-arms on every (re)population.
        if list_view.index is None:
            list_view.index = 0
            list_view.focus()

    def on_driver_progress(self, event: DriverProgress) -> None:
        """Handle progress updates sent via message from background threads."""
        warn = self.app.theme_variables.get("text-warning", "yellow")
        self.query_one("#init-progress", ProgressBar).progress = event.percentage * 100
        self.query_one("#status-label", Label).update(f"[bold {warn}]{event.message}[/bold {warn}]")


    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Drive the warning box + Install button off the highlighted row: a present-but-
        unbound card shows its card-specific notice + the button; a ready card (or no
        selection) shows the generic OS notice with the button hidden. [DEVICE-SETUP.md]"""
        name = event.item.name if event.item is not None else None
        u = self._unbound_by_name.get(name) if name else None
        warning = self.query_one("#os-warning", Static)
        button = self.query_one("#install-winusb", Button)
        if u is not None:
            self._selected_unbound = u
            warning.update(self._selected_unbound_notice(u))
            button.display = (sys.platform == "win32")  # WinUSB install is Windows-only
        else:
            self._selected_unbound = None
            warning.update(self._get_os_warning())
            button.display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "install-winusb":
            u = self._selected_unbound
            # The button only shows while an unbound row is highlighted (win32), so u is set;
            # guard anyway, and don't stack a second install over an in-flight one.
            if u is None or self._is_initializing:
                return
            self.perform_install(u)

    @work(exclusive=True)
    async def perform_install(self, u) -> None:
        """Run the elevated WinUSB install for a present-but-unbound card, then re-scan.

        The blocking ShellExecuteExW + wait is offloaded to a thread so the UAC round-trip
        doesn't freeze the TUI; on success we re-poll the bus and the card flips ⚠ → ready
        on its own. A declined UAC prompt is a soft notice; any other failure opens a modal.
        """
        status = self.query_one("#status-label", Label)
        list_view = self.query_one("#device-list", ListView)
        button = self.query_one("#install-winusb", Button)

        self._is_initializing = True
        if self._refresh_timer:
            self._refresh_timer.pause()
        list_view.disabled = True
        button.disabled = True
        status.update(
            f"[bold yellow]Installing WinUSB for {u.description} — accept the Windows "
            f"elevation prompt…[/bold yellow]")

        result = None
        try:
            result = await asyncio.to_thread(install_winusb, u.vid, u.pid, name=u.description)
        except Exception as e:
            logger.exception("WinUSB install crashed for %s", u.vidpid)
            self.app.push_screen(SetupErrorDialog("WinUSB install failed", str(e)))
        finally:
            list_view.disabled = False
            button.disabled = False
            self._is_initializing = False
            if self._refresh_timer:
                self._refresh_timer.resume()

        if result is None:
            status.update("[bold red]WinUSB install failed.[/bold red]")
        elif result.ok:
            status.update(
                f"[bold green]WinUSB installed for {u.description}. Re-scanning…[/bold green]")
            self._last_signature = None   # force the picker to re-render the now-ready card
            await self.poll_usb()
        elif result.cancelled:
            status.update("[yellow]Elevation cancelled — WinUSB was not installed.[/yellow]")
        else:
            status.update("[bold red]WinUSB install failed.[/bold red]")
            details = f"libwdi code {result.wdi_code}" if result.wdi_code is not None else None
            self.app.push_screen(
                SetupErrorDialog("WinUSB install failed", result.message, details))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        iface_name = event.item.name
        # Unbound rows aren't real interfaces — get_interface() returns None and the guard
        # below skips. (The Install button is their action; Enter is a no-op for now.)
        iface = self.device_manager.get_interface(iface_name)
        if iface and not self._is_initializing:
            self._is_initializing = True
            if self._refresh_timer:
                self._refresh_timer.pause()
            
            # Show progress UI
            self.query_one("#init-progress", ProgressBar).display = True
            self.query_one("#device-list", ListView).disabled = True
            
            # Start the connection worker and return immediately!
            # This keeps the UI thread 100% free to process progress messages.
            self.perform_connect(iface)

    @work(exclusive=True)
    async def perform_connect(self, iface) -> None:
        """Textual Worker that manages the connection lifecycle."""
        def update_progress(percentage: float, message: str):
            self.post_message(DriverProgress(percentage, message))

        try:
            # Mount the interface (connect)
            # This method internally offloads heavy procedural loops to a thread
            success = await iface.connect(progress_cb=update_progress)
            
            if success:
                # Pass the active interface back to the main app
                self.app.active_interface = iface
                
                # We are safely back on the main thread here, just switch!
                self.query_one("#init-progress", ProgressBar).progress = 100
                self.query_one("#status-label", Label).update("[bold green]Initialization Complete. Starting Scanner...[/bold green]")
                
                await asyncio.sleep(0.5)
                self.app.switch_screen("scanner")
            else:
                raise RuntimeError("Hardware failed to initialize.")

        except Exception as e:
            logger.exception(f"Failed to connect to {iface.name}")
            self.query_one("#status-label", Label).update(f"[bold red]Initialization Failed: {e}[/bold red]")
            self.query_one("#device-list", ListView).disabled = False
            self.query_one("#init-progress", ProgressBar).display = False
            if self._refresh_timer:
                self._refresh_timer.resume()
        finally:
            self._is_initializing = False
