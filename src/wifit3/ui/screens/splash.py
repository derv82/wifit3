import sys
import asyncio
import logging
from pathlib import Path
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, ListView, ListItem, Label, Header, Footer, ProgressBar, Button
from textual.message import Message
from textual.containers import Vertical, Center, Horizontal
from textual import work
from rich.text import Text
from rich.style import Style

from wifit3.setup import ids_from_registry
from wifit3.setup.linux import current_user, install_rule, remove_rule
from wifit3.setup.windows import install_winusb, restore_driver
from wifit3.ui.screens.confirm_install import ConfirmInstallDialog
from wifit3.ui.screens.confirm_uninstall import ConfirmUninstallDialog
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.ui.screens.propagating import PropagatingDialog
from wifit3.wlan.manager import WlanDeviceManager

logger = logging.getLogger(__name__)

def _without_bgcolor(style: Style) -> Style:
    """Return a copy of ``style`` with the background color unset."""
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
    background instead of painting its own black canvas."""
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
    """Splash + device picker: the logo, the list of live cards, Start and Uninstall buttons."""

    BINDINGS = [("q", "app.quit", "Quit")]

    def __init__(self, device_manager: WlanDeviceManager):
        super().__init__()
        self.device_manager = device_manager
        self._refresh_timer = None
        self._last_signature = None
        self._is_initializing = False
        # Guard so overlapping polls don't stack (a bus scan can outlast the poll interval).
        self._poll_in_flight = False
        # ListItem name of the highlighted row — what START acts on.
        self._selected_name = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="splash-container"):
            with Center():
                yield Static(LOGO, id="ascii-art")
            with Center():
                yield Label("Scanning for compatible hardware…", id="status-label")
            with Center():
                yield ProgressBar(total=100, show_eta=False, id="init-progress")
            with Center():
                with Horizontal(id="device-row"):
                    yield ListView(id="device-list")
                    yield Button("START", id="start-btn", variant="success")
                    # Compact uninstall: reverses wifit3's driver/access changes.
                    yield Button("✕", id="uninstall-btn", variant="error")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#init-progress").display = False
        self.query_one("#start-btn", Button).disabled = True   # enabled once a card appears
        uninstall_btn = self.query_one("#uninstall-btn", Button)
        uninstall_btn.disabled = True
        uninstall_btn.tooltip = "Uninstall the wifit3 driver / access rule for the selected card"
        # Poll frequently — discovery opens no devices now, so a tight interval makes plugging
        # a card in feel instant. The first tick runs immediately (set_interval waits a full
        # period before its first fire).
        self._refresh_timer = self.set_interval(0.5, self.poll_usb)
        self.call_after_refresh(self.poll_usb)

    async def poll_usb(self) -> None:
        # Skip if a connect/install is running, or a prior scan is still in flight — the bus
        # scan can take ~1s on Windows, longer than the poll interval, so don't stack them.
        if self._is_initializing or self._poll_in_flight:
            return
        self._poll_in_flight = True
        try:
            interfaces = await self.device_manager.refresh()
            signature = tuple((i.name, i.description) for i in interfaces)
            if signature == self._last_signature:
                return
            self._last_signature = signature

            list_view = self.query_one("#device-list", ListView)
            list_view.clear()
            for iface in interfaces:
                list_view.append(ListItem(Label(iface.description), name=iface.name))

            status = self.query_one("#status-label", Label)
            start_btn = self.query_one("#start-btn", Button)
            uninstall_btn = self.query_one("#uninstall-btn", Button)
            if interfaces:
                status.update("[bold bright_green]Select a card and press START[/bold bright_green]")
                start_btn.disabled = False
                uninstall_btn.disabled = False
                # clear() reset index to None; re-arm the highlight so START has a target.
                if list_view.index is None:
                    list_view.index = 0
                    list_view.focus()
            else:
                status.update("Scanning for compatible hardware…")
                start_btn.disabled = True
                uninstall_btn.disabled = True
                self._selected_name = None
        finally:
            self._poll_in_flight = False

    def on_driver_progress(self, event: DriverProgress) -> None:
        """Connect-time progress, posted from the worker thread."""
        warn = self.app.theme_variables.get("text-warning", "yellow")
        self.query_one("#init-progress", ProgressBar).progress = event.percentage * 100
        self.query_one("#status-label", Label).update(f"[bold {warn}]{event.message}[/bold {warn}]")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._selected_name = event.item.name if event.item is not None else None

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row starts that card."""
        if self._is_initializing:
            return
        iface = self.device_manager.get_interface(event.item.name)
        if iface is not None:
            self.perform_start(iface)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._is_initializing or not self._selected_name:
            return
        iface = self.device_manager.get_interface(self._selected_name)
        if iface is None:
            return
        if event.button.id == "start-btn":
            self.perform_start(iface)
        elif event.button.id == "uninstall-btn":
            self.perform_uninstall(iface)

    @work(exclusive=True)
    async def perform_start(self, iface) -> None:
        """Start a card: try to connect; if that fails because it isn't WinUSB-bound, offer a
        one-time install and connect again. All the blocking work (connect, openability probe,
        install) runs off-thread so the UI stays responsive."""
        status = self.query_one("#status-label", Label)
        list_view = self.query_one("#device-list", ListView)
        start_btn = self.query_one("#start-btn", Button)

        self._is_initializing = True
        if self._refresh_timer:
            self._refresh_timer.pause()
        list_view.disabled = True
        start_btn.disabled = True

        def release():
            list_view.disabled = False
            start_btn.disabled = False
            self._is_initializing = False
            if self._refresh_timer:
                self._refresh_timer.resume()
            # Hand focus back to the picker — dismissing the install modal (or a failure)
            # otherwise leaves nothing focused.
            list_view.focus()

        try:
            # Happy path: just connect. Opening + init is inherent to using the card, so we
            # don't pre-probe — a WinUSB-bound card (the common case) connects with no extra
            # work or lag.
            if await self._connect(iface):
                return  # _connect switched to the scanner

            # Connect failed. The fix depends on the OS: Windows usually means "not
            # WinUSB-bound", Linux means "the kernel driver holds it + we lack node access".
            # Both run a blocking, only-on-failure probe before assuming so.
            await iface.close()
            openable = await asyncio.to_thread(self.device_manager.is_openable, iface)
            vid, pid, desc = iface.vid, iface.pid, iface.description

            async def _refind_and_connect(fail_msg: str) -> None:
                """After a setup action, re-scan, re-find the card by VID:PID, and connect.
                Soft-fails (status + release) if it vanished; raises ``fail_msg`` if it's back
                but still won't initialize."""
                await self.device_manager.refresh()
                self._last_signature = None
                again = self.device_manager.get_interface_by_vidpid(vid, pid)
                if again is None:
                    status.update("[bold red]Card not found after setup — replug and retry.[/bold red]")
                    release()
                    return
                if not await self._connect(again):
                    raise RuntimeError(fail_msg)

            if sys.platform == "win32":
                if openable:
                    raise RuntimeError("the card failed to initialize")  # a real fault → modal
                # Not openable on Windows → offer the one-time WinUSB install.
                if not await self.app.push_screen_wait(ConfirmInstallDialog(desc)):
                    status.update("[bold bright_green]Select a card and press START[/bold bright_green]")
                    release()
                    return
                status.update(f"[bold yellow]Installing WinUSB driver for {desc}… "
                              f"(up to a minute)[/bold yellow]")
                result = await asyncio.to_thread(install_winusb, vid, pid, name=desc)
                if not result.ok:
                    release()
                    if result.cancelled:
                        status.update("[yellow]Install cancelled.[/yellow]")
                    else:
                        status.update("[bold red]WinUSB install failed.[/bold red]")
                        bits = []
                        if result.wdi_code is not None:
                            bits.append(f"libwdi code {result.wdi_code}")
                        if result.detail:
                            bits.append(result.detail)
                        self.app.push_screen(SetupErrorDialog(
                            "WinUSB install failed", result.message, " · ".join(bits) or None))
                    return
                # The card re-enumerated under WinUSB — re-find it, then connect.
                await _refind_and_connect("the card failed to initialize after installing WinUSB")

            elif sys.platform.startswith("linux"):
                # On Linux a kernel-claimed card still reads as "openable", so the real signal
                # is whether the usbfs node is writable. Writable-but-failed = a genuine fault.
                needs_perm = await asyncio.to_thread(
                    self.device_manager.linux_needs_permission, iface)
                if not needs_perm:
                    raise RuntimeError("the card failed to initialize")
                # No node access → offer the one-time udev access rule (reuses the install
                # dialog with Linux wording — a missing REQUIRED link, same shape).
                others = len(ids_from_registry()) - 1
                if not await self.app.push_screen_wait(ConfirmInstallDialog(
                        desc,
                        title="Wifit3 needs one-time access to talk to this card",
                        link_label="Device Access",
                        warning=(
                            f"[bold $text-warning]One-time sudo setup:[/bold $text-warning]\n"
                            f"- Installs a [bold]udev rule file[/] for the current user "
                            f"([cyan]{current_user()}[/])\n"
                            f"- Allows Wifit3 to use [italic]any supported wireless cards[/italic] "
                            f"[bold]without sudo[/bold].\n"
                            f"- Does [bold]not[/bold] change how the cards work as normal Wi-Fi.\n"
                            f"- [bold green]Reversible:[/bold green] Press "
                            f"[bold white on red] ✕ [/bold white on red] to remove the rule."),
                        verb="Grant access for",
                        confirm_label="Grant access",
                        also=f" (+ {others} cards)" if others > 0 else "")):
                    status.update("[bold bright_green]Select a card and press START[/bold bright_green]")
                    release()
                    return
                status.update(f"[bold yellow]Granting access for {desc}… "
                              f"(one password prompt)[/bold yellow]")
                result = await asyncio.to_thread(
                    install_rule, node=self.device_manager.usb_node_path(iface))
                if not result.ok:
                    release()
                    if result.cancelled:
                        status.update("[yellow]Setup cancelled.[/yellow]")
                    else:
                        status.update("[bold red]Couldn't set up device access.[/bold red]")
                        self.app.push_screen(SetupErrorDialog(
                            "Linux device-access setup failed", result.message, result.detail))
                    return
                # install_rule chgrp's the live node as root before returning, so it's normally
                # writable the instant we get here. Confirm it (behind a spinner) before connecting
                # — connecting an unready node is the EACCES failure that used to bounce the user
                # back to the picker with a bogus "replug" message.
                self.app.push_screen(PropagatingDialog("Applying device access…"))
                try:
                    ready = await self.device_manager.linux_wait_for_access(
                        iface, want_writable=True)
                finally:
                    self.app.pop_screen()
                if not ready:
                    release()
                    self.app.push_screen(SetupErrorDialog(
                        "Device access didn't take effect",
                        f"The access rule is installed, but {desc} hasn't picked it up.",
                        "Unplug and replug the card, then press START."))
                    return
                await _refind_and_connect("the card failed to initialize after granting access")

            else:
                raise RuntimeError("the card failed to initialize")
        except Exception as e:
            logger.exception("Failed to start %s", getattr(iface, "description", "?"))
            status.update(f"[bold red]Could not start: {e}[/bold red]")
            self.query_one("#init-progress", ProgressBar).display = False
            release()

    @work(exclusive=True)
    async def perform_uninstall(self, iface) -> None:
        """Reverse wifit3's driver/access change for a card: WinUSB unbind (Windows) or remove
        the udev access rule (Linux). Both are privileged + blocking, so they run off-thread.
        The card returns to its normal Wi-Fi driver — Windows immediately, Linux on replug."""
        if sys.platform == "win32":
            os_kind = "win"
        elif sys.platform.startswith("linux"):
            os_kind = "linux"
        else:
            return  # no uninstall action on other platforms

        status = self.query_one("#status-label", Label)
        list_view = self.query_one("#device-list", ListView)
        start_btn = self.query_one("#start-btn", Button)
        uninstall_btn = self.query_one("#uninstall-btn", Button)

        self._is_initializing = True
        if self._refresh_timer:
            self._refresh_timer.pause()
        list_view.disabled = True
        start_btn.disabled = True
        uninstall_btn.disabled = True

        def release():
            list_view.disabled = False
            start_btn.disabled = False
            uninstall_btn.disabled = False
            self._is_initializing = False
            if self._refresh_timer:
                self._refresh_timer.resume()
            list_view.focus()

        try:
            if not await self.app.push_screen_wait(
                    ConfirmUninstallDialog(iface.description, os_kind)):
                status.update("[bold bright_green]Select a card and press START[/bold bright_green]")
                release()
                return
            status.update(f"[bold yellow]Removing wifit3 driver for {iface.description}…[/bold yellow]")
            # Drop our handle first so the unbind / rule reload isn't blocked by us holding it.
            await iface.close()
            if os_kind == "win":
                result = await asyncio.to_thread(restore_driver, iface.vid, iface.pid)
            else:
                result = await asyncio.to_thread(
                    remove_rule, node=self.device_manager.usb_node_path(iface))
        except Exception as e:
            logger.exception("Uninstall failed for %s", getattr(iface, "description", "?"))
            status.update(f"[bold red]Uninstall failed: {e}[/bold red]")
            release()
            return

        # Linux: remove_rule chowns the node back to root as root, so it's revoked on return.
        # Confirm it actually went unwritable so the card is really revoked, not just de-filed —
        # this is the bug where uninstall did nothing until a manual replug.
        revoked = True
        if os_kind == "linux" and result.ok:
            self.app.push_screen(PropagatingDialog("Revoking device access…"))
            try:
                revoked = await self.device_manager.linux_wait_for_access(
                    iface, want_writable=False)
            finally:
                self.app.pop_screen()

        # Re-scan so the list reflects the card's new binding state, then report.
        await self.device_manager.refresh()
        self._last_signature = None
        release()
        if result.ok and revoked:
            status.update(f"[bold green]{result.message}[/bold green]")
        elif result.ok and not revoked:
            self.app.push_screen(SetupErrorDialog(
                "Access rule removed",
                f"The rule is gone, but {iface.description} keeps access until it's replugged.",
                "Unplug and replug the card to fully revoke."))
        elif result.cancelled:
            status.update("[yellow]Uninstall cancelled.[/yellow]")
        else:
            status.update("[bold red]Uninstall failed.[/bold red]")
            self.app.push_screen(SetupErrorDialog(
                "Uninstall failed", result.message, result.detail))

    async def _connect(self, iface) -> bool:
        """Try to connect ``iface``; on success switch to the scanner and return True. Returns
        False if the card couldn't be opened/initialized — the caller decides whether that's a
        WinUSB issue worth offering an install for."""
        progress = self.query_one("#init-progress", ProgressBar)
        progress.display = True
        progress.progress = 0
        try:
            ok = await iface.connect(
                progress_cb=lambda p, m: self.post_message(DriverProgress(p, m)))
        except Exception as e:
            logger.info("connect() failed for %s: %s", iface.description, e)
            progress.display = False
            return False
        if not ok:
            progress.display = False
            return False
        self.app.active_interface = iface
        progress.progress = 100
        self.query_one("#status-label", Label).update(
            "[bold green]Ready — starting the scanner…[/bold green]")
        await asyncio.sleep(0.4)
        self.app.switch_screen("scanner")
        return True
