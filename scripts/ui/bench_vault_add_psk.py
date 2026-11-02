"""Add a WPA PSK to Heavy-200 through the Add key modal; print the time from posting Enter in the modal
until the item pane has painted the new key."""
import asyncio
import time

from textual import events
from textual.widgets import Input

from vault_bench import bssid_of, open_vault, painted, select, shown_state, terminal_size, wait_for
from wifit3.ui.app import WifiteApp
from wifit3.ui.vault.modals.add_credential import AddCredentialModal

PSK = "typed-in-password"


async def main() -> None:
    app = WifiteApp(cli_log_level="unset")
    async with app.run_test(size=terminal_size()) as pilot:
        await pilot.pause()
        drawer, _ = await open_vault(app)
        bssid = bssid_of(app, "Heavy-200")
        await select(drawer, bssid)
        await asyncio.sleep(1.0)
        app.post_message(events.Key("a", "a"))
        modal = await wait_for(lambda: app.screen if isinstance(app.screen, AddCredentialModal) else None)
        value = await wait_for(lambda: modal.query_one("#value", Input))
        await wait_for(lambda: value.has_focus)
        value.value = PSK
        await asyncio.sleep(0.5)
        started = time.perf_counter()
        app.post_message(events.Key("enter", None))
        await wait_for(lambda: app.screen is drawer and (state := shown_state(drawer, bssid))
                       and any(key.value == PSK for key in state.keys))
        print(f"Heavy-200 at {'x'.join(map(str, terminal_size()))}: "
              f"{(await painted(drawer) - started) * 1000:.0f} ms from Enter to the new key painted")


if __name__ == "__main__":
    asyncio.run(main())
