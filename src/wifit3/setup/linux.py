"""Linux kernel-driver access via a blanket udev permission rule.

The Linux analog of :mod:`wifit3.setup.windows`.
"""
from __future__ import annotations

import grp
import logging
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

RULE_DIR = "/etc/udev/rules.d"
# One shared rule file for every supported VID:PID. The 60- prefix sorts after 50-udev-default
# (whose 0664 root:root node perms we override) and ahead of the seat/uaccess files we don't use.
RULE_PATH = f"{RULE_DIR}/60-wifit3.rules"

_REMOVED_MSG = ("Removed the device-access rule. Replug a card to restore its normal "
                "Wi-Fi driver.")


def _access_group() -> str | None:
    # Identifies root or wheel
    mine = set(os.getgroups()) | {os.getgid()}
    for name in ("sudo", "wheel"):
        try:
            if grp.getgrnam(name).gr_gid in mine:
                return name
        except KeyError:
            pass
    return None


def current_user() -> str:
    # Login name of the uid that gets access — the real uid, not env ($LOGNAME can disagree).
    return pwd.getpwuid(os.getuid()).pw_name


@dataclass(frozen=True)
class LinuxSetupResult:
    # Outcome of a privileged Linux setup action (rule install / removal).
    ok: bool
    message: str
    cancelled: bool = False
    detail: str | None = None


def emit_udev_text(group: str | None = None) -> str:
    """The blanket all-supported-cards udev rules"""
    from wifit3.setup import ids_from_registry
    lines = ["# wifit3 Linux device-access rule", ""]
    seen: set[tuple[int, int]] = set()
    for e in ids_from_registry():
        if (e.vid, e.pid) in seen:
            continue
        seen.add((e.vid, e.pid))
        lines.append(f"# {e.description}")
        lines.append(
            f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{e.vid:04x}", '
            f'ATTR{{idProduct}}=="{e.pid:04x}", GROUP="{group}", MODE="0660"')
    return "\n".join(lines) + "\n"


def _choose_escalation_method() -> str | None:
    # The privileged-exec method to use: ``pkexec`` (gui), ``sudo``, or ``None``
    if shutil.which("pkexec"):
        return "pkexec"
    if shutil.which("sudo"):
        return "sudo"
    return None


def run_privileged(shell_cmd: str, method: str) -> int:
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


def _install_rules_and_touch(tmp_rule_file: str, group: str, dev_node_path: str | None) -> str:
    # Installs wifit3's rule file, reloads rules, and chowns the node to invoke the new permissions.
    steps = [
        f"install -m 0644 {tmp_rule_file} {RULE_PATH}",
        "udevadm control --reload-rules",
    ]
    if dev_node_path:
        steps += [f"chgrp {group} {dev_node_path}", f"chmod 0660 {dev_node_path}"]
    return " && ".join(steps)


def _delete_rules_and_touch(node: str | None) -> str:
    # Deletes wifit3's rule file, reloads rules, and chowns the node to invoke the new permissions.
    steps = [
        f"rm -f {RULE_PATH}",
        "udevadm control --reload-rules",
    ]
    if node:
        # subshell so `|| true` rescues only the chown: `&&` and `||` are equal precedence,
        # so a bare `... && chown || true` would let `true` mask an rm/reload failure too
        steps.append(f"(chown root:root {node} 2>/dev/null || true)")
    return " && ".join(steps)


def _manual_hint() -> str:
    """The copy-paste fallback when no graphical elevator is available (headless boxes)."""
    return (f"No pkexec/sudo found. Either run `sudo .venv/bin/python3 -m wifit3`, or install manually: "
            f"sudo cp <rule> {RULE_PATH} && sudo udevadm control --reload-rules, then replug.")


def install_rule(*, node: str | None = None) -> LinuxSetupResult:
    """Install the blanket udev rule (all supported cards) under one ``pkexec`` prompt — or, if
    already root, do nothing (root opens + detaches the node directly, no rule needed).
    """
    if not sys.platform.startswith("linux"):
        raise RuntimeError("install_rule is Linux-only")

    if os.geteuid() == 0:
        return LinuxSetupResult(
            ok=True, detail=RULE_PATH,
            message="Running as root — no access rule needed; the device opens directly.")

    group = _access_group()
    if group is None:
        # No system admin group to chgrp the node to — writing the rule anyway would let udev
        # silently drop it (the OWNER="uid-1000-user" trap). Say so now, not via a later timeout.
        return LinuxSetupResult(
            ok=False,
            detail="Add yourself: `sudo usermod -aG sudo $USER`, then log out and back in. "
                   "Or run `sudo .venv/bin/python3 -m wifit3`.",
            message="You're not in the sudo or wheel group, so device access can't be granted.")

    text = emit_udev_text(group)

    # sudo/wheel check
    method = _choose_escalation_method()
    if method is None:
        return LinuxSetupResult(
            ok=False, detail=_manual_hint(),
            message="No priviledge elevator (pkexec/sudo) found to install the access rule.")

    # Write rule
    tmp_rule = str(Path(tempfile.gettempdir()) / "wifit3.rules")
    try:
        Path(tmp_rule).write_text(text)
    except OSError as e:
        return LinuxSetupResult(ok=False, message=f"Couldn't stage the access rule: {e}")

    # Install & touch
    rc = run_privileged(_install_rules_and_touch(tmp_rule, group, node), method)
    if rc == 0:
        return LinuxSetupResult(ok=True, detail=RULE_PATH,
                                message="Installed the device-access rule.")
    if rc == 126:
        return LinuxSetupResult(
            ok=False, cancelled=True,
            message="Authorization dismissed — the access rule was not installed.")
    return LinuxSetupResult(ok=False, detail=_manual_hint(),
                            message=f"Couldn't install the access rule (exit {rc}).")


def remove_rule(*, node: str | None = None) -> LinuxSetupResult:
    """Remove the blanket udev rule — the uninstall (✕) button."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("remove_rule is Linux-only")

    if not Path(RULE_PATH).exists():
        return LinuxSetupResult(
            ok=True, detail=RULE_PATH,
            message="No wifit3 access rule is installed — nothing to remove.")

    # User is root: directly take ownership of the device
    if os.geteuid() == 0:
        try:
            Path(RULE_PATH).unlink(missing_ok=True)
            subprocess.call(["udevadm", "control", "--reload-rules"])
            if node and Path(node).exists():
                subprocess.call(["chown", "root:root", node])
        except OSError as e:
            return LinuxSetupResult(ok=False, message=f"Couldn't remove the access rule: {e}")
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=_REMOVED_MSG)

    # Non-root, escalation
    method = _choose_escalation_method()
    if method is None:
        return LinuxSetupResult(
            ok=False,
            detail=f"sudo rm -f {RULE_PATH} && sudo udevadm control --reload-rules",
            message="No graphical elevator (pkexec/sudo) found to remove the access rule.")

    rc = run_privileged(_delete_rules_and_touch(node), method)
    if rc == 0:
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=_REMOVED_MSG)
    if rc == 126:
        return LinuxSetupResult(
            ok=False, cancelled=True,
            message="Authorization dismissed — the access rule was not removed.")
    return LinuxSetupResult(ok=False, detail=RULE_PATH,
                            message=f"Couldn't remove the access rule (exit {rc}).")
