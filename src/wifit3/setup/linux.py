"""Linux kernel-driver access via a blanket udev permission rule. [DEVICE-SETUP.md Tier 1]

The Linux analog of :mod:`wifit3.setup.windows`. Where Windows must *bind* the card to WinUSB
so libusb can open it, Linux only needs the user to have write-access to the card's usbfs node
(``/dev/bus/usb/BBB/DDD``): with that, a non-root process can both open the device AND detach
its kernel driver — ``USBDEVFS_DISCONNECT`` keys off node write-access, not ``CAP_SYS_ADMIN``
(measured 2026-06-09; DEVICE-SETUP.md L1).

A udev permission rule is **permission-only**: it sets the node's group and changes nothing
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

import grp
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
# One shared rule for every supported VID:PID. The 60- prefix sorts after 50-udev-default
# (whose 0664 root:root node perms we override) and ahead of the seat/uaccess files we don't use.
RULE_PATH = f"{RULE_DIR}/60-wifit3.rules"

_REMOVED_MSG = ("Removed the device-access rule. Replug a card to restore its normal "
                "Wi-Fi driver.")

# The admin groups whose members get node access, in preference order: Debian/Kali ship "sudo",
# Fedora/Arch/RHEL ship "wheel". Both are system groups (gid < 1000), which is why udev actually
# applies GROUP= for them — it silently ignores a non-system owner/group (e.g. a uid-1000 login
# user), the trap that makes a rule parse cleanly yet do nothing.
_ADMIN_GROUPS = ("sudo", "wheel")


def _access_group() -> str | None:
    # The admin group the current process is actually a member of (sudo, else wheel) — None if
    # neither, so install_rule can fail loudly rather than write a rule udev silently drops. The
    # membership test mirrors what the kernel enforces, so a hit guarantees the node, once chgrp'd
    # to this group, is writable by us.
    mine = set(os.getgroups()) | {os.getgid()}
    for name in _ADMIN_GROUPS:
        try:
            if grp.getgrnam(name).gr_gid in mine:
                return name
        except KeyError:
            pass
    return None


def _perms_clause(group: str) -> str:
    # The rule's perms for future plugs: a system admin group owns the node at 0660. A system gid
    # (< 1000) is the point — udev applies GROUP= for it, but silently ignores a non-system owner
    # (a uid-1000 login user), the trap that makes a rule parse yet grant nothing. The
    # already-plugged node is chgrp'd directly at install time (see _install_shell).
    return f'GROUP="{group}", MODE="0660"'


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


def build_rule_text(ids: list[tuple[int, int, str]], group: str,
                    *, blanket: bool = False) -> str:
    """A udev rules file granting ``group`` RW on each VID:PID's usbfs node.

    One deduped rule per VID:PID, matching the usb_device (``ATTR{idVendor}``/``idProduct``,
    lowercase hex, no ``0x``) — the canonical "let libusb open + detach this device" shape.

    Each card's description goes on its OWN comment line ABOVE its rule, never as a trailing
    ``# ...`` on the rule line: modern udev only accepts ``#`` at the start of a line and
    rejects the WHOLE rule ("a comma between tokens is expected") if a comment trails it. (This
    bit us once; ``tests/setup/test_linux.py`` asserts no rule line carries an inline ``#``.)
    """
    clause = _perms_clause(group)
    scope = "all supported cards" if blanket else "one activated card"
    lines = [
        "# wifit3 Linux device-access rule (generated from the driver registry).",
        f"# Scope: {scope}. Group: {group} -> {clause}",
        "# Grants the admin group RW on the card's usbfs node so wifit3 can open it AND detach",
        "# the kernel driver without root. Permission only — it never unbinds the driver or",
        "# changes how the card works as normal Wi-Fi; wifit3 detaches at runtime (replug undoes).",
        "# Remove to revoke: rm the file + udevadm control --reload-rules, then replug (or use ✕).",
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


def emit_udev_text(group: str | None = None) -> str:
    """The blanket all-supported-cards udev file, sourced from the live driver registry.

    The single source of truth for what :func:`install_rule` writes and what ``wifit3
    --emit-udev`` prints, so the installed rule never drifts from the supported-hardware list.
    ``group`` defaults to the user's admin group (sudo/wheel); for the CLI dump it falls back to
    ``sudo`` when the user is in neither, so the printed rule is still valid to hand-edit.
    """
    from wifit3.setup import ids_from_registry

    ids = [(e.vid, e.pid, e.description) for e in ids_from_registry()]
    return build_rule_text(ids, group or _access_group() or _ADMIN_GROUPS[0], blanket=True)


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


def _install_shell(tmp: str, group: str, node: str | None) -> str:
    # Write the rule (so future plugs are handled by udev), reload, then chgrp the already-plugged
    # node by hand so it's usable now without a replug. We set the LIVE node directly instead of
    # via `udevadm trigger`: a synthetic trigger re-applies a rule's MODE but NOT its GROUP, so it
    # never grants the running device — only the chgrp does. [verified on RT5572]
    grant = f" && chgrp {group} {node} && chmod 0660 {node}" if node else ""
    return f"install -m 0644 {tmp} {RULE_PATH} && udevadm control --reload-rules{grant}"


def _remove_shell(node: str | None) -> str:
    # Delete the rule (stops future plugs), reload, then chown the live node back to root so access
    # is revoked now. Same reason as grant: a trigger won't reset the granted GROUP — udev applies
    # a rule but never un-applies it — so we reset by hand. Tolerates an already-unplugged node.
    reset = f" && (chown root:root {node} 2>/dev/null || true)" if node else ""
    return f"rm -f {RULE_PATH} && udevadm control --reload-rules{reset}"


def _manual_hint() -> str:
    """The copy-paste fallback when no graphical elevator is available (headless boxes)."""
    return (f"No pkexec/sudo found. Either run `sudo wifit3`, or install manually: "
            f"sudo cp <rule> {RULE_PATH} && sudo udevadm control --reload-rules, then replug.")


def install_rule(*, node: str | None = None) -> LinuxSetupResult:
    """Install the blanket udev rule (all supported cards) under one ``pkexec`` prompt — or, if
    already root, do nothing (root opens + detaches the node directly, no rule needed).

    Blocks on the graphical password dialog — call OFF the event loop. The rule grants the user's
    admin group (sudo/wheel) RW on every supported VID:PID's node on future plugs; ``node`` (the
    card's ``/dev/bus/usb/BBB/DDD``) is ALSO chgrp'd directly so the already-plugged card is usable
    now without a replug. With node access, ``connect()``'s non-root open + kernel detach succeed.
    This does NOT unbind any driver (that's the per-session runtime detach in the driver's
    ``_claim()``); cards stay normal Wi-Fi adapters until then, and a replug re-attaches. Fails
    loudly if the user is in no admin group. Returns a :class:`LinuxSetupResult`; raises only for a
    non-Linux host.
    """
    if not sys.platform.startswith("linux"):
        raise RuntimeError("install_rule is Linux-only")

    if os.geteuid() == 0:
        # Zero-install path: root opens + detaches the usbfs node directly, so no rule (and no
        # admin-group lookup) is needed. [DEVICE-SETUP.md L197]
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
                   "Or run `sudo wifit3`.",
            message="You're not in the sudo or wheel group, so device access can't be granted.")

    text = emit_udev_text(group)

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

    rc = run_privileged(_install_shell(tmp, group, node), method)
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
    """Remove the blanket udev rule — the uninstall (✕) button, Linux side.

    One ``pkexec`` prompt: delete the shared rule, reload, and chown ``node`` (the card's
    ``/dev/bus/usb/BBB/DDD``) back to root so the live device loses access immediately — no replug.
    Deleting the single shared file revokes **all** supported cards' future access; the chown only
    resets the one card passed (others revert on their next replug). No driver is re-attached here
    (we never replaced one — the runtime detach is per-session), so each returns to normal Wi-Fi on
    its next replug. A missing rule is a benign no-op (``ok=True``). Blocks on the dialog — call off
    the loop. Raises only for a non-Linux host.
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
            if node and Path(node).exists():
                subprocess.call(["chown", "root:root", node])
        except OSError as e:
            return LinuxSetupResult(ok=False, message=f"Couldn't remove the access rule: {e}")
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=_REMOVED_MSG)

    method = _choose_method()
    if method is None:
        return LinuxSetupResult(
            ok=False,
            detail=f"sudo rm -f {RULE_PATH} && sudo udevadm control --reload-rules",
            message="No graphical elevator (pkexec/sudo) found to remove the access rule.")

    rc = run_privileged(_remove_shell(node), method)
    if rc == 0:
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=_REMOVED_MSG)
    if rc == 126:
        return LinuxSetupResult(
            ok=False, cancelled=True,
            message="Authorization dismissed — the access rule was not removed.")
    return LinuxSetupResult(ok=False, detail=RULE_PATH,
                            message=f"Couldn't remove the access rule (exit {rc}).")
