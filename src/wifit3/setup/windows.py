"""Windows WinUSB binding via the bundled wdi-simple.exe (libwdi)."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from wifit3.chips.driver import DeviceID
from wifit3.device.manager import device as find_device
from wifit3.setup.base import Prompter, Setup, SetupResult

logger = logging.getLogger(__name__)

_BIN = Path(__file__).parent / "bin"

_ARCH_DIRS = {"amd64": "win-x64", "x86_64": "win-x64", "arm64": "win-arm64"}

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

_LIBUSB_SERVICES = frozenset({"winusb", "libusbk", "libusb0"})
_PNPUTIL_OK = frozenset({0, 3010})  # Exit codes, 3010=ERROR_SUCCESS_REBOOT_REQUIRED

_LIBWDI_DEBUG_LOG_PREFIX = re.compile(r"^libwdi:debug\s*")

# libwdi wdi_error codes (libwdi.h) -> human message.
_WDI_MESSAGES = {
    0:   "WinUSB installed.",
    -1:  "I/O error while installing the driver.",
    -2:  "Internal error (invalid parameter).",
    -3:  "Access denied while installing the driver.",
    -4:  "The card was unplugged before the install finished.",
    -5:  "The card wasn't found on the USB bus.",
    -6:  "The card is busy. Another install may be in progress.",
    -7:  "The driver install timed out.",
    -8:  "Internal error (overflow).",
    -9:  "Windows is still finishing a previous driver install. Wait a moment and retry.",
    -10: "The install was interrupted.",
    -11: "Out of resources while installing the driver.",
    -12: "WinUSB isn't supported for this card.",
    -13: "A WinUSB driver is already installed for this card.",
    -14: "Install cancelled.",
    -15: "Administrator rights are required (the elevation prompt was declined or blocked).",
    -16: "32/64-bit mismatch (WOW64): wrong wdi-simple build for this Windows.",
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
    """Installation data."""
    ok: bool
    message: str
    cancelled: bool = False
    wdi_code: int | None = None
    detail: str | None = None   # wdi-simple's own last output line


@dataclass(frozen=True)
class UninstallResult:
    ok: bool
    message: str
    cancelled: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class _ElevatedRun:
    """See :func:`_launch_elevated`."""
    launched: bool        # did ShellExecuteExW start the process at all?
    win_error: int        # GetLastError when not launched (e.g. 1223 = UAC declined)
    exit_code: int | None  # signed exit code; None until _wait_elevated, or on timeout
    hproc: int | None = None  # process handle from launch, consumed by _wait_elevated


def wdi_simple_path() -> Path:
    sub = _ARCH_DIRS.get(platform.machine().lower())
    if sub is None:
        raise FileNotFoundError(
            f"No bundled wdi-simple.exe for arch {platform.machine()!r} (x64 and arm64 only)")
    exe = _BIN / sub / "wdi-simple.exe"
    if not exe.is_file():
        raise FileNotFoundError(f"Bundled wdi-simple.exe missing at {exe}")
    return exe


def _winusb_dir() -> Path:
    return Path(tempfile.gettempdir()) / "wifit3_winusb"


def winusb_log_path() -> Path:
    return _winusb_dir() / "wdi-simple.log"


def _build_args(vid: int, pid: int, iid: int | None = None, name: str | None = None,
                dest: str | None = None, log_level: int | None = None) -> list[str]:
    """wdi-simple.exe argv to bind ``vid:pid`` to WinUSB.."""
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
    """Unsigned exit -> signed int32."""
    return dword - 0x1_0000_0000 if dword >= 0x8000_0000 else dword


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _last_line(text: str) -> str:
    """The last non-blank line of wdi-simple's output: the most telling bit for the modal."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _restore_command(inf: str) -> str:
    """Removes ``inf`` & re-scans bus. ``/uninstall`` restores previous driver."""
    return f'/c pnputil /delete-driver "{inf}" /uninstall /force && pnputil /scan-devices'


def _launch_elevated(file: str, params: str) -> _ElevatedRun:
    """Show UAC prompt; return once dismissed. Cancel=``launched=False``, Accept=sets ``hproc``."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.restype = ctypes.c_bool
    shell32.ShellExecuteExW.argtypes = [ctypes.c_void_p]

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"          # the elevation verb -> UAC
    info.lpFile = file
    info.lpParameters = params
    info.nShow = _SW_HIDE          # hide the child's console; the UAC dialog still shows

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return _ElevatedRun(launched=False, win_error=ctypes.get_last_error(), exit_code=None)
    return _ElevatedRun(launched=True, win_error=0, exit_code=None, hproc=info.hProcess)


def _wait_elevated(run: _ElevatedRun) -> _ElevatedRun:
    """Block until the launched process exits, retrieve install exit code."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.restype = ctypes.c_bool
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        if kernel32.WaitForSingleObject(run.hproc, _PROCESS_WAIT_MS) == _WAIT_TIMEOUT:
            logger.warning("Elevated process didn't exit within %d ms", _PROCESS_WAIT_MS)
            return run
        code = ctypes.c_ulong(0)
        kernel32.GetExitCodeProcess(run.hproc, ctypes.byref(code))
        return replace(run, exit_code=_signed32(code.value))
    finally:
        kernel32.CloseHandle(run.hproc)


def _run_elevated(file: str, params: str) -> _ElevatedRun:
    """Executes ``file``, blocks until exit. See :func:`_wait_elevated`."""
    run = _launch_elevated(file, params)
    return _wait_elevated(run) if run.launched else run


@dataclass(frozen=True)
class _PendingInstall:
    """A WinUSB install staged and past the UAC prompt."""
    logpath: Path
    run: _ElevatedRun | None = None
    error: InstallResult | None = None

    @property
    def launched(self) -> bool:
        return self.run is not None and self.run.launched


def _launch_winusb(vid: int, pid: int, iid: int | None = None,
                   name: str | None = None) -> _PendingInstall:
    """Starts WinUSB installation, returns once the install begins."""
    if sys.platform != "win32":
        raise RuntimeError("WinUSB install is Windows-only")

    exe = wdi_simple_path()
    dest = _winusb_dir()  # Absolute, user-writable extraction dir.
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("WinUSB install: couldn't create extraction dir %s: %s", dest, e)
    logpath = winusb_log_path()
    batpath = dest / "run-wdi.bat"

    args_str = subprocess.list2cmdline(
        _build_args(vid, pid, iid=iid, name=name, dest=str(dest), log_level=0))
    # .bat sidesteps cmd /c's quoting traps
    bat = f'@echo off\r\n"{exe}" {args_str} > "{logpath}" 2>&1\r\n'
    try:
        batpath.write_text(bat, encoding="mbcs")
    except OSError as e:
        return _PendingInstall(logpath=logpath,
                               error=InstallResult(ok=False, message=f"Couldn't stage the installer: {e}"))
    logger.info("WinUSB install (elevated): %s %s", exe.name, args_str)
    return _PendingInstall(logpath=logpath, run=_launch_elevated(str(batpath), ""))


def _finish_winusb(pending: _PendingInstall) -> InstallResult:
    """Waits for install, then reads wdi's exit code & log."""
    if pending.error is not None:
        return pending.error
    run = _wait_elevated(pending.run) if pending.run.launched else pending.run
    output = _read_text(pending.logpath)
    if output:
        logger.info("wdi-simple output:\n%s", output)

    if not run.launched:
        if run.win_error == _ERROR_CANCELLED:
            logger.info("WinUSB install: user declined the UAC prompt")
            return InstallResult(
                ok=False, cancelled=True,
                message="Elevation cancelled. WinUSB was not installed.")
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
    """The oemNN.inf of the WinUSB/libusb driver bound to ``vid:pid``, or ``None``."""
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


def restore_driver(vid: int, pid: int) -> UninstallResult:
    """Remove the WinUSB/libusb binding on ``vid:pid`` so the native driver reclaims it."""
    if sys.platform != "win32":
        raise RuntimeError("restore_driver is Windows-only")

    inf = _find_winusb_inf(vid, pid)
    if inf is None:
        return UninstallResult(
            ok=False,
            message="Couldn't find a WinUSB/libusb driver bound to this card to remove.")

    comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
    params = _restore_command(inf)
    logger.info("Restore driver (elevated): %s %s", comspec, params)

    run = _run_elevated(comspec, params)
    if not run.launched:
        if run.win_error == _ERROR_CANCELLED:
            logger.info("Restore: user declined the UAC prompt")
            return UninstallResult(
                ok=False, cancelled=True,
                message="Elevation cancelled. The WinUSB driver was not removed.")
        logger.warning("Restore: ShellExecuteExW failed (WinError %d)", run.win_error)
        return UninstallResult(
            ok=False, message=f"Could not launch the uninstaller (WinError {run.win_error}).")
    if run.exit_code is None:
        return UninstallResult(ok=False, detail=inf,
                             message="The driver uninstall didn't finish in time.")

    code = run.exit_code
    if code in _PNPUTIL_OK:
        msg = "Removed the WinUSB driver. The card should return to normal Wi-Fi."
        if code == 3010:
            msg += " (A reboot may be needed to finish.)"
        logger.info("Restore: removed %s (pnputil exit=%d)", inf, code)
        return UninstallResult(ok=True, message=msg, detail=inf)
    logger.warning("Restore: pnputil failed for %s (exit=%d)", inf, code)
    return UninstallResult(
        ok=False, detail=inf, message=f"pnputil couldn't remove the driver (exit {code}).")


class SetupWindows(Setup):
    """Windows device setup: bind WinUSB via bundled wdi-simple.exe."""

    def requires_setup(self, device_id: DeviceID) -> bool:
        """True when the device doesn't have WinUSB."""
        if find_device(device_id) is None:
            return False
        return _find_winusb_inf(device_id.vid, device_id.pid) is None

    async def install(self, device_id: DeviceID, ui: Prompter) -> DeviceID | None:
        from wifit3.ui.screens.confirm_install import ConfirmInstallDialog
        if not await ui.ask(ConfirmInstallDialog(device_id.description, chipset=device_id.chipset)):
            return None

        from wifit3.ui.wiffy import INSTALL_LINES
        ui.status(f"Installing WinUSB driver for {device_id.description}… (up to a minute)")
        tail = asyncio.create_task(self._tail_log(ui))
        try:
            pending = await asyncio.to_thread(
                _launch_winusb, device_id.vid, device_id.pid, name=device_id.description)
            if pending.launched:
                ui.begin_assistant(*INSTALL_LINES)   # UAC dismissed
            result = await asyncio.to_thread(_finish_winusb, pending)
        finally:
            tail.cancel()
            try:
                await tail
            except asyncio.CancelledError:
                pass
        await ui.end_assistant(result.ok)   # slide the assistant out

        if not result.ok:
            if not result.cancelled:
                bits = []
                if result.wdi_code is not None:
                    bits.append(f"libwdi code {result.wdi_code}")
                if result.detail:
                    bits.append(result.detail)
                detail = " · ".join(bits)
                ui.error("WinUSB install failed",
                         f"{result.message} ({detail})" if detail else result.message)
            return None
        # WinUSB may re-enumerate the device to a new USB address.
        return await asyncio.to_thread(find_device, device_id) or device_id

    async def uninstall(self, device_id: DeviceID, ui: Prompter) -> SetupResult:
        from wifit3.ui.screens.confirm_uninstall import ConfirmUninstallDialog
        name = device_id.chipset
        if await ui.ask(ConfirmUninstallDialog(name, "win")) is None:
            return SetupResult(ok=False, cancelled=True, message="Uninstall cancelled.")
        from wifit3.ui.wiffy import UNINSTALL_LINES
        ui.status(f"Removing wifit3 driver for {device_id.description}…")
        # pnputil is quick and streams nothing, so give WiFFy a short intro so he still gets a line in.
        ui.begin_assistant(*UNINSTALL_LINES, intro_delay=0.5)
        result = await asyncio.to_thread(restore_driver, device_id.vid, device_id.pid)
        await ui.end_assistant(result.ok)
        return SetupResult(ok=result.ok, message=result.message, cancelled=result.cancelled,
                           detail=result.detail)

    async def _tail_log(self, ui: Prompter) -> None:
        """Invokes ``ui.status(line)`` when the last line of the log changes."""
        path = winusb_log_path()
        last = None
        while True:
            await asyncio.sleep(0.5)
            line = _last_line(_read_text(path))
            if line and line != last:
                last = line
                ui.status(_LIBWDI_DEBUG_LOG_PREFIX.sub("", line))
