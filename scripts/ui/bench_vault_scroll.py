"""Open the vault from the splash, press Down each time an AP is painted until the last one is; print the
total, the median step and the 5 slowest. A step runs from posting Down until its AP is painted."""
import asyncio
import statistics
import time

from textual import events

from vault_bench import aps, bssid_at, open_vault, painted, shown_state, terminal_size, wait_for
from wifit3.ui.app import WifiteApp


async def main() -> None:
    app = WifiteApp(cli_log_level="unset")
    async with app.run_test(size=terminal_size()) as pilot:
        await pilot.pause()
        drawer, _ = await open_vault(app)
        await pilot.pause()
        table = aps(drawer)
        steps: list[tuple[str, float]] = []
        for row in range(table.cursor_row + 1, table.row_count):
            started = time.perf_counter()
            app.post_message(events.Key("down", None))
            bssid = bssid_at(table, row)
            await wait_for(lambda: shown_state(drawer, bssid))
            steps.append((app.vault.ssid(bssid) or bssid, await painted(drawer) - started))
        total = sum(seconds for _, seconds in steps)
        median = statistics.median(seconds for _, seconds in steps)
        print(f"{table.row_count} APs at {'x'.join(map(str, terminal_size()))}: {total * 1000:.0f} ms to paint "
              f"the last AP, median step {median * 1000:.0f} ms")
        slowest = sorted(steps, key=lambda step: step[1], reverse=True)[:5]
        print("slowest: " + ", ".join(f"{ssid} {seconds * 1000:.0f} ms" for ssid, seconds in slowest))


if __name__ == "__main__":
    asyncio.run(main())
