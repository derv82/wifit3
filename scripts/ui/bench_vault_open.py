"""Start at the splash, post v, print the time until the drawer is open with its first AP painted.
The drawer's 200 ms slide-in follows by construction and is not counted."""
import asyncio
import time

from vault_bench import aps, open_vault, terminal_size
from wifit3.ui.app import WifiteApp


async def main() -> None:
    app = WifiteApp(cli_log_level="unset")
    async with app.run_test(size=terminal_size()) as pilot:
        await pilot.pause()
        await asyncio.sleep(1.0)
        started = time.perf_counter()
        drawer, painted_at = await open_vault(app)
        print(f"{aps(drawer).row_count} APs at {'x'.join(map(str, terminal_size()))}: "
              f"{(painted_at - started) * 1000:.0f} ms from v to the first AP painted")


if __name__ == "__main__":
    asyncio.run(main())
