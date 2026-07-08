"""Linux "hand wifit3 this chipset" setup — the Zadig/WinUSB analog.

We can't keep the card usable as a normal adapter *and* drive it from userland: the kernel binds
its driver and uploads firmware the instant the card enumerates, tainting the cold-boot state the
port replays against. A udev permission rule alone doesn't stop that bind — it only chmods the
node. So Linux mirrors the Windows model: the user opts in to give wifit3 **complete control of one
chipset**, which writes a per-chipset *pair* of files —

* ``/etc/modprobe.d/wifit3-<chip>.conf``   — ``blacklist``/``install`` the kernel module(s), so the
  kernel never binds + taints the card again (this is what keeps it cold).
* ``/etc/udev/rules.d/60-wifit3-<chip>.rules`` — grant the user's admin group access to the raw USB
  node, so wifit3 can open it without sudo.

The module list is discovered *live* from the plugged-in card (sysfs bound-driver ∪ ``modprobe -R``
on its modalias), with the driver's ``CONFLICTING_LINUX_MODULES`` as a fallback hint — so it reflects what's
actually installed (mainline vs DKMS) rather than a hand-list that rots. Uninstall deletes both
files; the card returns to its normal Wi-Fi driver on the next replug.

The blacklist only stops *future* binds, and an already-resident module still binds a freshly
plugged device, so install also best-effort ``modprobe -r``s the module — but a clean cold start
still needs a physical replug, which the splash asks for.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wifit3.setup import SetupTarget

logger = logging.getLogger(__name__)

RULE_DIR = "/etc/udev/rules.d"
BLACKLIST_DIR = "/etc/modprobe.d"

# Sysfs USB tree — a module constant so tests can repoint it at a fixture.
SYSFS_USB = "/sys/bus/usb/devices"

# The shared mac80211/cfg80211 stack (and usbfs, which is what libusb leaves bound once we claim
# the card) must never land in a blacklist — only the leaf USB-binding driver does.
_NEVER_BLACKLIST = frozenset({
    "usbcore", "usbfs", "usbhid", "rfkill",
    "mac80211", "cfg80211", "ath", "ath9k_common", "ath9k_hw",
    # Bluetooth stack — a combo Wi-Fi+BT card (e.g. RTL8821CU) binds btusb on its BT
    # interface, and modprobe blacklist is module-global, so blacklisting it kills ALL
    # system Bluetooth (this card's *and* the laptop's internal BT), not just this Wi-Fi card.
    "btusb", "bluetooth", "btrtl", "btintel", "btbcm", "btmtk",
})

_REMOVED_MSG = ("Removed the udev rule + blocklist for this chipset. Replug the card to restore its "
                "normal Wi-Fi driver.")
_REPLUG_MSG = ("udev rule + blocklist installed. Unplug and replug the card, then press START.")


def _safe(key: str) -> str:
    """Filename-safe chipset key (the registry keys already are, but never trust an interpolation
    into a privileged ``rm``/``install`` path)."""
    return "".join(c if (c.isalnum() or c in "_-") else "-" for c in key) or "chip"


def rule_path(key: str) -> str:
    # 60- sorts after 50-udev-default so our GROUP/MODE wins.
    return f"{RULE_DIR}/60-wifit3-{_safe(key)}.rules"


def blacklist_path(key: str) -> str:
    return f"{BLACKLIST_DIR}/wifit3-{_safe(key)}.conf"


def _access_group() -> str | None:
    import grp  # Unix-only
    mine = set(os.getgroups()) | {os.getgid()}
    for name in ("sudo", "wheel"):
        try:
            if grp.getgrnam(name).gr_gid in mine:
                return name
        except KeyError:
            pass
    return None


def current_user() -> str:
    import pwd  # Unix-only
    return pwd.getpwuid(os.getuid()).pw_name


@dataclass(frozen=True)
class LinuxSetupResult:
    ok: bool
    message: str
    cancelled: bool = False
    detail: str | None = None


# --- live kernel-module discovery ---------------------------------------------------------------

def _matching_usb_dirs(ids):
    """Yield the sysfs device dirs whose idVendor:idProduct is in ``ids``."""
    base = Path(SYSFS_USB)
    if not base.exists():
        return
    want = {(f"{v:04x}", f"{p:04x}") for v, p in ids}
    for d in sorted(base.iterdir()):
        try:
            vid = (d / "idVendor").read_text().strip().lower()
            pid = (d / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if (vid, pid) in want:
            yield d


def _bound_modules(ids) -> set[str]:
    """The kernel driver(s) currently bound to the card's interfaces — ground truth for the
    module that grabbed *this* device on *this* machine. The sysfs *driver* name is not always the
    *module* name (out-of-tree Realtek: driver ``rtl8814au`` ← module ``8814au``), and modprobe
    blacklists the *module* — so resolve ``driver/module`` too and keep both."""
    mods: set[str] = set()
    for d in _matching_usb_dirs(ids):
        for intf in sorted(d.glob(f"{d.name}:*")):
            link = intf / "driver"
            try:
                if link.is_symlink():
                    mods.add(os.path.basename(os.readlink(link)))          # sysfs driver name
                    modlink = link / "module"                              # driver -> its .ko module
                    if modlink.is_symlink():
                        mods.add(os.path.basename(os.readlink(modlink)))   # real module to blacklist
            except OSError:
                pass
    return mods


def _card_modaliases(ids):
    """Every modalias for a matching card — device-level *and* per-interface. USB Wi-Fi drivers
    (rt2800usb, ...) bind at the *interface* and match its modalias (``…icFFiscFF…``); some
    devices/kernels expose *only* the interface modalias (no device-level one), so reading the
    device level alone misses the driver entirely."""
    for d in _matching_usb_dirs(ids):
        a = _read(d / "modalias")
        if a:
            yield a
        for intf in sorted(d.glob(f"{d.name}:*")):
            a = _read(intf / "modalias")
            if a:
                yield a


def _resolve_via_modalias(ids) -> set[str]:
    """Every module that *could* claim this card per the installed alias table — catches the
    mainline-and-DKMS-both-installed case the bound-driver read alone would miss."""
    if not shutil.which("modprobe"):
        return set()
    aliases = set(_card_modaliases(ids))
    mods: set[str] = set()
    for alias in aliases:
        try:
            out = subprocess.run(["modprobe", "-R", alias], capture_output=True, text=True,
                                 timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        mods.update(line.strip() for line in out.stdout.split() if line.strip())
    return mods


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def kernel_driver_bound(ids) -> bool:
    """Is a real kernel Wi-Fi driver bound to this card (i.e. it's tainted, not just unbound)?"""
    return bool(_bound_modules(ids) - _NEVER_BLACKLIST)


def discover_kernel_modules(target: SetupTarget) -> list[str]:
    """The module name(s) to blacklist for ``target``: live bound-driver ∪ modalias-resolved ∪ the
    driver's hardcoded hint, minus the shared stack we must never touch. Over-listing an absent
    module is harmless (blacklisting nothing on this machine)."""
    mods = (_bound_modules(target.ids) | _resolve_via_modalias(target.ids)
            | {m for m in target.module_hints if m})
    return sorted(mods - _NEVER_BLACKLIST)


# --- file text emitters -------------------------------------------------------------------------

def emit_udev_text(target: SetupTarget, group: str) -> str:
    """The per-chipset access rule: every VID:PID this driver claims, granted to ``group``. The
    blacklist displaces *all* of them from the kernel, so all of them need userland access too."""
    lines = [f"# wifit3 device-access rule — {target.description} ({target.key})", ""]
    seen: set[tuple[int, int]] = set()
    for vid, pid in target.ids:
        if (vid, pid) in seen:
            continue
        seen.add((vid, pid))
        lines.append(
            f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{vid:04x}", '
            f'ATTR{{idProduct}}=="{pid:04x}", GROUP="{group}", MODE="0660"')
    return "\n".join(lines) + "\n"


def emit_blacklist_text(target: SetupTarget, modules: list[str]) -> str:
    """The per-chipset modprobe blacklist. ``install … /bin/true`` closes the loads ``blacklist``
    alone misses (by-name / dependency), so nothing pulls the kernel driver back in."""
    lines = [
        f"# wifit3 hands {target.description} ({target.key}) to userland.",
        "# Stops the kernel driver binding + uploading firmware (which taints the cold-boot state",
        "# the wifit3 port replays). Delete this file (uninstall) to return the card to normal.",
        "",
    ]
    for m in modules:
        lines.append(f"blacklist {m}")
        lines.append(f"install {m} /bin/true")
    return "\n".join(lines) + "\n"


# --- privileged shell builders ------------------------------------------------------------------

def _install_cmd(*, tmp_rule: str | None, key: str, tmp_blacklist: str | None,
                 modules: list[str], group: str | None, node: str | None) -> str:
    # Every interpolated value is shlex.quote'd: this string is handed to `sh -c`, so an unquoted
    # path with a space (a temp dir, a repointed target) would word-split, and a metachar would be
    # worse. quote() is a no-op on the safe values we actually pass, so the shape is unchanged.
    q = shlex.quote
    steps: list[str] = []
    if tmp_rule:
        steps.append(f"install -m 0644 {q(tmp_rule)} {q(rule_path(key))}")
    if tmp_blacklist:
        steps.append(f"install -m 0644 {q(tmp_blacklist)} {q(blacklist_path(key))}")
    steps.append("udevadm control --reload-rules")
    if modules:
        # Best-effort: frees the warm card now and stops an already-resident module re-grabbing the
        # device on replug. Fails harmlessly if another card holds the module — the blacklist still
        # keeps it from *loading* on a future cold boot.
        mods = " ".join(q(m) for m in modules)
        steps.append(f"(modprobe -r {mods} 2>/dev/null || true)")
    if group and node:
        # Ignore failures in the ch* commands in case the device re-enumerated to a different node.
        steps += [f"(chgrp {q(group)} {q(node)} || true)", f"(chmod 0660 {q(node)} || true)"]
    return " && ".join(steps)


def _remove_cmd(keys: list[str], node: str | None) -> str:
    q = shlex.quote
    paths: list[str] = []
    for k in keys:
        paths += [rule_path(k), blacklist_path(k)]
    steps = [
        f"rm -f {' '.join(q(p) for p in paths)}",
        "udevadm control --reload-rules",
    ]
    if node:
        # subshell so `|| true` rescues only the chown, not the rm/reload (equal precedence). Only
        # the selected card's live node is revoked; siblings return to the kernel on their replug.
        steps.append(f"(chown root:root {q(node)} 2>/dev/null || true)")
    return " && ".join(steps)


def _choose_escalation_method() -> str | None:
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
        proc = subprocess.run(argv, stderr=subprocess.PIPE, text=True)
    except KeyboardInterrupt:
        return 130
    if proc.returncode == 0:
        logger.info("Linux setup: elevated command succeeded")
    else:
        logger.warning("Linux setup: elevated command failed (rc=%d): %s",
                       proc.returncode, (proc.stderr or "").strip())
    return proc.returncode


def _run_as_root(shell_cmd: str) -> int:
    sh = shutil.which("sh") or "/bin/sh"
    logger.info("Linux setup (root): sh -c %r", shell_cmd)
    try:
        return subprocess.call([sh, "-c", shell_cmd])
    except KeyboardInterrupt:
        return 130


def _manual_hint(key: str) -> str:
    return (f"No pkexec/sudo found. Either run `sudo .venv/bin/python3 -m wifit3`, or install "
            f"manually: copy the rule to {rule_path(key)} + the blacklist to {blacklist_path(key)}, "
            f"then `sudo udevadm control --reload-rules` and replug.")


def _stage(name: str, text: str | None) -> str | None:
    if text is None:
        return None
    p = str(Path(tempfile.gettempdir()) / name)
    Path(p).write_text(text)
    return p


# --- uninstall planning: shared-module reference counting ---------------------------------------
#
# One kernel module can back several distinct wifit3 chipsets (rt5372, rt3070, rt2800usb, ... all
# bind rt2800usb.ko). The blacklist is module-granular, so handing over one such card blocks the
# module for the whole family. The reference count is *implicit*: modprobe unions every
# ``/etc/modprobe.d/*.conf``, so a module stays blacklisted while any wifit3 conf still lists it and
# is lifted only when the last one is removed. We never compute "what to unblacklist" — we read
# across our own files to (a) tell the user which sibling still blocks a card and (b) offer a wide
# uninstall that clears the whole family. Only ever our own ``wifit3-*`` files are read or deleted.

_CONF_PREFIX = "wifit3-"
_CONF_SUFFIX = ".conf"
_HANDS_RE = re.compile(r"# wifit3 hands (.+) \(([^)]+)\) to userland")


@dataclass(frozen=True)
class SiblingConf:
    """Another installed wifit3 chipset whose blacklist shares ≥1 kernel module with the target."""
    key: str
    description: str
    modules: tuple[str, ...]


@dataclass(frozen=True)
class UninstallPlan:
    """What removing a chipset entails — drives the splash's narrow/wide uninstall choice."""
    key: str
    own_modules: tuple[str, ...]
    siblings: tuple[SiblingConf, ...]
    has_own_files: bool

    @property
    def removable(self) -> bool:
        """Is there anything for us to remove (our own files, or a sibling blocking this card)?"""
        return self.has_own_files or bool(self.siblings)


def modules_in_conf(path) -> set[str]:
    """The kernel modules a wifit3 blacklist conf blocks — its ``blacklist <mod>`` lines."""
    mods: set[str] = set()
    try:
        text = Path(path).read_text()
    except OSError:
        return mods
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("blacklist "):
            name = stripped.split(None, 1)[1].strip()
            if name:
                mods.add(name)
    return mods


def _installed_blacklist_confs() -> dict[str, Path]:
    """Every installed wifit3 blacklist conf, keyed by its (filename-safe) chipset key."""
    base = Path(BLACKLIST_DIR)
    out: dict[str, Path] = {}
    if not base.exists():
        return out
    for p in sorted(base.glob(f"{_CONF_PREFIX}*{_CONF_SUFFIX}")):
        out[p.name[len(_CONF_PREFIX):-len(_CONF_SUFFIX)]] = p
    return out


def _desc_from_conf(path: Path) -> str | None:
    """Recover the human chipset label from a conf's header comment (self-contained; no registry
    import). Greedy match tolerates parentheses in the description — the ``(key)`` before
    ``to userland`` anchors it."""
    try:
        for line in path.read_text().splitlines():
            m = _HANDS_RE.match(line.strip())
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return None


def confs_blacklisting(module: str) -> list[str]:
    """The wifit3 chipset keys whose blacklist conf blocks ``module``."""
    return sorted(k for k, p in _installed_blacklist_confs().items()
                  if module in modules_in_conf(p))


def _target_modules(target: SetupTarget) -> set[str]:
    """The kernel modules ``target`` blocks: its own conf if installed, else live discovery (which
    works even on an unbound card via modalias) so the never-explicitly-installed sibling case still
    resolves. Falls back to the driver's hints when the card can't be probed."""
    own = Path(blacklist_path(target.key))
    if own.exists():
        return modules_in_conf(own)
    return set(discover_kernel_modules(target))


def plan_uninstall(target: SetupTarget) -> UninstallPlan:
    """What removing ``target`` entails: its kernel modules, the *other* installed wifit3 chipsets
    that share ≥1 of those modules (direct siblings, non-transitive), and whether ``target`` has
    files of its own. Scans only wifit3's files."""
    own_modules = _target_modules(target)
    self_key = _safe(target.key)
    siblings: list[SiblingConf] = []
    for key, path in _installed_blacklist_confs().items():
        if key == self_key:
            continue
        shared = modules_in_conf(path) & own_modules
        if shared:
            siblings.append(SiblingConf(key=key, description=_desc_from_conf(path) or key,
                                        modules=tuple(sorted(shared))))
    has_own = Path(blacklist_path(target.key)).exists() or Path(rule_path(target.key)).exists()
    return UninstallPlan(key=target.key, own_modules=tuple(sorted(own_modules)),
                         siblings=tuple(siblings), has_own_files=has_own)


def _residual_blocked(own_modules: set[str], removed_keys: set[str]) -> dict[str, set[str]]:
    """After removing ``removed_keys``, which of ``own_modules`` are still blocked by a *remaining*
    wifit3 conf, and by whom. Keyed on the intended removal set (not a filesystem re-scan) so it's
    correct whether or not the elevated delete has landed yet."""
    out: dict[str, set[str]] = {}
    if not own_modules:
        return out
    for key, path in _installed_blacklist_confs().items():
        if key in removed_keys:
            continue
        for m in modules_in_conf(path) & own_modules:
            out.setdefault(m, set()).add(key)
    return out


# --- public install / remove --------------------------------------------------------------------

def install_rule(target: SetupTarget, *, node: str | None = None) -> LinuxSetupResult:
    """Hand ``target``'s chipset to wifit3: write the per-chipset blacklist + udev access rule under
    one elevation prompt, reload udev, and best-effort unload the kernel module. The card needs a
    physical replug afterwards to reach a clean cold state (the caller asks for it)."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("install_rule is Linux-only")

    modules = discover_kernel_modules(target)
    is_root = os.geteuid() == 0
    group = None if is_root else _access_group()
    if not is_root and group is None:
        return LinuxSetupResult(
            ok=False,
            detail="Add yourself: `sudo usermod -aG sudo $USER`, then log out and back in. "
                   "Or run `sudo .venv/bin/python3 -m wifit3`.",
            message="You're not in the sudo or wheel group, so device access can't be granted.")

    # As root the access rule is moot (root opens any node) — only the blacklist matters.
    rule_text = emit_udev_text(target, group) if group else None
    blacklist_text = emit_blacklist_text(target, modules) if modules else None

    try:
        tmp_rule = _stage(f"wifit3-{_safe(target.key)}.rules", rule_text)
        tmp_blacklist = _stage(f"wifit3-{_safe(target.key)}.conf", blacklist_text)
    except OSError as e:
        return LinuxSetupResult(ok=False, message=f"Couldn't stage the setup files: {e}")

    cmd = _install_cmd(tmp_rule=tmp_rule, key=target.key, tmp_blacklist=tmp_blacklist,
                       modules=modules, group=group, node=node)

    if is_root:
        rc = _run_as_root(cmd)
    else:
        method = _choose_escalation_method()
        if method is None:
            return LinuxSetupResult(
                ok=False, detail=_manual_hint(target.key),
                message="No graphical elevator (pkexec/sudo) found to install the udev rule + blocklist.")
        rc = run_privileged(cmd, method)

    if rc == 0:
        detail = blacklist_path(target.key) if modules else (
            "Couldn't determine the kernel module to blacklist — the card may be re-grabbed on "
            "replug. Tell us the chipset so we can add a fallback hint.")
        return LinuxSetupResult(ok=True, detail=detail, message=_REPLUG_MSG)
    if rc == 126:
        return LinuxSetupResult(
            ok=False, cancelled=True,
            message="Authorization dismissed — the udev rule + blocklist were not installed.")
    return LinuxSetupResult(ok=False, detail=_manual_hint(target.key),
                            message=f"Couldn't install the udev rule + blocklist (exit {rc}).")


def remove_rule(target: SetupTarget, *, node: str | None = None,
                also_keys: tuple[str, ...] = ()) -> LinuxSetupResult:
    """Return ``target``'s chipset (and any ``also_keys`` siblings) to the kernel: delete their
    per-chipset blacklist + access-rule pairs and reload udev. The normal Wi-Fi driver rebinds on the
    next replug.

    ``also_keys`` is the wide-uninstall radius — sibling chipsets sharing ``target``'s kernel module,
    removed so the shared module is actually freed (see :func:`plan_uninstall`). A narrow uninstall
    passes none; if that leaves the card's module blocked by a remaining sibling, the success message
    says so."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("remove_rule is Linux-only")

    keys = _dedupe([target.key, *also_keys])
    rpath, bpath = rule_path(target.key), blacklist_path(target.key)
    if not any(Path(rule_path(k)).exists() or Path(blacklist_path(k)).exists() for k in keys):
        return LinuxSetupResult(
            ok=True, detail=rpath,
            message="No wifit3 rules installed for this chipset — nothing to remove.")

    # Read our modules before the elevated delete wipes our conf; the residual scan then tells the
    # user if a sibling we're *not* removing still holds them.
    own_modules = modules_in_conf(bpath) if Path(bpath).exists() else set()
    removed_keys = {_safe(k) for k in keys}

    def _ok() -> LinuxSetupResult:
        residual = _residual_blocked(own_modules, removed_keys)
        if residual:
            mods = ", ".join(sorted(residual))
            blockers = ", ".join(sorted({k for who in residual.values() for k in who}))
            return LinuxSetupResult(
                ok=True, detail=rpath,
                message=(f"Removed wifit3's rules for this card. Kernel module(s) {mods} stay "
                         f"blocked by chipset(s) {blockers} still handed to wifit3 — uninstall those "
                         f"too to fully restore this card. Replug to apply."))
        return LinuxSetupResult(ok=True, detail=rpath, message=_REMOVED_MSG)

    cmd = _remove_cmd(keys, node)
    if os.geteuid() == 0:
        rc = _run_as_root(cmd)
        return (_ok() if rc == 0
                else LinuxSetupResult(ok=False, detail=rpath,
                                      message=f"Couldn't remove the udev rule + blocklist (exit {rc})."))

    method = _choose_escalation_method()
    if method is None:
        rmpaths = " ".join(shlex.quote(p) for k in keys for p in (rule_path(k), blacklist_path(k)))
        return LinuxSetupResult(
            ok=False,
            detail=f"sudo rm -f {rmpaths} && sudo udevadm control --reload-rules",
            message="No graphical elevator (pkexec/sudo) found to remove the udev rule + blocklist.")

    rc = run_privileged(cmd, method)
    if rc == 0:
        return _ok()
    if rc == 126:
        return LinuxSetupResult(
            ok=False, cancelled=True,
            message="Authorization dismissed — the udev rule + blocklist are still installed.")
    return LinuxSetupResult(ok=False, detail=rpath,
                            message=f"Couldn't remove the udev rule + blocklist (exit {rc}).")


def _dedupe(keys) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out
