"""Entry point for ``python -m wifit3`` and the ``wifit3`` console script."""


async def _smoke() -> None:
    """Headless self-test: boot the full TUI, render a frame, exit. Used by CI to catch
    PyInstaller bundling breaks the import-smoke can't (missing libusb DLL, missing Textual
    widget .tcss or ANSI assets that only load at mount).

    Two checks, in order of what they prove about the bundle:
      1. ``WlanDeviceManager().refresh()`` loads the bundled libusb backend — a missing DLL
         raises WifiteFatalError here (no card needed; an empty scan is success).
      2. ``App.run_test()`` mounts every screen headless (no TTY), pulling the widget .tcss
         and logo assets that a broken ``collect_all`` would silently drop.
    """
    from wifit3.ui.app import WifiteApp
    from wifit3.wlan.manager import WlanDeviceManager

    await WlanDeviceManager().refresh()

    app = WifiteApp()
    async with app.run_test() as pilot:
        await pilot.pause()


def main() -> None:
    import argparse

    from wifit3 import __version__

    parser = argparse.ArgumentParser(prog="wifit3", description="Userland 802.11 wireless auditor.")
    parser.add_argument("--version", action="version", version=f"wifit3 {__version__}")
    parser.add_argument(
        "--smoke", action="store_true",
        help="Boot the TUI headless, render one frame, and exit 0 (CI bundling self-test).")
    args = parser.parse_args()

    if args.smoke:
        import asyncio

        # 60s ceiling so a hung mount fails CI instead of stalling the runner.
        asyncio.run(asyncio.wait_for(_smoke(), timeout=60))
        return

    # Import inside main(), not at module top: the WEP cracker's
    # ProcessPoolExecutor re-imports this module to spawn workers, which must
    # not drag in Textual + the whole UI just to run RC4 math.
    from wifit3.ui.app import WifiteApp

    WifiteApp().run()


if __name__ == "__main__":
    # Frozen (PyInstaller) builds use the `spawn` start method, so each
    # ProcessPoolExecutor worker (the WEP cracker) re-execs this exe. freeze_support()
    # makes that re-exec run the worker bootstrap and exit, instead of launching a
    # second TUI. It is a no-op for normal `python -m wifit3` / console-script runs.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
