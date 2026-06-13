"""Windows WinUSB binding via the bundled wdi-simple.exe (libwdi).

:func:`install_winusb` shells out to a vendored, *unsigned* ``wdi-simple.exe`` under UAC
elevation (``ShellExecuteExW`` ``"runas"``) to bind a card to WinUSB so libusb can open it.
:func:`restore_driver` reverses that: it finds the WinUSB/libusb driver bound to the card
(SetupAPI, the same enumeration libwdi uses) and ``pnputil /delete-driver … /uninstall``s
it, so Windows re-points the card to its native Wi-Fi driver (still in the driver store —
no pre-bind snapshot needed). The lookup keys off the *service* (WinUSB/libusbK/libusb0),
so it also rolls back Zadig's bindings, not just ours.

Both actions are privileged and block until the elevated process exits, so they MUST run
off the Textual event loop (the splash offloads them to a thread). The exe is built from
pinned upstream libwdi (see ``bin/PROVENANCE.md``); because it is unsigned, this path is
gated behind explicit user action in the splash.
"""
from __future__ import annotations

import ctypes
import logging
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_BIN = Path(__file__).parent / "bin"

# CPU arch -> bundled-binary subdir. x64 only for now: libwdi's VS2022 workflow builds
# x64/Win32, so there's no arm64 wdi-simple.exe yet (RELEASE-PLAN.md §2d).
_ARCH_DIRS = {"amd64": "win-x64", "x86_64": "win-x64"}

_WDI_TYPE_WINUSB = 0                # wdi-simple --type 0
_WDI_PENDING_TIMEOUT_MS = 120_000  # wdi-simple --timeout: how long it waits for a pending install
_PROCESS_WAIT_MS = 180_000         # our cap on WaitForSingleObject so a wedged install can't hang

# Win32 constants.
_SEE_MASK_NOCLOSEPROCESS = 0x00000040  # keep hProcess open so we can wait + read the exit code
_SW_HIDE = 0
_WAIT_TIMEOUT = 0x00000102
_ERROR_CANCELLED = 1223            # user declined the UAC elevation prompt

# SetupAPI / registry constants for the restore-time driver lookup (mirrors libwdi.c).
_DIGCF_PRESENT = 0x00000002
_DIGCF_ALLCLASSES = 0x00000004
_SPDRP_HARDWAREID = 0x00000001
_SPDRP_SERVICE = 0x00000004
_DICS_FLAG_GLOBAL = 0x00000001
_DIREG_DRV = 0x00000002
_KEY_READ = 0x00020019
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_SUCCESS = 0

# Driver services that mean "this card is on a libusb-class driver we can roll back" —
# covers our WinUSB installs and Zadig's WinUSB / libusbK / libusb-win32 bindings.
_LIBUSB_SERVICES = frozenset({"winusb", "libusbk", "libusb0"})
# pnputil exit codes we treat as success (3010 = ERROR_SUCCESS_REBOOT_REQUIRED).
_PNPUTIL_OK = frozenset({0, 3010})

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


class _SHELLEXECUTEINFOW(ctypes.Structure):
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


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("ClassGuid", ctypes.c_byte * 16),
        ("DevInst", ctypes.c_ulong),
        ("Reserved", ctypes.c_void_p),
    ]


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
    detail: str | None = None   # wdi-simple's own last output line, for the error modal


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of removing a WinUSB/libusb binding so the native driver reclaims the card.

    ``cancelled`` is the declined-UAC case; ``detail`` carries the oemNN.inf that was
    removed (or the one we tried to) for the logs / Details box.
    """
    ok: bool
    message: str
    cancelled: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class _ElevatedRun:
    """Low-level result of a single elevated launch (see :func:`_run_elevated`)."""
    launched: bool        # did ShellExecuteExW start the process at all?
    win_error: int        # GetLastError when not launched (e.g. 1223 = UAC declined)
    exit_code: int | None  # signed process exit code, or None if launched-but-timed-out


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


def _build_args(vid: int, pid: int, iid: int | None = None, name: str | None = None,
                dest: str | None = None, log_level: int | None = None) -> list[str]:
    """wdi-simple.exe argv to bind ``vid:pid`` to WinUSB.

    VID/PID are passed as ``0x``-hex (wdi-simple strtol-parses either base, and hex matches
    how the rest of the codebase refers to them).

    ``iid`` (interface MI) is **omitted by default**. wdi-simple's ``-i`` flag *also* sets
    is_composite=TRUE, so passing it for a simple single-interface card makes the install
    target ``USB\\VID&PID&MI_00`` — which never matches the real ``USB\\VID&PID`` node, so the
    INF installs "successfully" but binds nothing and the card is left driverless. Pass
    ``iid`` only for genuinely composite devices (libwdi issue #206).

    ``dest`` is the driver-extraction dir: wdi-simple defaults it to the *relative*
    ``usb_driver``, which fails when the elevated process runs from ``C:\\Windows\\System32``
    (WDI_ERROR_ACCESS), so we always pass an absolute one. ``log_level`` (0=debug..4=none)
    cranks wdi-simple's own logging into the captured output."""
    args = [
        "--vid", f"0x{vid:04x}",
        "--pid", f"0x{pid:04x}",
        "--type", str(_WDI_TYPE_WINUSB),
        "--timeout", str(_WDI_PENDING_TIMEOUT_MS),
    ]
    if iid is not None:
        args += ["--iid", str(iid)]
    if name:
        args += ["--name", name]
    if dest:
        args += ["--dest", dest]
    if log_level is not None:
        args += ["--log", str(log_level)]
    return args


def _wdi_message(code: int) -> str:
    return _WDI_MESSAGES.get(code, f"The driver install failed (libwdi code {code}).")


def _signed32(dword: int) -> int:
    """Reinterpret an unsigned process exit code as a signed int32 (WDI codes are negative)."""
    return dword - 0x1_0000_0000 if dword >= 0x8000_0000 else dword


def _read_text(path: Path) -> str:
    """Read a captured log file (tolerant of console-codepage bytes); "" if absent."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _last_line(text: str) -> str:
    """The last non-blank line of wdi-simple's output — the most telling bit for the modal."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _restore_command(inf: str) -> str:
    """The ``cmd /c`` parameter string that removes ``inf`` then re-scans the bus.

    ``/delete-driver … /uninstall`` re-points every device on that package to a different
    driver (the native one, still in the store) before deleting it; ``/scan-devices`` then
    forces the rebind. ``&&`` chains so cmd's exit code reflects the delete on failure."""
    return f'/c pnputil /delete-driver "{inf}" /uninstall /force && pnputil /scan-devices'


def _run_elevated(file: str, params: str) -> _ElevatedRun:
    """Launch ``file params`` elevated (UAC), wait for it, and read its exit code.

    Windows-only and blocking — call OFF the event loop. A declined UAC prompt surfaces as
    ``launched=False`` with ``win_error == ERROR_CANCELLED``."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.restype = ctypes.c_bool
    shell32.ShellExecuteExW.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"          # the elevation verb -> UAC
    info.lpFile = file
    info.lpParameters = params
    info.nShow = _SW_HIDE          # hide the child's console; the UAC dialog still shows

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return _ElevatedRun(launched=False, win_error=ctypes.get_last_error(), exit_code=None)

    hproc = info.hProcess
    try:
        if kernel32.WaitForSingleObject(hproc, _PROCESS_WAIT_MS) == _WAIT_TIMEOUT:
            logger.warning("Elevated %s didn't exit within %d ms", file, _PROCESS_WAIT_MS)
            return _ElevatedRun(launched=True, win_error=0, exit_code=None)
        code = ctypes.c_ulong(0)
        kernel32.GetExitCodeProcess(hproc, ctypes.byref(code))
        return _ElevatedRun(launched=True, win_error=0, exit_code=_signed32(code.value))
    finally:
        kernel32.CloseHandle(hproc)


def install_winusb(vid: int, pid: int, iid: int | None = None,
                   name: str | None = None) -> InstallResult:
    """Bind ``vid:pid`` to WinUSB by running the bundled wdi-simple.exe **elevated**.

    Driver install is inherently privileged, so this raises one UAC prompt and blocks until
    the elevated process exits — call it OFF the Textual event loop. wdi-simple runs from a
    redirected batch so its console output (which can't pipe back across the UAC boundary) is
    captured to a log and echoed to ``wifit3.log``. Returns an :class:`InstallResult`; it does
    not raise for an install *failure* (reported via the result) — only for a broken
    environment (non-Windows, or a missing bundled exe)."""
    if sys.platform != "win32":
        raise RuntimeError("install_winusb is Windows-only")

    exe = wdi_simple_path()
    # Absolute, user-writable extraction dir — see _build_args() for why the default fails.
    dest = Path(tempfile.gettempdir()) / "wifit3_winusb"
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("WinUSB install: couldn't create extraction dir %s: %s", dest, e)
    logpath = dest / "wdi-simple.log"
    batpath = dest / "run-wdi.bat"

    args_str = subprocess.list2cmdline(
        _build_args(vid, pid, iid=iid, name=name, dest=str(dest), log_level=0))
    # Run wdi-simple from a one-shot batch that redirects its output to a log we read back. A
    # .bat sidesteps cmd /c's quoting traps (the command both starts with a quoted path and
    # redirects); the batch's exit code is wdi-simple's WDI return code.
    bat = f'@echo off\r\n"{exe}" {args_str} > "{logpath}" 2>&1\r\n'
    try:
        batpath.write_text(bat, encoding="mbcs")
    except OSError as e:
        return InstallResult(ok=False, message=f"Couldn't stage the installer: {e}")
    logger.info("WinUSB install (elevated): %s %s", exe.name, args_str)

    run = _run_elevated(str(batpath), "")
    output = _read_text(logpath)
    if output:
        logger.info("wdi-simple output:\n%s", output)

    if not run.launched:
        if run.win_error == _ERROR_CANCELLED:
            logger.info("WinUSB install: user declined the UAC prompt")
            return InstallResult(
                ok=False, cancelled=True,
                message="Elevation cancelled — WinUSB was not installed.")
        logger.warning("WinUSB install: ShellExecuteExW failed (WinError %d)", run.win_error)
        return InstallResult(
            ok=False, message=f"Could not launch the installer (WinError {run.win_error}).")
    if run.exit_code is None:
        return InstallResult(ok=False, detail=_last_line(output),
                             message="The driver installer didn't finish within 3 minutes.")

    wdi = run.exit_code
    logger.info("WinUSB install: wdi-simple exit=%d (%s)", wdi, _wdi_message(wdi))
    return InstallResult(ok=(wdi == 0), wdi_code=wdi, message=_wdi_message(wdi),
                         detail=_last_line(output) if wdi != 0 else None)


def _reg_prop(setupapi, hdev, data: _SP_DEVINFO_DATA, prop: int) -> str | None:
    """One device-registry string property (the first string for REG_MULTI_SZ ids)."""
    buf = ctypes.create_unicode_buffer(1024)
    size = ctypes.c_ulong(0)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev, ctypes.byref(data), prop, None,
        ctypes.cast(buf, ctypes.c_void_p), ctypes.sizeof(buf), ctypes.byref(size))
    return buf.value if ok else None


def _read_inf_path(setupapi, advapi32, hdev, data: _SP_DEVINFO_DATA) -> str | None:
    """The oemNN.inf bound to the device, from its driver key (DIREG_DRV -> "InfPath")."""
    hkey = setupapi.SetupDiOpenDevRegKey(
        hdev, ctypes.byref(data), _DICS_FLAG_GLOBAL, 0, _DIREG_DRV, _KEY_READ)
    if not hkey or hkey == _INVALID_HANDLE_VALUE:
        return None
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(ctypes.sizeof(buf))
        rc = advapi32.RegQueryValueExW(
            hkey, "InfPath", None, None, ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(size))
        return buf.value if rc == _ERROR_SUCCESS else None
    finally:
        advapi32.RegCloseKey(hkey)


def _find_winusb_inf(vid: int, pid: int) -> str | None:
    """The oemNN.inf of the WinUSB/libusb driver bound to ``vid:pid``, or ``None``.

    Mirrors libwdi's own enumeration: list present USB devices, match SPDRP_HARDWAREID on
    ``VID_xxxx&PID_xxxx``, confirm SPDRP_SERVICE is a libusb-class driver (so we never touch
    a card that's on its native Wi-Fi driver), then read the bound INF. Returns ``None`` if
    the card isn't present or isn't on a libusb-class driver. Windows-only."""
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong]
    setupapi.SetupDiEnumDeviceInfo.restype = ctypes.c_bool
    setupapi.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = ctypes.c_bool
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    setupapi.SetupDiOpenDevRegKey.restype = ctypes.c_void_p
    setupapi.SetupDiOpenDevRegKey.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_ulong, ctypes.c_ulong]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    advapi32.RegQueryValueExW.restype = ctypes.c_long
    advapi32.RegQueryValueExW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    advapi32.RegCloseKey.argtypes = [ctypes.c_void_p]

    hdev = setupapi.SetupDiGetClassDevsW(None, "USB", None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES)
    if not hdev or hdev == _INVALID_HANDLE_VALUE:
        return None
    needle = f"VID_{vid:04X}&PID_{pid:04X}"
    try:
        data = _SP_DEVINFO_DATA()
        data.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        i = 0
        while setupapi.SetupDiEnumDeviceInfo(hdev, i, ctypes.byref(data)):
            i += 1
            hwid = _reg_prop(setupapi, hdev, data, _SPDRP_HARDWAREID)
            if not hwid or needle not in hwid.upper():
                continue
            service = (_reg_prop(setupapi, hdev, data, _SPDRP_SERVICE) or "").lower()
            if service not in _LIBUSB_SERVICES:
                logger.info("Restore: %s is on service %r, not a libusb driver - skipping",
                            needle, service)
                return None
            inf = _read_inf_path(setupapi, advapi32, hdev, data)
            logger.info("Restore: %s bound to %s via service %s", needle, inf, service)
            return inf
        logger.info("Restore: no present device matched %s", needle)
        return None
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)


def restore_driver(vid: int, pid: int) -> RestoreResult:
    """Remove the WinUSB/libusb binding on ``vid:pid`` so the native driver reclaims it.

    Finds the bound oemNN.inf (SetupAPI) and elevates ``pnputil /delete-driver … /uninstall
    /force`` + ``/scan-devices``. Blocks on one UAC prompt — call OFF the event loop. Returns
    a :class:`RestoreResult`; raises only for a broken environment (non-Windows)."""
    if sys.platform != "win32":
        raise RuntimeError("restore_driver is Windows-only")

    inf = _find_winusb_inf(vid, pid)
    if inf is None:
        return RestoreResult(
            ok=False,
            message="Couldn't find a WinUSB/libusb driver bound to this card to remove.")

    comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
    params = _restore_command(inf)
    logger.info("Restore driver (elevated): %s %s", comspec, params)

    run = _run_elevated(comspec, params)
    if not run.launched:
        if run.win_error == _ERROR_CANCELLED:
            logger.info("Restore: user declined the UAC prompt")
            return RestoreResult(
                ok=False, cancelled=True,
                message="Elevation cancelled — the WinUSB driver was not removed.")
        logger.warning("Restore: ShellExecuteExW failed (WinError %d)", run.win_error)
        return RestoreResult(
            ok=False, message=f"Could not launch the uninstaller (WinError {run.win_error}).")
    if run.exit_code is None:
        return RestoreResult(ok=False, detail=inf,
                             message="The driver uninstall didn't finish in time.")

    code = run.exit_code
    if code in _PNPUTIL_OK:
        msg = "Removed the WinUSB driver — the card should return to normal Wi-Fi."
        if code == 3010:
            msg += " (A reboot may be needed to finish.)"
        logger.info("Restore: removed %s (pnputil exit=%d)", inf, code)
        return RestoreResult(ok=True, message=msg, detail=inf)
    logger.warning("Restore: pnputil failed for %s (exit=%d)", inf, code)
    return RestoreResult(
        ok=False, detail=inf, message=f"pnputil couldn't remove the driver (exit {code}).")
