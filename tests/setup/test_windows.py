"""Unit tests for the pure helpers in wifit3.setup.windows.

The elevated ShellExecuteExW path can't be exercised without a real UAC prompt + driver
rebind, so it's left to manual hardware testing; everything testable in isolation (argv
build, exit-code sign correction, WDI message mapping, bundled-exe resolution) is covered
here.
"""
import platform

import pytest

from wifit3.setup.windows import (
    InstallResult,
    _build_args,
    _signed32,
    _wdi_message,
    wdi_simple_path,
)

_X64 = platform.machine().lower() in ("amd64", "x86_64")


def test_build_args_defaults():
    assert _build_args(0x148F, 0x3070) == [
        "--vid", "0x148f", "--pid", "0x3070",
        "--type", "0", "--iid", "0", "--timeout", "120000",
    ]


def test_build_args_name_and_iid():
    args = _build_args(0x0BDA, 0x8812, iid=2, name="Ralink RT3070 / ALFA AWUS036NH")
    assert args[args.index("--iid") + 1] == "2"
    assert args[args.index("--name") + 1] == "Ralink RT3070 / ALFA AWUS036NH"


def test_build_args_omits_name_when_none():
    assert "--name" not in _build_args(0x0BDA, 0x8812)


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
