"""WINDOWS ONLY, and never imported: run as ``python -m wifit3.vault.tools.hashcat_console``.
``launch`` starts hashcat holding this process's console input buffer; ``keypress`` writes into it.
"""
import ctypes
import ctypes.wintypes as wintypes
import msvcrt
import os
import subprocess
import sys

GENERIC_READ_WRITE = 0xC0000000
FILE_SHARE_READ_WRITE = 0x3
OPEN_EXISTING = 3
KEY_EVENT = 0x0001

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.WriteConsoleInputW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]


class CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", CHAR_UNION), ("dwControlKeyState", wintypes.DWORD)]


class EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("pad", ctypes.c_byte * 20)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT_UNION)]


def open_console_input() -> int:
    """The HANDLE of the console input buffer this process is attached to."""
    handle = kernel32.CreateFileW("CONIN$", GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE,
                                  None, OPEN_EXISTING, 0, None)
    if handle is None:
        raise OSError(ctypes.get_last_error(), "CreateFileW(CONIN$)")
    return handle


def launch(cwd: str, log_path: str, command: list[str]) -> int:
    """Start ``command`` with this process's console input buffer as its stdin and its output
    appended to ``log_path``. Returns the pid."""
    stdin = msvcrt.open_osfhandle(open_console_input(), os.O_RDONLY)
    with open(log_path, "ab") as log:
        return subprocess.Popen(command, cwd=cwd, stdin=stdin, stdout=log,
                                stderr=subprocess.STDOUT).pid


def keypress(pid: int, key: str) -> None:
    """Write ``key`` as a key-down then key-up record into the console input buffer of ``pid``."""
    # AttachConsole requires a process with no console, and this one is detachable precisely
    # because wifit3's console is the terminal it is drawing the TUI on.
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        raise OSError(ctypes.get_last_error(), f"AttachConsole({pid})")
    records = (INPUT_RECORD * 2)()
    for index, key_down in ((0, 1), (1, 0)):
        records[index].EventType = KEY_EVENT
        event = records[index].Event.KeyEvent
        event.bKeyDown = key_down
        event.wRepeatCount = 1
        event.wVirtualKeyCode = ord(key.upper())
        event.uChar.UnicodeChar = key
    written = wintypes.DWORD()
    if not kernel32.WriteConsoleInputW(open_console_input(), ctypes.byref(records), 2,
                                       ctypes.byref(written)):
        raise OSError(ctypes.get_last_error(), "WriteConsoleInputW")


def main(argv: list[str]) -> int:
    if len(argv) > 3 and argv[0] == "launch":
        print(launch(argv[1], argv[2], argv[3:]))
        return 0
    if len(argv) == 3 and argv[0] == "keypress":
        keypress(int(argv[1]), argv[2])
        return 0
    print("usage: launch <cwd> <log_path> <command...> | keypress <pid> <key>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
