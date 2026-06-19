@echo off
REM Preview the Focus v2 panel border/title colours in a real terminal.
REM (Claude Code's in-terminal renderer shows them monochrome.)
cd /d "%~dp0\..\.."
uv run python scripts/ui/preview_focus_chrome.py
