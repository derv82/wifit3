import ctypes
import shutil
import subprocess
import sys
import time


def copy_copyq_text(text: str) -> bool:
    """Add UTF-8 text to CopyQ history when its command-line client is available."""
    executable = shutil.which("copyq")
    if executable is None:
        return False
    try:
        subprocess.run(
            [executable, "add", "-"],
            input=text.encode("utf-8"),
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def copy_macos_text(text: str) -> bool:
    """Copy UTF-8 text with the clipboard tool included in macOS."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["/usr/bin/pbcopy"],
            input=text.encode("utf-8"),
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def copy_windows_text(text: str) -> bool:
    """Copy Unicode text through Win32, returning whether it succeeded."""
    if sys.platform != "win32":
        return False

    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE

    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return False

    handle = None
    transferred = False
    try:
        if not user32.EmptyClipboard():
            return False
        encoded = (text + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(0x0002, len(encoded))
        if not handle:
            return False
        address = kernel32.GlobalLock(handle)
        if not address:
            return False
        try:
            ctypes.memmove(address, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):
            return False
        transferred = True
        return True
    finally:
        if handle and not transferred:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()
