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

from wifit3.models import DeviceID
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
_SPDRP_COMPATIBLEIDS = 0x00000002
_SPDRP_SERVICE = 0x00000004
_DICS_FLAG_GLOBAL = 0x00000001
_DIREG_DRV = 0x00000002
_KEY_READ = 0x00020019
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_SUCCESS = 0

_LIBUSB_SERVICES = frozenset({"winusb", "libusbk", "libusb0"})
_PNPUTIL_OK = frozenset({0, 3010})  # Exit codes, 3010=ERROR_SUCCESS_REBOOT_REQUIRED
_CM_PROB_NEED_RESTART = 14          # Windows deferred the rebind to a reboot; not a failure

_LIBWDI_DEBUG_LOG_PREFIX = re.compile(r"^libwdi:debug\s*")

# A composite device's children carry &MI_xx in their hardware id and the function's USB class in a
# compatible id; usbccgp publishes both even when the child has no driver bound.
_MI = re.compile(r"&MI_([0-9A-Fa-f]{2})")
_CLASS_FF = re.compile(r"Class_FF", re.IGNORECASE)

# Published driver packages. libwdi hardcodes this provider; the DeviceName is whatever we passed
# as --name, which is how our own packages are told apart from Zadig's.
_INF_DIR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "INF"
_INF_PROVIDER = re.compile(r'^\s*Provider\s*=\s*"?([^"\r\n]*)"?', re.IGNORECASE | re.MULTILINE)
_INF_DEVICE_ID = re.compile(r'^\s*DeviceID\s*=\s*"([^"]*)"', re.IGNORECASE | re.MULTILINE)
_INF_DEVICE_NAME = re.compile(r'^\s*DeviceName\s*=\s*"([^"]*)"', re.IGNORECASE | re.MULTILINE)

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
class _Node:
    """One present USB devnode matching a vid:pid."""
    instance_id: str
    hwid: str
    compat_ids: tuple[str, ...]
    service: str
    inf: str | None
    problem: int   # CM_PROB_* from CM_Get_DevNode_Status; 0 when the device started cleanly

    @property
    def mi(self) -> int | None:
        """The composite interface number, or None for a whole-device (non-composite) node."""
        found = _MI.search(self.hwid)
        return int(found.group(1), 16) if found else None

    @property
    def is_libusb(self) -> bool:
        return self.service.lower() in _LIBUSB_SERVICES

    @property
    def is_wifi_function(self) -> bool:
        """True for the vendor-specific (class 0xFF) function of a composite device."""
        return self.mi is not None and any(_CLASS_FF.search(c) for c in self.compat_ids)


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


def _restore_script(infs: list[str], log: str | None = None) -> str:
    """Batch that deletes each package then re-scans, exiting with the first delete's code.

    A one-line ``cmd /c`` can't do this: ``&&`` skips the rescan when a delete returns 3010
    (reboot required, a *success* code) and ``&`` discards it. ``/force`` is ignored with
    ``/uninstall``, so it is omitted. Redirects stay per-line because a parenthesised block
    would expand every ``%errorlevel%`` at parse time.
    """
    out = f' >> "{log}" 2>&1' if log else ""
    lines = ["@echo off", "set RC=0"]
    if log:
        lines.append(f'if exist "{log}" del "{log}"')
    for inf in infs:
        lines.append(f'pnputil /delete-driver "{inf}" /uninstall{out}')
        lines.append('if not "%errorlevel%"=="0" if "%RC%"=="0" set RC=%errorlevel%')
    lines += [f"pnputil /scan-devices{out}", "exit /b %RC%"]
    return "\r\n".join(lines) + "\r\n"


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
    error: SetupResult | None = None

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
                               error=SetupResult(ok=False, message=f"Couldn't stage the installer: {e}"))
    logger.info("WinUSB install (elevated): %s %s", exe.name, args_str)
    return _PendingInstall(logpath=logpath, run=_launch_elevated(str(batpath), ""))


def _finish_winusb(pending: _PendingInstall) -> SetupResult:
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
            return SetupResult(
                ok=False, cancelled=True,
                message="Elevation cancelled. WinUSB was not installed.")
        logger.warning("WinUSB install: ShellExecuteExW failed (WinError %d)", run.win_error)
        return SetupResult(
            ok=False, message=f"Could not launch the installer (WinError {run.win_error}).")
    if run.exit_code is None:
        return SetupResult(ok=False, detail=_last_line(output),
                             message="The driver installer didn't finish within 3 minutes.")

    wdi = run.exit_code
    logger.info("WinUSB install: wdi-simple exit=%d (%s)", wdi, _wdi_message(wdi))
    return SetupResult(ok=(wdi == 0), wdi_code=wdi, message=_wdi_message(wdi),
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


def _reg_multi_prop(setupapi, hdev, data: _SP_DEVINFO_DATA, prop: int) -> tuple[str, ...]:
    """Every string of a REG_MULTI_SZ device property (:func:`_reg_prop` returns only the first)."""
    buf = ctypes.create_unicode_buffer(2048)
    size = ctypes.c_ulong(0)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        hdev, ctypes.byref(data), prop, None,
        ctypes.cast(buf, ctypes.c_void_p), ctypes.sizeof(buf), ctypes.byref(size))
    if not ok:
        return ()
    return tuple(s for s in buf[:size.value // 2].split("\0") if s)


def _enum_usb_nodes(vid: int, pid: int) -> list[_Node]:
    """Every present USB devnode whose hardware id carries ``vid:pid``, in SetupAPI order.

    Order is undocumented and unstable, so callers must filter rather than take the first hit.
    """
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
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
    setupapi.SetupDiGetDeviceInstanceIdW.restype = ctypes.c_bool
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong)]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    cfgmgr32.CM_Get_DevNode_Status.restype = ctypes.c_ulong
    cfgmgr32.CM_Get_DevNode_Status.argtypes = [
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong, ctypes.c_ulong]
    advapi32.RegQueryValueExW.restype = ctypes.c_long
    advapi32.RegQueryValueExW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    advapi32.RegCloseKey.argtypes = [ctypes.c_void_p]

    hdev = setupapi.SetupDiGetClassDevsW(None, "USB", None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES)
    if not hdev or hdev == _INVALID_HANDLE_VALUE:
        return []
    needle = f"VID_{vid:04X}&PID_{pid:04X}"
    nodes: list[_Node] = []
    try:
        data = _SP_DEVINFO_DATA()
        data.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        i = 0
        while setupapi.SetupDiEnumDeviceInfo(hdev, i, ctypes.byref(data)):
            i += 1
            hwid = _reg_prop(setupapi, hdev, data, _SPDRP_HARDWAREID)
            if not hwid or needle not in hwid.upper():
                continue
            buf = ctypes.create_unicode_buffer(512)
            setupapi.SetupDiGetDeviceInstanceIdW(
                hdev, ctypes.byref(data), buf, ctypes.sizeof(buf) // 2, None)
            status, problem = ctypes.c_ulong(0), ctypes.c_ulong(0)
            cfgmgr32.CM_Get_DevNode_Status(
                ctypes.byref(status), ctypes.byref(problem), data.DevInst, 0)
            nodes.append(_Node(
                instance_id=buf.value,
                hwid=hwid,
                compat_ids=_reg_multi_prop(setupapi, hdev, data, _SPDRP_COMPATIBLEIDS),
                service=_reg_prop(setupapi, hdev, data, _SPDRP_SERVICE) or "",
                inf=_read_inf_path(setupapi, advapi32, hdev, data),
                problem=problem.value))
        return nodes
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)


def _find_winusb_inf(vid: int, pid: int) -> str | None:
    """The oemNN.inf of the WinUSB/libusb driver bound to ``vid:pid``, or ``None``."""
    bound = [n for n in _enum_usb_nodes(vid, pid) if n.is_libusb]
    if not bound:
        logger.info("Restore: no present VID_%04X&PID_%04X node is on a libusb driver", vid, pid)
        return None
    # An &MI_xx node is a correctly targeted binding; a bare one is a hijacked composite parent.
    bound.sort(key=lambda n: n.mi is None)
    logger.info("Restore: %s bound to %s via service %s",
                bound[0].instance_id, bound[0].inf, bound[0].service)
    return bound[0].inf


def _composite_iid(vid: int, pid: int) -> int | None:
    """The interface number to bind on a split composite device, else ``None`` for the whole device.

    Selecting from the live devnodes means we can only ever pass an ``--iid`` that actually exists.
    """
    iid = next((n.mi for n in _enum_usb_nodes(vid, pid) if n.is_wifi_function), None)
    if iid is not None:
        logger.info("Install: %04x:%04x is composite, targeting interface %d", vid, pid, iid)
    return iid


def _ours(text: str, vid: int, pid: int, name: str | None, chipset: str | None) -> bool:
    """True when a published INF is a libwdi package *this app* generated for ``vid:pid``.

    Zadig names its packages after the card's own bus-reported descriptor ("UB93", "802.11ac NIC"),
    so matching our own ``--name`` leaves those alone.
    """
    provider = _INF_PROVIDER.search(text)
    if not provider or provider.group(1).strip().lower() != "libwdi":
        return False
    device_id = _INF_DEVICE_ID.search(text)
    if not device_id or not device_id.group(1).upper().startswith(f"VID_{vid:04X}&PID_{pid:04X}"):
        return False
    device_name = _INF_DEVICE_NAME.search(text)
    if not device_name:
        return False
    # The chipset prefix also catches packages left by older builds, whose product_name differed.
    return bool((name and device_name.group(1) == name)
                or (chipset and device_name.group(1).startswith(f"{chipset} (")))


def _our_packages(vid: int, pid: int, name: str | None, chipset: str | None) -> list[str]:
    """Published ``oemNN.inf`` names of our own libwdi packages claiming ``vid:pid``.

    Reads the INFs rather than parsing pnputil, whose field labels are localised.
    """
    found = []
    try:
        candidates = sorted(_INF_DIR.glob("oem*.inf"))
    except OSError as e:
        logger.warning("Restore: couldn't list %s: %s", _INF_DIR, e)
        return found
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-16", errors="replace")
        except (OSError, UnicodeError):
            continue
        if _ours(text, vid, pid, name, chipset):
            found.append(path.name)
    return found


def restore_driver(vid: int, pid: int, *, name: str | None = None,
                   chipset: str | None = None) -> SetupResult:
    """Remove every WinUSB/libusb binding we made on ``vid:pid`` so the native driver reclaims it."""
    if sys.platform != "win32":
        raise RuntimeError("restore_driver is Windows-only")

    bound = _find_winusb_inf(vid, pid)
    stale = _our_packages(vid, pid, name, chipset)
    infs = list(dict.fromkeys(([bound] if bound else []) + stale))
    if not infs:
        return SetupResult(
            ok=False,
            message="Couldn't find a WinUSB/libusb driver bound to this card to remove.")

    dest = _winusb_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Restore: couldn't create %s: %s", dest, e)
    batpath, logpath = dest / "run-restore.bat", dest / "pnputil.log"
    try:
        batpath.write_text(_restore_script(infs, str(logpath)), encoding="mbcs")
    except OSError as e:
        return SetupResult(ok=False, message=f"Couldn't stage the uninstaller: {e}")
    logger.info("Restore driver (elevated): removing %s", ", ".join(infs))

    run = _run_elevated(str(batpath), "")
    output = _read_text(logpath)
    if output:
        logger.info("pnputil output:\n%s", output)
    if not run.launched:
        if run.win_error == _ERROR_CANCELLED:
            logger.info("Restore: user declined the UAC prompt")
            return SetupResult(
                ok=False, cancelled=True,
                message="Elevation cancelled. The WinUSB driver was not removed.")
        logger.warning("Restore: ShellExecuteExW failed (WinError %d)", run.win_error)
        return SetupResult(
            ok=False, message=f"Could not launch the uninstaller (WinError {run.win_error}).")
    if run.exit_code is None:
        return SetupResult(ok=False, detail=", ".join(infs),
                             message="The driver uninstall didn't finish in time.")
    if run.exit_code not in _PNPUTIL_OK:
        logger.warning("Restore: pnputil failed for %s (exit=%d)", infs, run.exit_code)
        return SetupResult(ok=False, detail=", ".join(infs),
                           message=f"pnputil couldn't remove the driver (exit {run.exit_code}).")
    return _verify_restore(vid, pid, infs, run.exit_code)


def _verify_restore(vid: int, pid: int, infs: list[str], code: int) -> SetupResult:
    """Re-read the bus after a restore: pnputil's exit code says nothing about what got rebound."""
    still = _find_winusb_inf(vid, pid)
    if still is not None:
        logger.warning("Restore: %s still holds %04x:%04x", still, vid, pid)
        return SetupResult(
            ok=False, detail=still,
            message="Another WinUSB driver package is still bound to this card.")
    # Only the nodes a restore can affect: the whole-device/parent node and the Wi-Fi function.
    # A sibling function (Bluetooth) carries its own driver and its own problems, none of ours.
    touched = [n for n in _enum_usb_nodes(vid, pid) if n.mi is None or n.is_wifi_function]
    sick = next((n for n in touched if n.problem and n.problem != _CM_PROB_NEED_RESTART), None)
    if sick is not None:
        logger.warning("Restore: %s has problem %d on %s / %s",
                       sick.instance_id, sick.problem, sick.service, sick.inf)
        return SetupResult(
            ok=False, detail=f"{sick.service or '(none)'} / {sick.inf or '(none)'}",
            message="The card's driver did not come back correctly. Unplug and replug it.")
    msg = "Removed the WinUSB driver. The card should return to normal Wi-Fi."
    if code == 3010 or any(n.problem == _CM_PROB_NEED_RESTART for n in touched):
        msg += " (A reboot may be needed to finish.)"
    logger.info("Restore: removed %s (pnputil exit=%d)", ", ".join(infs), code)
    return SetupResult(ok=True, message=msg, detail=", ".join(infs))


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
            iid = await asyncio.to_thread(_composite_iid, device_id.vid, device_id.pid)
            pending = await asyncio.to_thread(
                _launch_winusb, device_id.vid, device_id.pid, iid=iid,
                name=device_id.description)
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
        result = await asyncio.to_thread(
            restore_driver, device_id.vid, device_id.pid,
            name=device_id.description, chipset=device_id.chipset)
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
