# PyInstaller build spec for wifit3 — build with: uv run pyinstaller wifit3.spec
#
# Produces a single self-contained onefile binary — dist/wifit3.exe on Windows, dist/wifit3 on
# Linux/macOS. PyInstaller does NOT cross-compile: build each target on that OS.
#
# onefile (active) vs onedir tradeoff:
#   onefile: one binary; unpacks into a temp dir on each launch (slower cold start), trips AV/SmartScreen more.
#   onedir:  a dist/wifit3/ folder; faster start, friendlier to AV, but ships as the whole folder.
# To switch to onedir, swap the EXE/COLLECT blocks at the bottom of this file.
#
# Console app (console=True): Textual needs a real TTY, so a --windowed build would have no
# stdin/stdout and break. The exe closes-on-double-click like any console program — launch from a terminal.

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# Collects firmware blobs (*.bin, *.fw), ANSI art (*.ans), and Windows installer (wdi-simple.exe)
datas = collect_data_files("wifit3")
if sys.platform != "win32":
    # /setup/bin/* is 100% windows-specific
    datas = [(src, dest) for (src, dest) in datas
             if "/setup/bin/" not in src.replace("\\", "/")]
binaries = []
hiddenimports = []

# libusb_package ships libusb-1.0.dll as a binary (no DLL -> zero USB devices found).
# textual ships its widgets' built-in .tcss as data files. Pull each fully.
for _pkg in ("libusb_package", "textual", "rich"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# Chip drivers are discovered at runtime by a pkgutil walk over wifit3.chips (device/manager.py),
# never statically imported, so PyInstaller's analysis can't see them and a onefile build ships
# with zero drivers: the app launches but finds no interfaces. Force every chip subpackage in.
hiddenimports += collect_submodules("wifit3.chips")

# libusb_package.get_library_path() locates the libusb shared lib via importlib.resources, i.e.
# it must sit at libusb_package/libusb-1.0.* INSIDE the package dir of the bundle. collect_all
# pulls the lib in as a BINARY, but a onefile build flattens binaries to the bundle root on
# extraction — so importlib.resources can't see it under libusb_package/ and pyusb falls back to
# a (frozen-broken) system search → "No backend available". Re-add the lib as a DATA file under
# libusb_package/ so it extracts into the package dir where get_library_path() looks. Windows is
# unaffected (its .dll is already collected as data, which keeps its subdir); the Linux .so is not.
import libusb_package as _libusb_package
_lp_dir = os.path.dirname(_libusb_package.__file__)
for _f in os.listdir(_lp_dir):
    if _f.startswith("libusb-1.0."):
        datas.append((os.path.join(_lp_dir, _f), "libusb_package"))

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
    excludes=["pyshark", "pytest", "ruff", "textual_dev", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# App icon: .ico is a Windows resource; a Linux ELF has no icon slot (PyInstaller ignores icon=
# there), so the icon is Windows-only. Fall back to None if the file is absent so a build never
# fails over a missing icon.
_icon = "assets/wifit3.ico" if sys.platform == "win32" else None
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
    # universal2 (fat Intel+arm64) on macOS; thin elsewhere. Strict arch validation will abort
    # the build if any bundled binary isn't fat — that's the desired loud failure.
    target_arch="universal2" if sys.platform == "darwin" else None,
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
