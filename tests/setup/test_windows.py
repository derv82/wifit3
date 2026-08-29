"""Unit tests for the pure helpers in wifit3.setup.windows.

The elevated ShellExecuteExW path can't be exercised without a real UAC prompt + driver
rebind, so it's left to manual hardware testing; everything testable in isolation (argv
build, exit-code sign correction, WDI message mapping, bundled-exe resolution) is covered
here.
"""
import platform

import pytest

from wifit3.setup.windows import (
    _LIBUSB_SERVICES,
    _PNPUTIL_OK,
    InstallResult,
    UninstallResult,
    _build_args,
    _restore_command,
    _signed32,
    _wdi_message,
    wdi_simple_path,
)

_X64 = platform.machine().lower() in ("amd64", "x86_64")


def test_build_args_defaults_omit_iid():
    # No --iid for a simple device: wdi-simple's -i also sets is_composite=TRUE, so passing it
    # targets USB\VID&PID&MI_00 and never binds the real single-interface node (libwdi #206).
    assert _build_args(0x148F, 0x3070) == [
        "--vid", "0x148f", "--pid", "0x3070", "--type", "0", "--timeout", "120000",
    ]
    assert "--iid" not in _build_args(0x148F, 0x3070)


def test_build_args_iid_only_when_composite():
    args = _build_args(0x0BDA, 0x8812, iid=2, name="Composite card")
    assert args[args.index("--iid") + 1] == "2"
    assert args[args.index("--name") + 1] == "Composite card"


def test_build_args_log_level():
    assert _build_args(0x0BDA, 0x8187, log_level=0)[-2:] == ["--log", "0"]
    assert "--log" not in _build_args(0x0BDA, 0x8187)


def test_build_args_omits_name_when_none():
    assert "--name" not in _build_args(0x0BDA, 0x8812)


def test_build_args_dest_appends_absolute_extraction_dir():
    # wdi-simple's default extraction dir is relative ("usb_driver") -> fails from System32
    # when elevated; we always pass an absolute --dest.
    args = _build_args(0x0BDA, 0x8187, dest=r"C:\Temp\wifit3_winusb")
    assert args[args.index("--dest") + 1] == r"C:\Temp\wifit3_winusb"


def test_build_args_omits_dest_when_none():
    assert "--dest" not in _build_args(0x0BDA, 0x8187)


def test_signed32_roundtrips_negative_wdi_codes():
    # wdi-simple returns the negative WDI enum; Windows surfaces it as an unsigned DWORD.
    assert _signed32(0) == 0
    assert _signed32(0xFFFFFFFF) == -1            # WDI_ERROR_IO
    assert _signed32(0xFFFFFFF1) == -15           # WDI_ERROR_NEEDS_ADMIN
    assert _signed32(0xFFFFFF9D) == -99           # WDI_ERROR_OTHER


def test_wdi_message_known_codes():
    assert _wdi_message(0) == "WinUSB installed."
    assert "Administrator" in _wdi_message(-15)
    assert "unplugged" in _wdi_message(-4)


def test_wdi_message_unknown_code_is_descriptive():
    msg = _wdi_message(42)
    assert "42" in msg


def test_install_result_defaults():
    r = InstallResult(ok=True, message="WinUSB installed.")
    assert r.ok and not r.cancelled and r.wdi_code is None


@pytest.mark.skipif(not _X64, reason="only the x64 wdi-simple.exe is bundled")
def test_wdi_simple_path_resolves_to_bundled_exe():
    p = wdi_simple_path()
    assert p.name == "wdi-simple.exe"
    assert p.is_file()
    assert p.parent.name == "win-x64"


# --- restore_driver helpers ------------------------------------------------------------

def test_restore_command_deletes_then_rescans():
    cmd = _restore_command("oem42.inf")
    assert cmd.startswith("/c ")
    assert '/delete-driver "oem42.inf" /uninstall /force' in cmd
    assert cmd.rstrip().endswith("pnputil /scan-devices")
    assert "&&" in cmd  # scan only after a successful delete, so cmd's exit reflects delete


def test_libusb_services_cover_zadig_bindings():
    # WinUSB (ours + Zadig), plus Zadig's libusbK / libusb-win32, so restore rolls those back.
    assert _LIBUSB_SERVICES == {"winusb", "libusbk", "libusb0"}


def test_pnputil_reboot_required_counts_as_success():
    assert 0 in _PNPUTIL_OK
    assert 3010 in _PNPUTIL_OK  # ERROR_SUCCESS_REBOOT_REQUIRED


def test_restore_result_defaults():
    r = UninstallResult(ok=True, message="done")
    assert r.ok and not r.cancelled and r.detail is None
