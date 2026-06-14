@echo off
REM Build the distributable single-file wifit3.exe (PyInstaller onefile -> dist\wifit3.exe).
REM See wifit3.spec for what's bundled and how to revert to a onedir build.
uv run pyinstaller wifit3.spec --noconfirm
