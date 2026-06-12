"""Linux kernel-driver access via a blanket udev permission rule. [DEVICE-SETUP.md Tier 1]

The Linux analog of :mod:`wifit3.setup.windows`. Where Windows must *bind* the card to WinUSB
so libusb can open it, Linux only needs the user to have write-access to the card's usbfs node
(``/dev/bus/usb/BBB/DDD``): with that, a non-root process can both open the device AND detach
its kernel driver — ``USBDEVFS_DISCONNECT`` keys off node write-access, not ``CAP_SYS_ADMIN``
(measured 2026-06-09; DEVICE-SETUP.md L1).

A udev permission rule is **permission-only**: it sets the node's owner/ACL and changes nothing
else — the kernel driver still binds, the card is still a normal Wi-Fi adapter, until wifit3
detaches it at *runtime* (per-session; replug undoes). So one **blanket** rule covering every
supported VID:PID lets the user hot-plug *any* supported card without sudo, while touching none
of them — which is why it's the default (one ``pkexec`` ever, not one per card).

- :func:`install_rule` writes the blanket rule (all supported cards) under one ``pkexec`` prompt.
- :func:`remove_rule` deletes it — the uninstall (✕) button's Linux side (revokes ALL cards).
- :func:`emit_udev_text` returns the same blanket text for the ``--emit-udev`` power-user CLI.

The privileged step blocks on a graphical ``pkexec`` dialog, so callers run it OFF the Textual
event loop (the splash uses ``asyncio.to_thread``), exactly like :func:`install_winusb`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

RULE_DIR = "/etc/udev/rules.d"
# One shared rule for every supported VID:PID. The 60- prefix sorts BEFORE systemd's
# 70-uaccess.rules: the uaccess builtin only grants the seat ACL for devices already TAG-ed
# "uaccess" when it runs, so our TAG+="uaccess" must be set by an earlier-sorting file or the
# ACL is never applied.
RULE_PATH = f"{RULE_DIR}/60-wifit3.rules"

# Per-card perms clause applied to the usbfs node. The shipping default is "all" = uaccess
# (logind grants a dynamic seat ACL — tightest scope on systemd desktops, revoked on logout)
# PLUS plugdev/0660 (works on pre-systemd / headless boxes): belt-and-suspenders so the node
# is writable on whatever the user runs. The probe's "loose" (0666 world-RW) lever is omitted
# here — it exists only to isolate a measurement and must never ship.
_PERMS_CLAUSE = {
    "uaccess": 'TAG+="uaccess"',
    "plugdev": 'MODE="0660", GROUP="plugdev"',
    "all": 'TAG+="uaccess", MODE="0660", GROUP="plugdev"',
}
_DEFAULT_PERMS = "all"

_REMOVED_MSG = ("Removed the device-access rule. Replug a card to restore its normal "
                "Wi-Fi driver.")


@dataclass(frozen=True)
class LinuxSetupResult:
    """Outcome of a privileged Linux setup action (rule install / removal).

    Mirrors :class:`windows.InstallResult` / :class:`windows.RestoreResult` so the splash
    treats both OSes uniformly: ``ok`` drives the happy path, ``cancelled`` flags the benign
    "pkexec dialog declined" case (a softer message than a failure), ``detail`` carries the
    rule path or a copy-paste manual fallback for the error modal + logs.
    """
    ok: bool
    message: str
    cancelled: bool = False
    detail: str | None = None


def build_rule_text(ids: list[tuple[int, int, str]], perms: str = _DEFAULT_PERMS,
                    *, blanket: bool = False) -> str:
    """A udev rules file granting the local user RW on each VID:PID's usbfs node.

    One deduped rule per VID:PID, matching the usb_device (``ATTR{idVendor}``/``idProduct``,
    lowercase hex, no ``0x``) — the canonical "let libusb open + detach this device" shape.

    Each card's description goes on its OWN comment line ABOVE its rule, never as a trailing
    ``# ...`` on the rule line: modern udev only accepts ``#`` at the start of a line and
    rejects the WHOLE rule ("a comma between tokens is expected") if a comment trails it. (This
    bit us once; ``tests/setup/test_linux.py`` asserts no rule line carries an inline ``#``.)
    """
    clause = _PERMS_CLAUSE[perms]
    scope = "all supported cards" if blanket else "one activated card"
    lines = [
        "# wifit3 Linux device-access rule (generated from the driver registry).",
        f"# Scope: {scope}. Perms: {perms} -> {clause}",
        "# Grants the local user RW on the card's usbfs node so wifit3 can open it AND detach",
        "# the kernel driver without root. Permission only — it never unbinds the driver or",
        "# changes how the card works as normal Wi-Fi; wifit3 detaches at runtime (replug undoes).",
        "# Remove it to revoke (the uninstall button, or: rm the file + udevadm control --reload-rules).",
        "",
    ]
    seen: set[tuple[int, int]] = set()
    for vid, pid, desc in ids:
        if (vid, pid) in seen:
            continue
        seen.add((vid, pid))
        lines.append(f"# {desc}")
        lines.append(
            f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{vid:04x}", '
            f'ATTR{{idProduct}}=="{pid:04x}", {clause}')
    return "\n".join(lines) + "\n"


def emit_udev_text(perms: str = _DEFAULT_PERMS) -> str:
    """The blanket all-supported-cards udev file, sourced from the live driver registry.

    The single source of truth for what :func:`install_rule` writes and what ``wifit3
    --emit-udev`` prints, so the installed rule never drifts from the supported-hardware list.
    """
    from wifit3.setup import ids_from_registry

    ids = [(e.vid, e.pid, e.description) for e in ids_from_registry()]
    return build_rule_text(ids, perms, blanket=True)


def _choose_method() -> str | None:
    """The privileged-exec method to use: ``pkexec`` (graphical) preferred, ``sudo`` fallback,
    or ``None`` when neither is present (caller surfaces the manual copy-paste path)."""
    if shutil.which("pkexec"):
        return "pkexec"
    if shutil.which("sudo"):
        return "sudo"
    return None


def run_privileged(shell_cmd: str, method: str) -> int:
    """Run ``shell_cmd`` (a ``/bin/sh -c`` string) as root via ``method``; return the exit code.

    ``pkexec`` pops a graphical polkit password dialog (the Linux UAC analog); ``sudo`` is the
    headless fallback. Returns 127 when the runner isn't found. pkexec's 126 (dismissed / not
    authorized) is surfaced by callers as the cancelled-vs-failed split.
    """
    sh = shutil.which("sh") or "/bin/sh"
    runner = shutil.which(method)
    if not runner:
        logger.warning("Linux setup: %s not found", method)
        return 127
    argv = [runner, sh, "-c", shell_cmd]
    logger.info("Linux setup: elevating via %s: sh -c %r", Path(runner).name, shell_cmd)
    try:
        return subprocess.call(argv)
    except KeyboardInterrupt:
        return 130


def _install_shell(tmp: str) -> str:
    """Privileged one-liner: drop the staged rule into place and re-apply it to live devices.

    ``udevadm trigger --action=add`` re-fires the rule (and the logind uaccess ACL) against the
    already-plugged card, so the node usually becomes writable with no replug. Firmware-
    reenumerating combos (MT7921AU) can still need a replug — the caller surfaces that.
    """
    return (f"install -m 0644 {tmp} {RULE_PATH} && "
            "udevadm control --reload-rules && "
            "udevadm trigger --action=add --subsystem-match=usb")


def _remove_shell() -> str:
    return f"rm -f {RULE_PATH} && udevadm control --reload-rules"


def _manual_hint() -> str:
    """The copy-paste fallback when no graphical elevator is available (headless boxes)."""
    return (f"No pkexec/sudo found. Either run `sudo wifit3`, or install manually: "
            f"sudo cp <rule> {RULE_PATH} && sudo udevadm control --reload-rules && "
            "sudo udevadm trigger --action=add --subsystem-match=usb, then replug.")


def install_rule(perms: str = _DEFAULT_PERMS) -> LinuxSetupResult:
    """Install the blanket udev permission rule (all supported cards) under one ``pkexec`` prompt.

    Blocks on the graphical password dialog — call OFF the event loop. After this, every
    supported card's usbfs node is user-writable, so ``connect()``'s non-root open + kernel
    detach succeed for any of them. This does NOT unbind any driver (that's the per-session
    runtime detach in the driver's ``_claim()``); cards stay normal Wi-Fi adapters until then,
    and a replug re-attaches. Returns a :class:`LinuxSetupResult`; raises only for a non-Linux
    host (mirrors :func:`install_winusb`'s guard).
    """
    if not sys.platform.startswith("linux"):
        raise RuntimeError("install_rule is Linux-only")

    text = emit_udev_text(perms)

    if os.geteuid() == 0:  # already under `sudo wifit3` — write directly, no second prompt.
        try:
            Path(RULE_PATH).write_text(text)
            os.chmod(RULE_PATH, 0o644)
            subprocess.call(["udevadm", "control", "--reload-rules"])
            subprocess.call(["udevadm", "trigger", "--action=add", "--subsystem-match=usb"])
        except OSError as e:
            return LinuxSetupResult(ok=False, message=f"Couldn't write the access rule: {e}")
        return LinuxSetupResult(ok=True, detail=RULE_PATH,
                                message="Installed the device-access rule.")

    method = _choose_method()
    if method is None:
        return LinuxSetupResult(
            ok=False, detail=_manual_hint(),
            message="No graphical elevator (pkexec/sudo) found to install the access rule.")

    tmp = str(Path(tempfile.gettempdir()) / "wifit3.rules")
    try:
        Path(tmp).write_text(text)
    except OSError as e:
        return LinuxSetupResult(ok=False, message=f"Couldn't stage the access rule: {e}")

    rc = run_privileged(_install_shell(tmp), method)
    if rc == 0:
        return LinuxSetupResult(ok=True, detail=RULE_PATH,
                                message="Installed the device-access rule.")
    if rc == 126:
        return LinuxSetupResult(
            ok=False, cancelled=True,
            message="Authorization dismissed — the access rule was not installed.")
    return LinuxSetupResult(ok=False, detail=_manual_hint(),
                            message=f"Couldn't install the access rule (exit {rc}).")


def remove_rule() -> LinuxSetupResult:
    """Remove the blanket udev rule — the uninstall (✕) button, Linux side.

    One ``pkexec`` prompt: ``rm -f <rule> && udevadm control --reload-rules``. Because the rule
    is a single shared file, this revokes node access for **all** supported cards at once; none
    are re-attached here (we never replaced a driver — the runtime detach is per-session), so
    each returns to normal Wi-Fi on its next replug. A missing rule is a benign no-op
    (``ok=True``). Blocks on the dialog — call off the loop. Raises only for a non-Linux host.
    """
    if not sys.platform.startswith("linux"):
        raise RuntimeError("remove_rule is Linux-only")

    if not Path(RULE_PATH).exists():
        return LinuxSetupResult(
            ok=True, detail=RULE_PATH,
            message="No wifit3 access rule is installed — nothing to remove.")

    if os.geteuid() == 0:
        try:
            Path(RULE_PATH).unlink(missing_ok=True)
            subprocess.call(["udevadm", "control", "--reload-rules"])
        except OSError as e:
            return LinuxSetupResult(ok=False, message=f"Couldn't remove the access rule: {e}")
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=_REMOVED_MSG)

    method = _choose_method()
    if method is None:
        return LinuxSetupResult(
            ok=False,
            detail=f"sudo rm -f {RULE_PATH} && sudo udevadm control --reload-rules",
            message="No graphical elevator (pkexec/sudo) found to remove the access rule.")

    rc = run_privileged(_remove_shell(), method)
    if rc == 0:
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=_REMOVED_MSG)
    if rc == 126:
        return LinuxSetupResult(
            ok=False, cancelled=True,
            message="Authorization dismissed — the access rule was not removed.")
    return LinuxSetupResult(ok=False, detail=RULE_PATH,
                            message=f"Couldn't remove the access rule (exit {rc}).")
