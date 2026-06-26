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

from wifit3.errors import BringUpError, BringUpPermissionsError, WifiteFatalError
from wifit3.ui.ansi_art import make_black_transparent
from wifit3.setup import target_for_vidpid
from wifit3.setup.linux import current_user, install_rule, remove_rule
from wifit3.setup.windows import install_winusb, restore_driver
from wifit3.ui.screens.confirm_install import ConfirmInstallDialog
from wifit3.ui.screens.confirm_uninstall import ConfirmUninstallDialog
from wifit3.ui.screens.setup_error import SetupErrorDialog
from wifit3.ui.screens.fatal_error import FatalErrorModal
from wifit3.ui.screens.propagating import PropagatingDialog
from wifit3.wlan.manager import WlanDeviceManager

logger = logging.getLogger(__name__)


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
            return make_black_transparent(
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
                # Persistent failure line. poll_usb only ever touches #status-label, so an error
                # parked here survives the next bus scan (the status line gets overwritten ~2x/s).
                yield Label("", id="error-label")
            with Center():
                with Horizontal(id="device-row"):
                    yield ListView(id="device-list")
                    yield Button("START", id="start-btn", variant="success")
                    # Compact uninstall: reverses wifit3's driver/access changes.
                    yield Button("✕", id="uninstall-btn", variant="error")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#init-progress").display = False
        self.query_one("#error-label").display = False
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
        except WifiteFatalError as err:
            # Unrecoverable (e.g. no USB backend) and it surfaces on the very first scan — stop
            # polling and replace the splash with the Quit-only fatal modal.
            self._refresh_timer.stop()
            self.app.push_screen(FatalErrorModal(err))
        finally:
            self._poll_in_flight = False

    def on_driver_progress(self, event: DriverProgress) -> None:
        """Connect-time progress, posted from the worker thread."""
        warn = self.app.theme_variables.get("text-warning", "yellow")
        self.query_one("#init-progress", ProgressBar).progress = event.percentage * 100
        self.query_one("#status-label", Label).update(f"[bold {warn}]{event.message}[/bold {warn}]")

    def _show_error(self, message: str) -> None:
        """Surface a recoverable bring-up failure: a persistent red label (which poll_usb leaves
        alone, unlike the status line) plus a toast."""
        label = self.query_one("#error-label", Label)
        label.update(f"[bold red]⚠ {message}[/bold red]")
        label.display = True
        self.notify(message, title="Card bring-up failed", severity="error")

    def _clear_error(self) -> None:
        label = self.query_one("#error-label", Label)
        label.update("")
        label.display = False

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

        self._clear_error()
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
            bringup_err = None
            try:
                if await self._connect(iface):
                    return
            except BringUpError as e:
                bringup_err = e

            needs_access = isinstance(bringup_err, BringUpPermissionsError)
            if bringup_err is None:
                await iface.close()
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
                openable = not needs_access and await asyncio.to_thread(
                    self.device_manager.is_openable, iface)
                if openable:
                    raise bringup_err or RuntimeError(
                        "the card opens but failed to initialize — replug and try again")
                # Not WinUSB-bound → offer the one-time WinUSB install.
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
                tainted = await asyncio.to_thread(
                    self.device_manager.linux_kernel_driver_bound, iface)
                no_access = needs_access or await asyncio.to_thread(
                    self.device_manager.linux_needs_permission, iface)
                if not tainted and not no_access:
                    # Cold + accessible but still wouldn't init → a genuine bring-up fault, not a
                    # setup gap.
                    raise bringup_err or RuntimeError(
                        "the card has access but failed to initialize — replug and try again")
                target = target_for_vidpid(vid, pid)
                if target is None:
                    raise bringup_err or RuntimeError(
                        "this card isn't a supported chipset for setup")
                # Offer to hand wifit3 complete control of the chipset (the Linux analog of binding
                # WinUSB): blacklist the kernel driver so it stops grabbing + tainting the card, and
                # grant raw USB access. One sudo prompt; reversible via the ✕ button.
                if not await self.app.push_screen_wait(ConfirmInstallDialog(
                        desc,
                        title="Give wifit3 complete control of this chipset?",
                        link_label="Take control",
                        warning=(
                            f"[bold $text-warning]One-time sudo setup[/bold $text-warning] "
                            f"(user [cyan]{current_user()}[/]):\n"
                            f"- [bold]Blacklists the kernel driver[/] for this chipset so wifit3 "
                            f"drives it from a clean cold-boot state.\n"
                            f"- [bold]This card stops working as a normal Wi-Fi adapter[/] until "
                            f"you uninstall.\n"
                            f"- Grants raw USB access [bold]without sudo[/bold].\n"
                            f"- [bold green]Reversible:[/bold green] Press "
                            f"[bold white on red] ✕ [/bold white on red] to hand it back."),
                        verb="Take control of",
                        confirm_label="Take control")):
                    status.update("[bold bright_green]Select a card and press START[/bold bright_green]")
                    release()
                    return
                status.update(f"[bold yellow]Taking control of {desc}… "
                              f"(one password prompt)[/bold yellow]")
                result = await asyncio.to_thread(
                    install_rule, target, node=self.device_manager.usb_node_path(iface))
                if not result.ok:
                    release()
                    if result.cancelled:
                        status.update("[yellow]Setup cancelled.[/yellow]")
                    else:
                        status.update("[bold red]Couldn't take control of the chipset.[/bold red]")
                        self.app.push_screen(SetupErrorDialog(
                            "Linux device-control setup failed", result.message, result.detail))
                    return
                # The blacklist only guarantees a cold card on the NEXT enumeration, and the card is
                # still warm from the kernel's firmware upload — so require a physical replug rather
                # than connecting the tainted device now.
                self.query_one("#init-progress", ProgressBar).display = False
                status.update(f"[bold green]✓ {desc} is now wifit3's. "
                              f"Unplug and replug it, then press START.[/bold green]")
                release()
                return

            else:
                raise bringup_err or RuntimeError("the card failed to initialize")
        except BringUpError as e:
            logger.warning("Bring-up failed for %s: %s", getattr(iface, "description", "?"), e)
            detail = f": {e.detail}" if e.detail else ""
            self._show_error(f"{getattr(iface, 'description', 'Card')} — {e.stage} failed{detail}")
            self.query_one("#init-progress", ProgressBar).display = False
            release()
        except Exception as e:
            logger.exception("Failed to start %s", getattr(iface, "description", "?"))
            self._show_error(f"Could not start {getattr(iface, 'description', 'card')}: {e}")
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
                target = target_for_vidpid(iface.vid, iface.pid)
                if target is None:
                    status.update("[bold red]This card isn't a supported chipset.[/bold red]")
                    release()
                    return
                result = await asyncio.to_thread(
                    remove_rule, target, node=self.device_manager.usb_node_path(iface))
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
        except BringUpError:
            # A genuine post-open bring-up fault (firmware/init/…) — release the partial open,
            # then let perform_start surface it. (A card that won't even open raises a plain
            # USBError below → the install flow.)
            progress.display = False
            await iface.close()
            raise
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
