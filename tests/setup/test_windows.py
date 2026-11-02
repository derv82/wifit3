"""Unit tests for the pure helpers in wifit3.setup.windows.

The elevated ShellExecuteExW path can't be exercised without a real UAC prompt + driver
rebind, so it's left to manual hardware testing; everything testable in isolation (argv
build, exit-code sign correction, WDI message mapping, bundled-exe resolution) is covered
here.
"""
import platform

import pytest

from wifit3.setup.base import SetupResult
from wifit3.setup.windows import (
    _LIBUSB_SERVICES,
    _PNPUTIL_OK,
    _Node,
    _build_args,
    _ours,
    _restore_script,
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
    r = SetupResult(ok=True, message="WinUSB installed.")
    assert r.ok and not r.cancelled and r.wdi_code is None


@pytest.mark.skipif(not _X64, reason="only the x64 wdi-simple.exe is bundled")
def test_wdi_simple_path_resolves_to_bundled_exe():
    p = wdi_simple_path()
    assert p.name == "wdi-simple.exe"
    assert p.is_file()
    assert p.parent.name == "win-x64"


# --- restore_driver helpers ------------------------------------------------------------

def test_restore_script_deletes_each_package_then_rescans():
    lines = _restore_script(["oem42.inf", "oem43.inf"]).splitlines()
    assert lines.count('pnputil /delete-driver "oem42.inf" /uninstall') == 1
    assert lines.count('pnputil /delete-driver "oem43.inf" /uninstall') == 1
    assert lines[-2] == "pnputil /scan-devices"  # one rescan, after every delete


def test_restore_script_drops_force_which_pnputil_ignores_with_uninstall():
    assert "/force" not in _restore_script(["oem42.inf"])


def test_restore_script_propagates_the_first_delete_exit_code():
    # 3010 (reboot required) is a *success* code: "&&" would skip the rescan and "&" would
    # discard the code, so the batch latches it in RC and exits with it.
    script = _restore_script(["oem42.inf"])
    assert 'if not "%errorlevel%"=="0" if "%RC%"=="0" set RC=%errorlevel%' in script
    assert script.rstrip().endswith("exit /b %RC%")
    assert "(" not in script  # a parenthesised block expands %errorlevel% at parse time


def test_restore_script_redirects_per_line_when_logging():
    script = _restore_script(["oem42.inf"], r"C:\Temp\pnputil.log")
    assert script.count(r'>> "C:\Temp\pnputil.log" 2>&1') == 2  # the delete and the rescan
    assert r'if exist "C:\Temp\pnputil.log" del' in script


# --- package ownership -----------------------------------------------------------------

def _inf(provider: str, device_id: str, device_name: str) -> str:
    return (f'[Strings]\r\nDeviceName = "{device_name}"\r\nDeviceID   = "{device_id}"\r\n'
            f'\r\n[Version]\r\nProvider         = "{provider}"\r\n')


def test_ours_matches_the_name_we_passed_as_wdi_name():
    text = _inf("libwdi", "VID_0E8D&PID_7961", "MT7921AU (AWUS036AXML / Panda PAU0F)")
    assert _ours(text, 0x0E8D, 0x7961, "MT7921AU (AWUS036AXML / Panda PAU0F)", "MT7921AU")


def test_ours_matches_chipset_prefix_from_an_older_build():
    # An older wifit3 passed a different product_name; the chipset prefix still claims it.
    text = _inf("libwdi", "VID_0E8D&PID_7961", "MT7921AU (some older product name)")
    assert _ours(text, 0x0E8D, 0x7961, "MT7921AU (AWUS036AXML / Panda PAU0F)", "MT7921AU")


def test_ours_rejects_a_zadig_package_for_the_same_card():
    # Zadig names the package after the card's bus-reported descriptor, never after our chipset.
    text = _inf("libwdi", "VID_0CF3&PID_9271", "UB93")
    assert not _ours(text, 0x0CF3, 0x9271, "AR9271 (AWUS036NHA / TL-WN722N v1)", "AR9271")


def test_ours_rejects_a_composite_interface_package_from_zadig():
    text = _inf("libwdi", "VID_0BDA&PID_C820&MI_02", "802.11ac NIC (Interface 2)")
    assert not _ours(text, 0x0BDA, 0xC820, "RTL8821CU (Auscoumer 600)", "RTL8821CU")


def test_ours_rejects_a_vendor_package():
    text = _inf("MediaTek, Inc.", "VID_0E8D&PID_7961", "MT7921AU (AWUS036AXML / Panda PAU0F)")
    assert not _ours(text, 0x0E8D, 0x7961, "MT7921AU (AWUS036AXML / Panda PAU0F)", "MT7921AU")


def test_ours_rejects_another_cards_package():
    text = _inf("libwdi", "VID_0BDA&PID_8812", "RTL8812AU (AWUS036ACH)")
    assert not _ours(text, 0x0E8D, 0x7961, "MT7921AU (AWUS036AXML / Panda PAU0F)", "MT7921AU")


# --- devnode classification ------------------------------------------------------------

def _node(hwid: str, service: str = "", compat: tuple[str, ...] = (), problem: int = 0) -> _Node:
    return _Node(instance_id=hwid, hwid=hwid, compat_ids=compat, service=service,
                 inf=None, problem=problem)


def test_node_reads_the_composite_interface_number():
    assert _node(r"USB\VID_0E8D&PID_7961&REV_0100&MI_03").mi == 3
    assert _node(r"USB\VID_0E8D&PID_7961&REV_0100").mi is None


def test_node_spots_the_vendor_specific_function():
    wifi = _node(r"USB\VID_0E8D&PID_7961&REV_0100&MI_03",
                 compat=(r"USB\COMPAT_VID_0e8d&Class_ff&SubClass_ff&Prot_ff",))
    bt = _node(r"USB\VID_0E8D&PID_7961&REV_0100&MI_00",
               compat=(r"USB\COMPAT_VID_0e8d&Class_e0&SubClass_01&Prot_01",))
    assert wifi.is_wifi_function and not bt.is_wifi_function


def test_node_does_not_call_a_whole_device_a_composite_function():
    # A single-interface card must never get --iid; it has no MI_ node to target.
    bare = _node(r"USB\VID_0E8D&PID_7961&REV_0100",
                 compat=(r"USB\COMPAT_VID_0e8d&Class_ff&SubClass_ff&Prot_ff",))
    assert not bare.is_wifi_function


def test_node_recognises_every_libusb_flavour():
    assert _node("x", service="WinUSB").is_libusb      # the service name is cased by Windows
    assert _node("x", service="libusbK").is_libusb
    assert not _node("x", service="athur").is_libusb


def test_libusb_services_cover_zadig_bindings():
    # WinUSB (ours + Zadig), plus Zadig's libusbK / libusb-win32, so restore rolls those back.
    assert _LIBUSB_SERVICES == {"winusb", "libusbk", "libusb0"}


def test_pnputil_reboot_required_counts_as_success():
    assert 0 in _PNPUTIL_OK
    assert 3010 in _PNPUTIL_OK  # ERROR_SUCCESS_REBOOT_REQUIRED


def test_restore_result_defaults():
    r = SetupResult(ok=True, message="done")
    assert r.ok and not r.cancelled and r.detail is None
