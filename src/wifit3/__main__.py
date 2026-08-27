"""Entry point for ``python -m wifit3`` and the ``wifit3`` console script."""


async def _smoke() -> None:
    """Headless self-test: prove the PyInstaller bundle is intact, then exit. Used by CI to
    catch bundling breaks the unit-test import-smoke can't.

    Three checks:
      1. The bundled libusb shared lib is where ``libusb_package.get_library_path()`` looks
         (``libusb_package/libusb-1.0.*``) and actually loads. A onefile build can misplace it,
         which breaks USB enumeration with "No backend available". We deliberately do NOT
         ``libusb_init``/enumerate here: CI runners have no USB subsystem (no ``/dev/bus/usb``),
         so init legitimately fails there. That's a runtime-env concern, not a packaging break.
      2. ``App.run_test()`` mounts every screen headless (no TTY), pulling the widget .tcss and
         logo assets that a broken ``collect_all`` would silently drop.
      3. ``supported_ids()`` is non-empty: the pkgutil chip-discovery walk only enumerates
         drivers PyInstaller actually collected, so an empty map means the bundle shipped with
         no drivers and the app would launch but show zero interfaces.
    """
    import ctypes
    import os

    from libusb_package import get_library_path

    lib = get_library_path()
    if not (lib and os.path.isfile(str(lib))):
        raise RuntimeError(f"bundled libusb not found via libusb_package: {lib!r}")
    ctypes.CDLL(str(lib))  # must load from the bundle (deps resolved), not just exist on disk

    from wifit3.ui.app import WifiteApp

    app = WifiteApp()
    async with app.run_test() as pilot:
        await pilot.pause()

    # Chip discovery actually finds drivers. supported_ids() walks wifit3.chips via pkgutil; if
    # PyInstaller didn't collect the dynamically-imported chip packages the map is empty and the
    # app launches fine but shows zero interfaces. That is the break this check exists to catch.
    from wifit3.device.manager import supported_ids

    if not supported_ids():
        raise RuntimeError("chip discovery found no driver packages (PyInstaller bundling break)")


def main() -> None:
    """Parse CLI args, then run the headless smoke test or launch the TUI."""
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

    # WIFIT3_LOG=off opts out, =debug and =trace bumps to the firehose.
    WifiteApp(default_log_level="info").run()


if __name__ == "__main__":
    # Frozen (PyInstaller) builds use the `spawn` start method, so each
    # ProcessPoolExecutor worker (the WEP cracker) re-execs this exe. freeze_support()
    # makes that re-exec run the worker bootstrap and exit, instead of launching a
    # second TUI. It is a no-op for normal `python -m wifit3` / console-script runs.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
