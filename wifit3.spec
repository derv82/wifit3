# PyInstaller build spec for wifit3 — build with: uv run pyinstaller wifit3.spec
#
# Produces a single self-contained onefile binary — dist/wifit3.exe on Windows, dist/wifit3 on
# Linux (drag-and-drop distributable). PyInstaller does NOT cross-compile: build each target ON
# that OS (a Linux box/container for the Linux binary). macOS is not a build target.
# Tradeoff vs onedir: onefile unpacks the ~40 MB bundle into a temp dir on each launch (a
# slightly slower cold start) and trips AV/SmartScreen more readily. Multiprocessing is
# unaffected — the WEP cracker's spawned workers reuse the parent's already-extracted dir
# (freeze_support in __main__.py), so they do not re-unpack per worker. To revert to a onedir
# bundle (a dist/wifit3/ folder — faster start, friendlier to AV, but the whole folder must
# ship together), swap the EXE/COLLECT blocks at the bottom of this file.
#
# This is a CONSOLE app (console=True): Textual needs a real TTY, so a --windowed build
# would have no stdin/stdout and break. The exe therefore closes-on-double-click like any
# console program — it is meant to be launched from a terminal.
#
# No UAC manifest (uac_admin=False): the app self-elevates only the bundled wdi-simple.exe
# child via ShellExecuteExW "runas" (setup/windows.py); elevating the whole TUI would be wrong.

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Assets loaded via Path(__file__).parent / "assets" / ... are invisible to import analysis:
# firmware blobs (chips/*/assets/*.bin,*.fw), ANSI art (ui/assets/*.ans), and the vendored
# WinUSB installer (setup/bin/win-x64/wdi-simple.exe). collect_data_files grabs them all,
# preserving the package-relative layout that Path(__file__) resolves against in the bundle.
datas = collect_data_files("wifit3")
if sys.platform != "win32":
    # wdi-simple.exe (and its PROVENANCE) is the Windows WinUSB binder — setup/windows.py only
    # ever invokes it under sys.platform == "win32". Drop the whole vendored setup/bin/ tree
    # from the Linux bundle rather than ship a dead Windows .exe inside it.
    datas = [(src, dest) for (src, dest) in datas
             if "/setup/bin/" not in src.replace("\\", "/")]
binaries = []
# Drivers are statically imported in wlan/manager.py, but collect every chips.* submodule
# anyway so a future dynamic/lazy driver load can't silently drop one from the bundle.
hiddenimports = collect_submodules("wifit3.chips")

# libusb_package ships libusb-1.0.dll as a binary (no DLL -> zero USB devices found).
# textual ships its widgets' built-in .tcss as data files. Pull each fully.
for _pkg in ("libusb_package", "textual", "rich", "pydantic"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ["src/wifit3/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # pyshark shells out to a separate Wireshark/tshark install (can't be bundled) and is
    # dev/RE-only; the rest are dev tooling that has no place in a distributed build.
    excludes=["pyshark", "pytest", "ruff", "textual_dev"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# App icon: .ico is a Windows resource; a Linux ELF has no icon slot (PyInstaller ignores icon=
# there), so the icon is Windows-only. Fall back to None if the file is absent so a build never
# fails over a missing icon.
_icon = "packaging/wifit3.ico" if sys.platform == "win32" else None
if _icon and not os.path.isfile(_icon):
    _icon = None

# ---- onefile build (ACTIVE): one self-contained binary (dist/wifit3.exe on Windows) ----
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="wifit3",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=_icon,
    runtime_tmpdir=None,
)

# ---- onedir build (revert option): dist/wifit3/wifit3.exe + a sibling dist/wifit3/_internal/ ----
# To switch back: comment out the EXE(...) above and uncomment both blocks below. A onedir
# build must be distributed as the WHOLE dist/wifit3/ folder (zip it) — the .exe alone won't run.
# exe = EXE(
#     pyz,
#     a.scripts,
#     [],
#     exclude_binaries=True,
#     name="wifit3",
#     debug=False,
#     strip=False,
#     upx=False,
#     console=True,
#     icon=_icon,
# )
# coll = COLLECT(
#     exe,
#     a.binaries,
#     a.datas,
#     strip=False,
#     upx=False,
#     name="wifit3",
# )
