"""Windows WinUSB binding via the bundled wdi-simple.exe (libwdi). [DEVICE-SETUP.md Tier 1]

:func:`install_winusb` shells out to a vendored, *unsigned* ``wdi-simple.exe`` under UAC
elevation (``ShellExecuteW`` ``"runas"``) to bind a card to WinUSB so libusb can open it;
:func:`restore_driver` removes that binding so the card's native Wi-Fi driver reclaims it
(no pre-bind snapshot needed — the stock driver stays in the Windows driver store, so a
``pnputil /delete-driver <oem>.inf /uninstall`` + rescan re-points the device to it).

Both are privileged and must run off the Textual event loop (a ``@work`` thread). The exe
is built from pinned upstream libwdi (see ``bin/PROVENANCE.md``); because it is unsigned,
this path is gated behind explicit user action in the splash.
"""
from __future__ import annotations

import platform
from pathlib import Path

_BIN = Path(__file__).parent / "bin"

# CPU arch -> bundled-binary subdir. x64 only for now: libwdi's VS2022 workflow builds
# x64/Win32, so there's no arm64 wdi-simple.exe yet (DEVICE-SETUP.md open question).
_ARCH_DIRS = {"amd64": "win-x64", "x86_64": "win-x64"}


def wdi_simple_path() -> Path:
    """Absolute path to the bundled ``wdi-simple.exe`` for this CPU arch.

    Raises :class:`FileNotFoundError` if the arch isn't bundled or the binary is missing
    (e.g. an sdist/wheel that dropped the artifact)."""
    sub = _ARCH_DIRS.get(platform.machine().lower())
    if sub is None:
        raise FileNotFoundError(
            f"No bundled wdi-simple.exe for arch {platform.machine()!r} (x64 only)")
    exe = _BIN / sub / "wdi-simple.exe"
    if not exe.is_file():
        raise FileNotFoundError(f"Bundled wdi-simple.exe missing at {exe}")
    return exe


def install_winusb(vid: int, pid: int, iid: int = 0, name: str | None = None):
    """Bind ``vid:pid`` to WinUSB via elevated wdi-simple.exe. Wired in Tier-1 commit 2."""
    raise NotImplementedError("install_winusb lands in DEVICE-SETUP Tier-1 commit 2")


def restore_driver(vid: int, pid: int):
    """Drop the WinUSB binding so the native driver reclaims the card. Tier-1 commit 3."""
    raise NotImplementedError("restore_driver lands in DEVICE-SETUP Tier-1 commit 3")
