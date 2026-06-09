"""Windows WinUSB binding via the bundled wdi-simple.exe (libwdi). [DEVICE-SETUP.md Tier 1]

:func:`install_winusb` shells out to a vendored, *unsigned* ``wdi-simple.exe`` under UAC
elevation (``ShellExecuteExW`` ``"runas"``) to bind a card to WinUSB so libusb can open it;
:func:`restore_driver` (Tier-1 commit 3) removes that binding so the card's native Wi-Fi
driver reclaims it.

Both are privileged and block until the elevated process exits, so they MUST run off the
Textual event loop (the splash offloads them to a thread). The exe is built from pinned
upstream libwdi (see ``bin/PROVENANCE.md``); because it is unsigned, this path is gated
behind explicit user action in the splash.
"""
from __future__ import annotations

import ctypes
import logging
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_BIN = Path(__file__).parent / "bin"

# CPU arch -> bundled-binary subdir. x64 only for now: libwdi's VS2022 workflow builds
# x64/Win32, so there's no arm64 wdi-simple.exe yet (DEVICE-SETUP.md open question).
_ARCH_DIRS = {"amd64": "win-x64", "x86_64": "win-x64"}

_WDI_TYPE_WINUSB = 0                # wdi-simple --type 0
_WDI_PENDING_TIMEOUT_MS = 120_000  # wdi-simple --timeout: how long it waits for a pending install
_PROCESS_WAIT_MS = 180_000         # our cap on WaitForSingleObject so a wedged install can't hang

# Win32 constants.
_SEE_MASK_NOCLOSEPROCESS = 0x00000040  # keep hProcess open so we can wait + read the exit code
_SW_HIDE = 0
_WAIT_TIMEOUT = 0x00000102
_ERROR_CANCELLED = 1223            # user declined the UAC elevation prompt

# libwdi wdi_error codes (libwdi.h) -> human message. wdi-simple's process exit code IS the
# WDI return code; the enum values are negative, so the DWORD is sign-corrected first.
_WDI_MESSAGES = {
    0:   "WinUSB installed.",
    -1:  "I/O error while installing the driver.",
    -2:  "Internal error (invalid parameter).",
    -3:  "Access denied while installing the driver.",
    -4:  "The card was unplugged before the install finished.",
    -5:  "The card wasn't found on the USB bus.",
    -6:  "The card is busy — another install may be in progress.",
    -7:  "The driver install timed out.",
    -8:  "Internal error (overflow).",
    -9:  "Windows is still finishing a previous driver install — wait a moment and retry.",
    -10: "The install was interrupted.",
    -11: "Out of resources while installing the driver.",
    -12: "WinUSB isn't supported for this card.",
    -13: "A WinUSB driver is already installed for this card.",
    -14: "Install cancelled.",
    -15: "Administrator rights are required (the elevation prompt was declined or blocked).",
    -16: "32/64-bit mismatch (WOW64) — wrong wdi-simple build for this Windows.",
    -17: "Windows rejected the generated driver INF.",
    -18: "The driver catalog (.cat) is missing.",
    -19: "Windows refused the unsigned driver package.",
    -99: "The driver install failed (unspecified libwdi error).",
}


@dataclass(frozen=True)
class InstallResult:
    """Outcome of an elevated wdi-simple.exe run.

    ``ok`` is all the happy path needs; ``cancelled`` flags the benign "user declined the
    UAC prompt" case (worth a softer message than a real failure); ``wdi_code`` is the raw
    libwdi return code, surfaced in the error modal's Details box and the logs.
    """
    ok: bool
    message: str
    cancelled: bool = False
    wdi_code: int | None = None


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


def _build_args(vid: int, pid: int, iid: int = 0, name: str | None = None) -> list[str]:
    """wdi-simple.exe argv to bind ``vid:pid`` to WinUSB.

    VID/PID are passed as ``0x``-hex (wdi-simple strtol-parses either base, and hex matches
    how the rest of the codebase refers to them). ``iid`` is the interface MI — 0 for the
    single-interface cards we target."""
    args = [
        "--vid", f"0x{vid:04x}",
        "--pid", f"0x{pid:04x}",
        "--type", str(_WDI_TYPE_WINUSB),
        "--iid", str(iid),
        "--timeout", str(_WDI_PENDING_TIMEOUT_MS),
    ]
    if name:
        args += ["--name", name]
    return args


def _wdi_message(code: int) -> str:
    return _WDI_MESSAGES.get(code, f"The driver install failed (libwdi code {code}).")


def _signed32(dword: int) -> int:
    """Reinterpret an unsigned process exit code as a signed int32 (WDI codes are negative)."""
    return dword - 0x1_0000_0000 if dword >= 0x8000_0000 else dword


def install_winusb(vid: int, pid: int, iid: int = 0, name: str | None = None) -> InstallResult:
    """Bind ``vid:pid`` to WinUSB by running the bundled wdi-simple.exe **elevated**.

    Driver install is inherently privileged, so this raises one UAC prompt and blocks until
    the elevated process exits — call it OFF the Textual event loop. Returns an
    :class:`InstallResult`; it does not raise for an install *failure* (reported via the
    result) — only for a broken environment (non-Windows, or a missing bundled exe)."""
    if sys.platform != "win32":
        raise RuntimeError("install_winusb is Windows-only")

    exe = wdi_simple_path()
    params = subprocess.list2cmdline(_build_args(vid, pid, iid, name))
    logger.info("WinUSB install (elevated): %s %s", exe.name, params)

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIcon", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    shell32.ShellExecuteExW.restype = ctypes.c_bool
    shell32.ShellExecuteExW.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"          # the elevation verb -> UAC
    info.lpFile = str(exe)
    info.lpParameters = params
    info.nShow = _SW_HIDE          # hide wdi-simple's console; the UAC dialog still shows

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == _ERROR_CANCELLED:
            logger.info("WinUSB install: user declined the UAC prompt")
            return InstallResult(
                ok=False, cancelled=True,
                message="Elevation cancelled — WinUSB was not installed.")
        logger.warning("WinUSB install: ShellExecuteExW failed (WinError %d)", err)
        return InstallResult(
            ok=False, message=f"Could not launch the installer (WinError {err}).")

    hproc = info.hProcess
    try:
        if kernel32.WaitForSingleObject(hproc, _PROCESS_WAIT_MS) == _WAIT_TIMEOUT:
            logger.warning("WinUSB install: wdi-simple didn't exit within %d ms", _PROCESS_WAIT_MS)
            return InstallResult(ok=False, message="The driver installer didn't finish in time.")
        code = ctypes.c_ulong(0)
        kernel32.GetExitCodeProcess(hproc, ctypes.byref(code))
        wdi = _signed32(code.value)
    finally:
        kernel32.CloseHandle(hproc)

    logger.info("WinUSB install: wdi-simple exit=%d (%s)", wdi, _wdi_message(wdi))
    return InstallResult(ok=(wdi == 0), wdi_code=wdi, message=_wdi_message(wdi))


def restore_driver(vid: int, pid: int):
    """Drop the WinUSB binding so the native driver reclaims the card. Tier-1 commit 3."""
    raise NotImplementedError("restore_driver lands in DEVICE-SETUP Tier-1 commit 3")
