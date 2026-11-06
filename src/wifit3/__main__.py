"""Entry point for ``python -m wifit3`` and the ``wifit3`` console script."""

import os
import subprocess
import sys
import ctypes
import shutil


def setup_termux_environment() -> None:
    """Автоматическая подготовка окружения Termux: создание mock-пакета и установка termux-api, libusb."""
    # Проверяем, что запуск происходит именно в среде Termux
    if "com.termux" in sys.executable or os.path.exists("/data/data/com.termux"):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mock_dir = os.path.join(base_dir, "libusb_package")
        mock_init = os.path.join(mock_dir, "__init__.py")

        # Проверяем и устанавливаем системный libusb через pkg
        termux_lib_path = "/data/data/com.termux/files/usr/lib/libusb-1.0.so"
        has_termux_usb = shutil.which("termux-usb") is not None
        if not has_termux_usb:
            print("[*] termux-usb not found. Installing termux-api...")
            try:
                subprocess.run(["pkg", "install", "termux-api", "-y"], check=True)
                print("[+] termux-api installed successfully!")
            except Exception as e:
                print(f"[-] Failed to install termux-api via pkg automatically: {e}")
                print("[-] Please run 'pkg install termux-api' manually.")
        if not os.path.exists(termux_lib_path):
            print("[*] Termux detected. Installing system dependency: libusb...")
            try:
                # Обновляем репозитории и ставим libusb без лишних вопросов
                subprocess.run(["pkg", "update", "-y"], check=False)
                subprocess.run(["pkg", "install", "libusb", "-y"], check=True)
                print("[+] libusb installed successfully!")
            except Exception as e:
                print(f"[-] Failed to install libusb via pkg automatically: {e}")
                print("[-] Please run 'pkg install libusb' manually.")


# Инициализируем окружение Termux на самом раннем этапе до импорта остальных модулей
setup_termux_environment()


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

    from libusb_package import get_library_path

    lib = get_library_path()
    if not (lib and os.path.isfile(str(lib))):
        raise RuntimeError(f"bundled libusb not found via libusb_package: {lib!r}")
    ctypes.CDLL(str(lib))  # must load from the bundle (deps resolved), not just exist on disk

    from wifit3.ui.app import WifiteApp

    app = WifiteApp(cli_log_level="unset")
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

    parser = argparse.ArgumentParser(prog="wifit3", description="USB Wireless Auditor")
    parser.add_argument("--version", action="version", version=f"wifit3 {__version__}")
    parser.add_argument("--smoke", action="store_true", help="TEST ONLY: Run headless, render, exit 0")
    parser.add_argument("--quiet", action="store_true", help="Do not emit any logs")
    parser.add_argument("--debug", action="store_true", help="Emit verbose debug logs")
    parser.add_argument("--trace", action="store_true", help="Emit very verbose trace logs")
    args = parser.parse_args()

    if args.smoke:
        import asyncio

        # 60s ceiling so a hung mount fails CI instead of stalling the runner.
        asyncio.run(asyncio.wait_for(_smoke(), timeout=60))
        return

    # Lazy import for WEP cracker ProcessPoolExecutor case
    from wifit3.ui.app import WifiteApp

    cli_log_level = None
    if args.debug:
        cli_log_level = "debug"
    if args.trace:
        cli_log_level = "trace"
    if args.quiet:
        cli_log_level = "quiet"

    # Textual's stderr has fileno -1, which kills multiprocessing's resource tracker if it starts
    # later (Linux): crashed every ProcessPoolExecutor (WEP crack, Pixie). No tracker on Windows.
    from multiprocessing import resource_tracker
    try:
        resource_tracker.ensure_running()
    except Exception:
        pass

    WifiteApp(cli_log_level=cli_log_level).run()


if __name__ == "__main__":
    # Frozen (PyInstaller) builds use the `spawn` start method, so each
    # ProcessPoolExecutor worker (the WEP cracker) re-execs this exe. freeze_support()
    # makes that re-exec run the worker bootstrap and exit, instead of launching a
    # second TUI. It is a no-op for normal `python -m wifit3` / console-script runs.
    import multiprocessing

    multiprocessing.freeze_support()
    main()
