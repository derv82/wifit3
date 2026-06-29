@echo off
REM Builds the distributable wifit3.exe into dist\.
REM The same command builds the Linux binary to dist/wifit3.
uv run pyinstaller wifit3.spec --noconfirm
