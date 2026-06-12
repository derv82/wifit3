"""Linux kernel-driver access via udev permission rules. [DEVICE-SETUP.md Tier 1]

The Linux analog of :mod:`wifit3.setup.windows`. Where Windows must *bind* the card to WinUSB
so libusb can open it, Linux only needs the user to have write-access to the card's usbfs node
(``/dev/bus/usb/BBB/DDD``): with that, a non-root process can both open the device AND detach
its kernel driver — ``USBDEVFS_DISCONNECT`` keys off node write-access, not ``CAP_SYS_ADMIN``
(measured 2026-06-09; DEVICE-SETUP.md L1).

A udev permission rule is **permission-only**: it sets the node's owner/ACL and changes nothing
else — the kernel driver still binds, the card is still a normal Wi-Fi adapter, until wifit3
detaches it at *runtime* (per-session; replug undoes).

**Who a rule grants access to** is the ``all`` perms clause = ``TAG+="uaccess"`` *and*
``GROUP="plugdev", MODE="0660"`` together — two complementary mechanisms (DEVICE-SETUP.md):

  * ``uaccess`` — a dynamic logind ACL for the user on the *active local seat*. Auto-revoked at
    logout; **excludes SSH** (an SSH login isn't a seat session). Tightest scope.
  * ``plugdev`` — the (Debian/Ubuntu/Kali) pluggable-device group. Persistent, and it **works
    over SSH** — the common "laptop in a bag, phone SSH'd in" operator case. On non-Debian
    distros the group often doesn't exist, where the clause is inert and uaccess carries access.

So the two clauses cover different *users* and different *distros*; that's the belt-and-suspenders.

**Scope is a per-card choice** (the splash's two grant buttons): grant just the activated card,
or all supported cards. There is **one** rule file, and the file is its own source of truth —
:func:`granted_ids` parses the currently-granted VID:PIDs back out of it, and every change is a
**full rewrite** from that mutated set (never a surgical in-place edit, which a stray ``#`` or a
botched regex could corrupt). Grant-one *unions* into the set; revoke-one *subtracts* (deleting
the file when it empties); revoke-all deletes the file.

- :func:`grant_access` — grant one card (``(vid, pid, desc)``) or all (``None``).
- :func:`revoke_access` — revoke one card or all (the uninstall ✕ button's Linux side).
- :func:`emit_udev_text` — the blanket all-cards file for the ``--emit-udev`` power-user CLI.

The privileged step blocks on a graphical ``pkexec`` dialog, so callers run it OFF the Textual
event loop (the splash uses ``asyncio.to_thread``), exactly like :func:`install_winusb`.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

RULE_DIR = "/etc/udev/rules.d"
# One shared rule file for every granted VID:PID. The 60- prefix sorts BEFORE systemd's
# 70-uaccess.rules: the uaccess builtin only grants the seat ACL for devices already TAG-ed
# "uaccess" when it runs, so our TAG+="uaccess" must be set by an earlier-sorting file or the
# ACL is never applied.
RULE_PATH = f"{RULE_DIR}/60-wifit3.rules"

# Per-card perms clause applied to the usbfs node. The shipping default is "all" = uaccess
# (logind grants a dynamic seat ACL — tightest, revoked on logout, no SSH) PLUS plugdev/0660
# (persistent, works over SSH and on pre-systemd / non-uaccess boxes): the two together so the
# node is writable for both the at-keyboard user and the SSH operator. The probe's "loose"
# (0666 world-RW) lever is omitted here — it exists only to isolate a measurement, never ships.
_PERMS_CLAUSE = {
    "uaccess": 'TAG+="uaccess"',
    "plugdev": 'MODE="0660", GROUP="plugdev"',
    "all": 'TAG+="uaccess", MODE="0660", GROUP="plugdev"',
}
_DEFAULT_PERMS = "all"

_REMOVED_ALL_MSG = ("Removed all device-access rules. Replug a card to restore its normal "
                    "Wi-Fi driver.")
_NO_ELEVATOR_MSG = "No graphical elevator (pkexec/sudo) found to change device access."

# Pulls the VID and PID hex out of one generated rule line. The file is our own output, so the
# shape is fixed (ATTR{idVendor}=="vvvv", ATTR{idProduct}=="pppp"); this reads it back so the
# file can act as the granted-set source of truth.
_RULE_LINE_RE = re.compile(r'idVendor\}=="([0-9a-fA-F]+)".*?idProduct\}=="([0-9a-fA-F]+)"')


@dataclass(frozen=True)
class LinuxSetupResult:
    """Outcome of a privileged Linux setup action (grant / revoke).

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
    scope = "all supported cards" if blanket else "the activated card(s)"
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


def _registry_ids() -> list[tuple[int, int, str]]:
    """Every supported ``(vid, pid, description)``, from the live driver registry."""
    from wifit3.setup import ids_from_registry

    return [(e.vid, e.pid, e.description) for e in ids_from_registry()]


def supported_count() -> int:
    """Number of supported VID:PIDs — i.e. how many rule lines a blanket grant writes.

    The single source of the count the access dialog shows ("all USB Wi-Fi (N)"), so the
    advertised number always equals the rule lines actually written.
    """
    return len(_registry_ids())


def plugdev_members() -> list[str]:
    """Members of the ``plugdev`` group, for the access dialog's transparency line.

    On a single-user desktop this is usually one name (the installer-created user) — which is
    the reassurance, not the scare. Empty list when the group doesn't exist (most non-Debian
    distros) or can't be read; there ``uaccess`` is what carries access, not plugdev.
    """
    try:
        import grp

        return list(grp.getgrnam("plugdev").gr_mem)
    except (KeyError, OSError, ImportError):
        return []


def emit_udev_text(perms: str = _DEFAULT_PERMS) -> str:
    """The blanket all-supported-cards udev file, sourced from the live driver registry.

    What ``wifit3 --emit-udev`` prints for manual install — the same text :func:`grant_access`
    (all) would write, so the manual path never drifts from the supported-hardware list.
    """
    return build_rule_text(_registry_ids(), perms, blanket=True)


def granted_ids() -> set[tuple[int, int]]:
    """The VID:PIDs currently granted, parsed back out of the installed rule file.

    The file is its own source of truth — no sidecar state — so add-one / remove-one read the
    set, mutate it, and rewrite. Empty set when no rule is installed (or it's unreadable).
    """
    try:
        text = Path(RULE_PATH).read_text()
    except OSError:
        return set()
    out: set[tuple[int, int]] = set()
    for line in text.splitlines():
        if not line.startswith("SUBSYSTEM"):
            continue
        m = _RULE_LINE_RE.search(line)
        if m:
            out.add((int(m.group(1), 16), int(m.group(2), 16)))
    return out


def _rebuild_text(granted: set[tuple[int, int]], perms: str = _DEFAULT_PERMS) -> str:
    """Regenerate the whole rule file for ``granted``, descriptions sourced from the registry.

    A full rewrite from the (parsed, mutated) set — never a surgical in-place edit, so a
    single grant/revoke can't corrupt the file. The ``blanket`` header is set only when
    ``granted`` covers every supported card, so the file's scope comment reads honestly.
    """
    reg = _registry_ids()
    desc = {(v, p): d for v, p, d in reg}
    all_ids = {(v, p) for v, p, _ in reg}
    ids = [(v, p, desc.get((v, p), f"{v:04x}:{p:04x}")) for v, p in sorted(granted)]
    return build_rule_text(ids, perms, blanket=bool(all_ids) and granted >= all_ids)


def _choose_method() -> str | None:
    """The privileged-exec method: ``pkexec`` (graphical) preferred, ``sudo`` fallback, or
    ``None`` when neither is present (caller surfaces the manual copy-paste path)."""
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


def _privileged_write(text: str, ok_msg: str) -> LinuxSetupResult:
    """Write ``text`` to ``RULE_PATH`` (+ reload/trigger) as root; classify the outcome.

    Direct write when already root (``sudo wifit3`` — no second prompt); otherwise one
    ``pkexec``/``sudo`` prompt via :func:`run_privileged`.
    """
    if os.geteuid() == 0:
        try:
            Path(RULE_PATH).write_text(text)
            os.chmod(RULE_PATH, 0o644)
            subprocess.call(["udevadm", "control", "--reload-rules"])
            subprocess.call(["udevadm", "trigger", "--action=add", "--subsystem-match=usb"])
        except OSError as e:
            return LinuxSetupResult(ok=False, message=f"Couldn't write the access rule: {e}")
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=ok_msg)

    method = _choose_method()
    if method is None:
        return LinuxSetupResult(ok=False, detail=_manual_hint(), message=_NO_ELEVATOR_MSG)

    tmp = str(Path(tempfile.gettempdir()) / "wifit3.rules")
    try:
        Path(tmp).write_text(text)
    except OSError as e:
        return LinuxSetupResult(ok=False, message=f"Couldn't stage the access rule: {e}")

    rc = run_privileged(_install_shell(tmp), method)
    if rc == 0:
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=ok_msg)
    if rc == 126:
        return LinuxSetupResult(ok=False, cancelled=True,
                                message="Authorization dismissed — no changes were made.")
    return LinuxSetupResult(ok=False, detail=_manual_hint(),
                            message=f"Couldn't update device access (exit {rc}).")


def _privileged_remove(ok_msg: str) -> LinuxSetupResult:
    """Delete ``RULE_PATH`` (+ reload) as root; classify the outcome (root: direct; else one prompt)."""
    if os.geteuid() == 0:
        try:
            Path(RULE_PATH).unlink(missing_ok=True)
            subprocess.call(["udevadm", "control", "--reload-rules"])
        except OSError as e:
            return LinuxSetupResult(ok=False, message=f"Couldn't remove the access rule: {e}")
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=ok_msg)

    method = _choose_method()
    if method is None:
        return LinuxSetupResult(
            ok=False,
            detail=f"sudo rm -f {RULE_PATH} && sudo udevadm control --reload-rules",
            message=_NO_ELEVATOR_MSG)

    rc = run_privileged(_remove_shell(), method)
    if rc == 0:
        return LinuxSetupResult(ok=True, detail=RULE_PATH, message=ok_msg)
    if rc == 126:
        return LinuxSetupResult(ok=False, cancelled=True,
                                message="Authorization dismissed — no changes were made.")
    return LinuxSetupResult(ok=False, detail=RULE_PATH,
                            message=f"Couldn't remove the access rule (exit {rc}).")


def grant_access(card: tuple[int, int, str] | None,
                 perms: str = _DEFAULT_PERMS) -> LinuxSetupResult:
    """Grant userland access to one card (``(vid, pid, desc)``) or to ALL supported cards (``None``).

    The single rule file is its own state: a one-card grant is *unioned* into whatever's
    already granted and the file fully rewritten, so granting card B never drops card A.
    Blocks on the pkexec dialog — call OFF the event loop. Permission only; no driver is
    unbound (that's the per-session runtime detach). Raises only on a non-Linux host (mirrors
    :func:`install_winusb`'s guard).
    """
    if not sys.platform.startswith("linux"):
        raise RuntimeError("grant_access is Linux-only")

    if card is None:
        granted = {(v, p) for v, p, _ in _registry_ids()}
        ok_msg = f"Granted access to all {len(granted)} supported cards."
    else:
        vid, pid, desc = card
        granted = granted_ids() | {(vid, pid)}
        ok_msg = f"Granted access to {desc}."
    return _privileged_write(_rebuild_text(granted, perms), ok_msg)


def revoke_access(card: tuple[int, int, str] | None) -> LinuxSetupResult:
    """Revoke access for one card (``(vid, pid, ...)``) or ALL (``None`` → delete the file).

    One-card removal rewrites the file without that VID:PID — and deletes it if that was the
    last one; all-removal deletes the file outright. We never replaced a driver (the runtime
    detach is per-session), so each affected card returns to normal Wi-Fi on its next replug.
    A revoke of something not granted, or an all-revoke with no file, is a benign no-op
    (``ok=True``). Blocks on the dialog — call off the loop. Raises on non-Linux.
    """
    if not sys.platform.startswith("linux"):
        raise RuntimeError("revoke_access is Linux-only")

    if card is None:
        if not Path(RULE_PATH).exists():
            return LinuxSetupResult(
                ok=True, detail=RULE_PATH,
                message="No wifit3 access rule is installed — nothing to remove.")
        return _privileged_remove(_REMOVED_ALL_MSG)

    vid, pid = card[0], card[1]
    desc = card[2] if len(card) > 2 else f"{vid:04x}:{pid:04x}"
    current = granted_ids()
    if (vid, pid) not in current:
        return LinuxSetupResult(
            ok=True, detail=RULE_PATH,
            message=f"{desc} wasn't granted access — nothing to remove.")

    ok_msg = f"Removed access to {desc}. Replug it to restore its Wi-Fi driver."
    granted = current - {(vid, pid)}
    if not granted:  # that was the last card → drop the whole file
        return _privileged_remove(ok_msg)
    return _privileged_write(_rebuild_text(granted), ok_msg)
