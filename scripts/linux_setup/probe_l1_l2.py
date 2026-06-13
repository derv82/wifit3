"""Linux device-setup probe — measures non-root kernel-driver detach + per-module behaviour on real hardware.

Runs on the Kali box (Linux only). It is the *measurement* tool the design is gated on; the
rule generator + splash wiring are codeable on Windows, but these two facts can only be read
off the metal:

  L1  Does freeing the card from its kernel driver (USBDEVFS_DISCONNECT, i.e. PyUSB
      ``detach_kernel_driver``) need root, or just write-access to the usbfs node? If a
      permissive udev rule alone lets a *non-root* user detach, per-run sudo disappears
      entirely — the whole ballgame.
  L2  Which of our kernel modules detach cleanly via runtime PyUSB (lever 1) vs hold the
      netdev and need unbind-on-plug (lever 3). One row per card you plug in.

This is passive: detaching only unbinds the kernel net driver (what ``rmmod`` does, but scoped
to one device) — it never TXes 802.11. A replug re-attaches the kernel driver, so every action
here is free to undo.

────────────────────────────────────────────────────────────────────────────────────────────
Exact Kali run sheet (do these in order):

  # 0. Use the project venv so PyUSB + the live driver registry are importable.
  cd ~/path/to/wifit3

  # 1. Install the permissive udev rule ONCE (this is the single pkexec/UAC-style prompt).
  #    A graphical password dialog pops over your windows (polkit). Enter your password.
  .venv/bin/python3 scripts/linux_setup/probe_l1_l2.py --install-rule

  # 2. Unplug + replug the card so the rule (and its seat ACL) actually applies to the node.

  # 3. Run the probe AS YOUR NORMAL USER (NOT sudo — root makes the L1 result meaningless):
  .venv/bin/python3 scripts/linux_setup/probe_l1_l2.py

  # 4. Swap to the next card, replug, rerun step 3. Each run appends an L2 row.
  #    See the whole accumulated table any time:
  .venv/bin/python3 scripts/linux_setup/probe_l1_l2.py --show

  # New card with no driver yet? Identify its VID:PID + which kernel module grabs it:
  .venv/bin/python3 scripts/linux_setup/probe_l1_l2.py --list-all

  # When done, remove the probe rule (optional — it only touches our VID:PIDs):
  .venv/bin/python3 scripts/linux_setup/probe_l1_l2.py --remove-rule

Other knobs: ``--emit-udev`` prints the generated rule without installing; ``--perms loose``
writes a 0666 node rule to isolate the detach question from the open question (if uaccess
didn't grant you the node, a 0666 rule definitely will, so a remaining detach failure is purely
the CAP_SYS_ADMIN question); ``--use-sudo`` / ``--print-only`` swap the privileged-exec method;
``--vid/--pid`` force-tests one device (e.g. a new card not yet in the registry).
"""
from __future__ import annotations

import argparse
import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _is_root() -> bool:
    """True iff effective uid 0. os.geteuid is absent on Windows, so shim to non-root there."""
    return getattr(os, "geteuid", lambda: 1)() == 0

# ── PyUSB (from the project venv) ───────────────────────────────────────────────────────────
try:
    import usb.core
    import usb.util
except ImportError:
    sys.stderr.write(
        "PyUSB not importable. Run this with the project venv so pyusb is present:\n"
        "    .venv/bin/python3 scripts/linux_setup/probe_l1_l2.py ...\n")
    raise SystemExit(2)

# Numbered 60- (NOT 70-) so it sorts before systemd's 70-uaccess.rules: the uaccess builtin
# only grants the ACL for devices already TAG-ed "uaccess" when it runs, so our TAG+="uaccess"
# has to be set in an earlier-sorting file or the seat ACL is never applied.
RULE_PATH = "/etc/udev/rules.d/60-wifit3-probe.rules"
# Earlier releases shipped the rule at 70-; remove that too so a stale, mis-ordered copy can't
# linger and mask the 60- one.
LEGACY_RULE_PATHS = ("/etc/udev/rules.d/70-wifit3-probe.rules",)
DEFAULT_RESULTS = Path.cwd() / "wifit3-l1l2-results.tsv"

# Fallback VID:PID snapshot, captured from `ids_from_registry()` so it is faithful to the
# driver registry at write time. The live registry is preferred at runtime (see
# load_supported_ids); this only kicks in if importing wifit3 fails on a half-set-up box.
# Not the shipping source of truth — the real udev emitter reads ids_from_registry() directly.
_FALLBACK_IDS: list[tuple[int, int, str]] = [
    # ath9k_htc (AR9271)
    (0x0CF3, 0x9271, "Atheros AR9271 / ALFA AWUS036NHA"),
    # rtl8187 (RTL8187L)
    (0x0BDA, 0x8187, "Realtek RTL8187L / ALFA AWUS036H"),
    (0x0B05, 0x1706, "ASUS"), (0x0B05, 0x1707, "ASUS"),
    (0x050D, 0x7050, "Belkin F5D7050A v2.x"), (0x050D, 0x7051, "Belkin"),
    (0x13B1, 0x000D, "Cisco/Linksys"), (0x13B1, 0x0011, "Cisco/Linksys"),
    (0x13B1, 0x001A, "Cisco/Linksys"), (0x14B2, 0x3C02, "Conceptronic"),
    (0x2001, 0x3C00, "D-Link"), (0x1044, 0x8001, "Gigabyte"), (0x1044, 0x8007, "Gigabyte"),
    (0x06F8, 0xE000, "Hercules"), (0x0411, 0x005E, "Melco/Buffalo"),
    (0x0411, 0x0066, "Melco/Buffalo"), (0x0411, 0x0067, "Melco/Buffalo"),
    (0x0411, 0x008B, "Buffalo Nintendo Wi-Fi USB Connector"), (0x0411, 0x0097, "Melco/Buffalo"),
    (0x0DB0, 0x6861, "MSI"), (0x0DB0, 0x6865, "MSI"), (0x0DB0, 0x6869, "MSI"),
    (0x148F, 0x1706, "Ralink"), (0x148F, 0x2570, "Ralink"), (0x148F, 0x9020, "Ralink"),
    (0x079B, 0x004B, "Sagem"), (0x0681, 0x3C06, "Siemens"), (0x0707, 0xEE13, "SMC"),
    (0x114B, 0x0110, "Spairon"), (0x0769, 0x11F3, "SureCom"), (0x0EB0, 0x9020, "Trust"),
    (0x0F88, 0x3012, "VTech"), (0x5A57, 0x0260, "Zinwell"),
    # rt2800usb (RT5372 / RT3572 / RT5572) + rt3070 placeholder
    (0x148F, 0x5372, "Ralink RT5372 / Panda PAU05"),
    (0x148F, 0x3572, "Ralink RT3572 / ALFA AWUS051NH v2"),
    (0x148F, 0x5572, "Ralink RT5572 / Panda PAU09 N600"),
    (0x148F, 0x3070, "Ralink RT3070 / ALFA AWUS036NH"),
    # rtl8188eus (TL-WN722N v2/v3)
    (0x2357, 0x010C, "Realtek RTL8188EUS / TP-Link TL-WN722N v2/v3"),
    # rtl88xxau DKMS ports
    (0x0BDA, 0x8812, "Realtek RTL8812AU 2T2R (ALFA AWUS036ACH)"),
    (0x0BDA, 0x0811, "Realtek RTL8821AU/RTL8811AU 1T1R (ALFA AWUS036ACS)"),
    # rtl8822bu
    (0x2357, 0x0138, "TP-Link Archer T3U Plus (RTL8822BU)"),
    (0x2357, 0x012D, "TP-Link Archer T3U v1 (RTL8822BU)"),
    (0x2357, 0x0115, "TP-Link Archer T4U V3 (RTL8822BU)"),
    (0x2357, 0x012E, "TP-Link RTL8822BU"), (0x2357, 0x0116, "TP-Link RTL8822BU"),
    (0x2357, 0x0117, "TP-Link RTL8822BU"), (0x0BDA, 0xB812, "Realtek RTL8822BU"),
    (0x0BDA, 0xB82C, "Realtek RTL8822BU"), (0x0BDA, 0xB81A, "Realtek RTL8822BU (default)"),
    (0x0B05, 0x1841, "ASUS USB-AC55 B1 (RTL8822BU)"), (0x0B05, 0x184C, "ASUS U2 (RTL8822BU)"),
    (0x0B05, 0x19AA, "ASUS USB-AC58 rev A1 (RTL8822BU)"),
    (0x2001, 0x331E, "D-Link DWA-181 (RTL8822BU)"), (0x2001, 0x331C, "D-Link DWA-182 D1 (RTL8822BU)"),
    (0x13B1, 0x0043, "Linksys WUSB6400M (RTL8822BU)"), (0x13B1, 0x0045, "Linksys WUSB3600 v2 (RTL8822BU)"),
    (0x0846, 0x9055, "Netgear A6150 (RTL8822BU)"), (0x7392, 0xB822, "Edimax EW-7822ULC (RTL8822BU)"),
    (0x7392, 0xC822, "Edimax EW-7822UTC (RTL8822BU)"), (0x7392, 0xD822, "Edimax (RTL8822BU)"),
    (0x7392, 0xE822, "Edimax (RTL8822BU)"), (0x7392, 0xF822, "Edimax EW-7822UAD (RTL8822BU)"),
    (0x2C4E, 0x0107, "Mercusys MA30H (RTL8822BU)"), (0x2C4E, 0x010A, "Mercusys MA30N (RTL8822BU)"),
    (0x0411, 0x03D1, "BUFFALO WI-U2-866DM (RTL8822BU)"), (0x0411, 0x03D0, "BUFFALO WI-U3-866DHP (RTL8822BU)"),
    # rtl8814au
    (0x0BDA, 0x8813, "Realtek RTL8814AU 4T4R (ALFA AWUS1900)"),
    (0x056E, 0x400B, "Elecom WDC-1300SU2 (RTL8814AU)"), (0x056E, 0x400D, "Elecom (RTL8814AU)"),
    (0x0846, 0x9054, "Netgear A7000 (RTL8814AU)"), (0x0B05, 0x1817, "ASUS USB-AC68 (RTL8814AU)"),
    (0x0B05, 0x1852, "ASUS (RTL8814AU)"), (0x0B05, 0x1853, "ASUS USB-AC68 (RTL8814AU)"),
    (0x0E66, 0x0026, "Hawking HW12ACU (RTL8814AU)"), (0x2001, 0x331A, "D-Link DWA-192 (RTL8814AU)"),
    (0x20F4, 0x809A, "TRENDnet TEW-809UB (RTL8814AU)"), (0x20F4, 0x809B, "TRENDnet (RTL8814AU)"),
    (0x2357, 0x0106, "TP-Link Archer T9UH (RTL8814AU)"), (0x7392, 0xA834, "Edimax EW-7833UAC (RTL8814AU)"),
    (0x7392, 0xA833, "Edimax EW-7833 (RTL8814AU)"),
    # mt76x0u (MT7610U)
    (0x148F, 0x7610, "MediaTek MT7610U reference"), (0x13B1, 0x003E, "Linksys AE6000"),
    (0x0E8D, 0x7610, "MediaTek MT7610U (Alfa AWUS036ACM/ACHM, Sabrent NTWLAC, etc.)"),
    (0x7392, 0xA711, "Edimax 7711mac"), (0x7392, 0xB711, "Edimax / Elecom"),
    (0x148F, 0x761A, "TP-Link TL-WDN5200"), (0x148F, 0x760A, "TP-Link (unknown)"),
    (0x0B05, 0x17D1, "Asus USB-AC51"), (0x0B05, 0x17DB, "Asus USB-AC50"),
    (0x0DF6, 0x0075, "Sitecom WLA-3100"), (0x2019, 0xAB31, "Planex GW-450D"),
    (0x2001, 0x3D02, "D-Link DWA-171 rev B1"), (0x0586, 0x3425, "Zyxel NWD6505"),
    (0x07B8, 0x7610, "AboCom AU7212"), (0x04BB, 0x0951, "I-O DATA WN-AC433UK"),
    (0x057C, 0x8502, "AVM FRITZ!WLAN USB Stick AC 430"), (0x293C, 0x5702, "Comcast Xfinity KXW02AAA"),
    (0x20F4, 0x806B, "TRENDnet TEW-806UBH"), (0x7392, 0xC711, "Devolo Wifi ac Stick"),
    (0x0DF6, 0x0079, "Sitecom Europe ac Stick"), (0x2357, 0x0123, "TP-Link T2UHP_US_v1"),
    (0x2357, 0x010B, "TP-Link T2UHP_UN_v1"), (0x2357, 0x0105, "TP-Link Archer T1U"),
    (0x0E8D, 0x7630, "MediaTek MT7630U"), (0x0E8D, 0x7650, "MediaTek MT7650U"),
    # mt76x2u (MT7612U / MT7662U)
    (0x0B05, 0x1833, "Asus USB-AC54 (MT7612U)"), (0x0B05, 0x17EB, "Asus USB-AC55 (MT7612U)"),
    (0x0B05, 0x180B, "Asus USB-N53 B1 (MT7612U)"),
    (0x0E8D, 0x7612, "Alfa AWUS036ACM / Aukey USBAC1200 (MT7612U)"),
    (0x057C, 0x8503, "AVM FRITZ!WLAN AC860 (MT7612U)"), (0x0E8D, 0x7632, "HC-M7662BU1 (MT7662U)"),
    (0x0471, 0x2126, "LiteOn WN4516R (MT7612U)"), (0x0471, 0x7600, "LiteOn WN4519R (MT7612U)"),
    (0x2C4E, 0x0103, "Mercury UD13 (MT7612U)"), (0x0846, 0x9014, "Netgear WNDA3100v3 (MT7612U)"),
    (0x0846, 0x9053, "Netgear A6210 (MT7612U)"), (0x045E, 0x02E6, "Xbox One Wireless Adapter (MT7612U)"),
    (0x045E, 0x02FE, "Xbox One Wireless Adapter (MT7612U)"), (0x2357, 0x0137, "TP-Link TL-WDN6200 (MT7612U)"),
    # mt7921au
    (0x0E8D, 0x7961, "Mediatek MT7921AU / ALFA AWUS036AXML"),
]


# ── Supported-ID source ─────────────────────────────────────────────────────────────────────
def load_supported_ids() -> tuple[list[tuple[int, int, str]], str]:
    """Every supported (vid, pid, description), preferring the live driver registry.

    The live registry is the single source of truth; we only fall back to the embedded
    snapshot if importing wifit3 fails (a half-set-up box, an import error in one driver),
    so the probe still runs. Returns the list plus a human label for which source won.
    """
    src_dir = Path(__file__).resolve().parent.parent.parent / "src"
    if src_dir.is_dir():
        sys.path.insert(0, str(src_dir))
    try:
        from wifit3.setup import ids_from_registry
        ids = [(e.vid, e.pid, e.description) for e in ids_from_registry()]
        return ids, f"live registry ({len(ids)} ids)"
    except Exception as e:  # noqa: BLE001 — any import failure should degrade, not crash
        return _FALLBACK_IDS, f"embedded snapshot ({len(_FALLBACK_IDS)} ids; live import failed: {e})"


def get_backend():
    """libusb backend, preferring the bundled libusb_package; else the system libusb."""
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:  # noqa: BLE001
        return None  # PyUSB then discovers the system libusb.so itself


# ── udev rule ───────────────────────────────────────────────────────────────────────────────
# Each lever maps to a perms clause on the *usbfs device node* (/dev/bus/usb/BBB/DDD):
#   uaccess  TAG+="uaccess"      logind grants a dynamic ACL to the active-seat user (revoked
#                                on logout). The shipping default — tightest scope.
#   plugdev  MODE 0660 + plugdev RW for root + the plugdev group. Pre-systemd / headless fallback.
#   loose    MODE 0666          world RW. INSECURE — probe-only, to isolate "can I open" from
#                                "can I detach": with 0666 the open always succeeds, so a
#                                remaining detach failure is purely the root requirement.
#   all      uaccess + plugdev  belt-and-suspenders; maximises the chance you get the node so
#                                the detach test is actually reachable. Probe default.
_PERMS_CLAUSE = {
    "uaccess": 'TAG+="uaccess"',
    "plugdev": 'MODE="0660", GROUP="plugdev"',
    "loose":   'MODE="0666"',
    "all":     'TAG+="uaccess", MODE="0660", GROUP="plugdev"',
}


def build_rule_text(ids: list[tuple[int, int, str]], perms: str, source: str) -> str:
    """A udev rules file granting node access on every supported VID:PID.

    Matches the usb_device itself (ATTR{idVendor}/idProduct, lowercase hex, no 0x) — the
    canonical "let libusb open this device" shape. One deduped line per VID:PID.

    The per-card description goes on its OWN comment line above each rule, never as a trailing
    `# ...` on the rule line: modern udev only accepts `#` at the start of a line and rejects
    the WHOLE rule ("a comma between tokens is expected") if a comment trails it.
    """
    clause = _PERMS_CLAUSE[perms]
    lines = [
        "# wifit3 Linux device-setup PROBE rule (generated - not the shipping file).",
        f"# Source: {source}. Perms lever: {perms} -> {clause}",
        "# Grants the local user RW on these cards' usbfs node so wifit3 can open AND",
        "# detach the kernel driver without root (the non-root-detach measurement).",
        "# Remove with: probe_l1_l2.py --remove-rule",
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


# ── privileged exec ─────────────────────────────────────────────────────────────────────────
def run_privileged(shell_cmd: str, method: str) -> int:
    """Run ``shell_cmd`` (a /bin/sh -c string) as root via pkexec or sudo.

    pkexec is the default: on a desktop session a polkit agent pops a graphical password
    dialog (the Linux UAC analog). Returns the child exit code; pkexec uses 126 (dismissed /
    not authorized) and 127 (not in policy / not found), which we surface plainly.
    """
    sh = shutil.which("sh") or "/bin/sh"
    if method == "sudo":
        runner = shutil.which("sudo")
        if not runner:
            print("[!] sudo not found.")
            return 127
        argv = [runner, sh, "-c", shell_cmd]
    else:
        runner = shutil.which("pkexec")
        if not runner:
            print("[!] pkexec not found — fall back to: --use-sudo (or --print-only).")
            return 127
        argv = [runner, sh, "-c", shell_cmd]
    print(f"[*] elevating via {Path(runner).name}: sh -c {shell_cmd!r}")
    try:
        return subprocess.call(argv)
    except KeyboardInterrupt:
        return 130


def _install_shell(tmp: str) -> str:
    legacy_rm = "".join(f"rm -f {p} && " for p in LEGACY_RULE_PATHS)
    return (f"{legacy_rm}install -m 0644 {tmp} {RULE_PATH} && "
            "udevadm control --reload-rules && "
            "udevadm trigger --action=add --subsystem-match=usb")


def _remove_shell() -> str:
    paths = " ".join((RULE_PATH, *LEGACY_RULE_PATHS))
    return f"rm -f {paths} && udevadm control --reload-rules"


def cmd_emit_udev(args) -> int:
    ids, source = load_supported_ids()
    sys.stdout.write(build_rule_text(ids, args.perms, source))
    return 0


def cmd_install_rule(args) -> int:
    ids, source = load_supported_ids()
    text = build_rule_text(ids, args.perms, source)
    print(f"[*] {source}; perms lever '{args.perms}' -> "
          f"{len({(v, p) for v, p, _ in ids})} VID:PIDs")
    tmp = str(Path(tempfile.gettempdir()) / "wifit3-probe.rules")

    if args.print_only:  # just show the privileged command — works from any box.
        if sys.platform == "linux":
            Path(tmp).write_text(text)
            print(f"[*] staged the rule at {tmp}. Run this yourself:")
        else:
            print(f"[i] preview from {sys.platform} (run on Linux; see --emit-udev for the rule):")
        print(f"    sudo sh -c '{_install_shell(tmp)}'")
        return 0

    if sys.platform != "linux":
        print(f"[!] Rule install is Linux-only (you're on {sys.platform}). "
              "Use --emit-udev to preview or --print-only for the command.")
        return 2

    if _is_root():  # whole script under sudo — write directly, no second prompt.
        for legacy in LEGACY_RULE_PATHS:  # drop any stale, mis-ordered copy so it can't mask us
            Path(legacy).unlink(missing_ok=True)
        Path(RULE_PATH).write_text(text)
        os.chmod(RULE_PATH, 0o644)
        subprocess.call(["udevadm", "control", "--reload-rules"])
        subprocess.call(["udevadm", "trigger", "--action=add", "--subsystem-match=usb"])
        print(f"[OK] wrote {RULE_PATH} and reloaded udev.")
        return _post_install_hint()

    Path(tmp).write_text(text)
    rc = run_privileged(_install_shell(tmp), "sudo" if args.use_sudo else "pkexec")
    if rc == 0:
        print(f"[OK] installed {RULE_PATH} and reloaded udev.")
        return _post_install_hint()
    if rc == 126:
        print("[!] Authorization dismissed/denied — rule NOT installed.")
    else:
        print(f"[!] Privileged install failed (exit {rc}).")
    return 1


def _post_install_hint() -> int:
    print("\n[next] Unplug + replug the card so the rule + seat ACL apply to the node,")
    print("       then run the probe AS YOUR NORMAL USER (no sudo):")
    print("           .venv/bin/python3 scripts/linux_setup/probe_l1_l2.py")
    return 0


def cmd_remove_rule(args) -> int:
    if args.print_only:
        print(f"[*] run this yourself: sudo sh -c '{_remove_shell()}'")
        return 0
    if sys.platform != "linux":
        print(f"[!] Rule removal is Linux-only (you're on {sys.platform}).")
        return 2
    if _is_root():
        try:
            os.remove(RULE_PATH)
        except FileNotFoundError:
            pass
        subprocess.call(["udevadm", "control", "--reload-rules"])
        print(f"[OK] removed {RULE_PATH}.")
        return 0
    rc = run_privileged(_remove_shell(), "sudo" if args.use_sudo else "pkexec")
    print(f"[OK] removed {RULE_PATH}." if rc == 0 else f"[!] removal failed (exit {rc}).")
    return 0 if rc == 0 else 1


# ── sysfs (kernel-driver discovery) ─────────────────────────────────────────────────────────
_SYSFS_USB = Path("/sys/bus/usb/devices")


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def find_sysfs_device(vid: int, pid: int, bus: int, address: int) -> Path | None:
    """The /sys/bus/usb/devices/<N-...> dir for this device (usb_device nodes have no ':')."""
    if not _SYSFS_USB.is_dir():
        return None
    for entry in _SYSFS_USB.iterdir():
        if ":" in entry.name:
            continue  # that's an interface, not the device
        try:
            if (int(_read(entry / "idVendor") or "-1", 16) == vid
                    and int(_read(entry / "idProduct") or "-1", 16) == pid
                    and int(_read(entry / "busnum") or "-1") == bus
                    and int(_read(entry / "devnum") or "-1") == address):
                return entry
        except ValueError:
            continue
    return None


def interface_drivers(devdir: Path) -> list[tuple[str, str]]:
    """[(interface name, bound kernel driver or '<none>')] for each interface of the device."""
    out: list[tuple[str, str]] = []
    prefix = devdir.name + ":"
    for entry in sorted(devdir.parent.iterdir()):
        if not entry.name.startswith(prefix):
            continue
        link = entry / "driver"
        drv = os.path.basename(os.readlink(link)) if link.is_symlink() else "<none>"
        out.append((entry.name, drv))
    return out


def usbfs_node(bus: int, address: int) -> Path:
    return Path(f"/dev/bus/usb/{bus:03d}/{address:03d}")


def node_perms(node: Path) -> str:
    try:
        st = node.stat()
    except OSError as e:
        return f"<stat failed: {e}>"
    import grp
    import pwd
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    writable = "writable" if os.access(node, os.W_OK) else "NOT writable"
    return f"{stat.filemode(st.st_mode)} {owner}:{group} ({writable} by uid {os.geteuid()})"


# ── the L1/L2 probe ─────────────────────────────────────────────────────────────────────────
def probe_device(dev, vid: int, pid: int, desc: str, results_path: Path) -> None:
    """Open the card, try a non-root kernel detach per interface, and record the L1/L2 result."""
    am_root = _is_root()
    bus, address = dev.bus, dev.address
    node = usbfs_node(bus, address)
    # Node write-access is the clean discriminator: libusb_open needs the node RW, so a
    # non-writable node fails at *open* (rule not applied / not in plugdev / no active seat) —
    # a different conclusion from "opened fine but the detach ioctl wants root."
    node_writable = os.access(node, os.W_OK)
    devdir = find_sysfs_device(vid, pid, bus, address)
    drivers = interface_drivers(devdir) if devdir else []
    primary_driver = next((d for _, d in drivers if d not in ("<none>", "usbfs")), "<none>")

    print(f"\n{'═' * 92}")
    print(f"  {desc}  [{vid:04x}:{pid:04x}]   bus {bus} addr {address}")
    print(f"  usbfs node : {node}")
    print(f"  node perms : {node_perms(node)}")
    print(f"  sysfs intf : {', '.join(f'{n}->{d}' for n, d in drivers) or '<none found>'}")
    print(f"  kernel drv : {primary_driver}")
    if am_root:
        print("  [!!] RUNNING AS ROOT — the L1 result below is MEANINGLESS (root can always")
        print("       detach). Re-run as your normal user once the udev rule is installed.")

    # Per-interface detach attempt. is_kernel_driver_active / detach both ioctl the usbfs node,
    # so a permission failure here (EACCES on open, EPERM on the ioctl) is the L1 'needs root'
    # signal; success as a non-root user is L1 holding.
    per_intf: list[str] = []
    detach_errnos: list[int] = []
    any_active = False
    open_failed: str | None = None
    try:
        cfg = dev.get_active_configuration()
        intf_nums = sorted({i.bInterfaceNumber for i in cfg})
    except usb.core.USBError as e:
        open_failed = f"{e.strerror or e} (errno {e.errno})"
        intf_nums = []

    for i in intf_nums:
        try:
            active = dev.is_kernel_driver_active(i)
        except usb.core.USBError as e:
            per_intf.append(f"if{i}: GETDRIVER failed ({e.strerror}, errno {e.errno})")
            if e.errno in (errno.EACCES, errno.EPERM):
                detach_errnos.append(e.errno)
            continue
        if not active:
            per_intf.append(f"if{i}: no kernel driver")
            continue
        any_active = True
        try:
            dev.detach_kernel_driver(i)
            per_intf.append(f"if{i}: DETACHED ok")
        except usb.core.USBError as e:
            per_intf.append(f"if{i}: detach FAILED ({e.strerror}, errno {e.errno})")
            detach_errnos.append(e.errno or 0)

    print(f"  detach     : {' | '.join(per_intf) or '(no interfaces enumerated)'}")

    # Bonus L1.5: after a successful non-root detach, can we actually claim + talk to it?
    claim = "not attempted"
    if not open_failed and any_active and not detach_errnos:
        try:
            usb.util.claim_interface(dev, intf_nums[0])
            claim = f"claimed if{intf_nums[0]} ok — device is fully usable userland"
            usb.util.release_interface(dev, intf_nums[0])
        except usb.core.USBError as e:
            claim = f"claim failed ({e.strerror}, errno {e.errno})"
    print(f"  claim test : {claim}")

    # Verdict. Order matters: rule down "can't even open" before judging the detach, since a
    # non-writable node makes every downstream errno just a symptom of that.
    needs_root = any(e in (errno.EACCES, errno.EPERM) for e in detach_errnos)
    if am_root:
        verdict = "N/A (root) — rerun as your user"
    elif not node_writable:
        verdict = (f"INCONCLUSIVE — node not writable by uid {os.geteuid()} (rule not applied, "
                   "not in plugdev, or no active seat). Replug, or try --perms loose/plugdev.")
    elif open_failed:
        verdict = (f"INCONCLUSIVE — couldn't open node ({open_failed}). Replug, or --perms loose.")
    elif needs_root:
        verdict = "L1 FAILS — detach needs root (EPERM/EACCES). This module needs lever 3 (unbind-on-plug)."
    elif not any_active:
        verdict = "no kernel driver was bound (already free / no in-kernel driver for this card)"
    elif detach_errnos:
        verdict = f"detach failed for a non-permission reason (errno {detach_errnos})"
    else:
        verdict = "L1 HOLDS — non-root detach succeeded. No per-run sudo needed for this module."
    print(f"  ▶ VERDICT  : {verdict}")
    print(f"  (replug to re-attach {primary_driver} and undo this.)")

    usb.util.dispose_resources(dev)
    _append_result(results_path, vid, pid, desc, primary_driver, am_root,
                   open_failed, per_intf, verdict)


def _append_result(path: Path, vid, pid, desc, driver, am_root, open_failed, per_intf, verdict):
    new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if new:
            f.write("timestamp\tvid:pid\tdriver\tas_root\tdetach\tverdict\tdescription\n")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        detach = "; ".join(per_intf) if not open_failed else f"OPEN FAILED: {open_failed}"
        f.write(f"{ts}\t{vid:04x}:{pid:04x}\t{driver}\t{int(am_root)}\t{detach}\t{verdict}\t{desc}\n")
    print(f"  (appended L2 row -> {path})")


def cmd_probe(args) -> int:
    if sys.platform != "linux":
        print(f"[!] This probe is Linux-only (you're on {sys.platform}). Run it on the Kali box.")
        return 2
    backend = get_backend()
    ids, source = load_supported_ids()
    results_path = Path(args.out)
    print(f"[*] L1/L2 device-setup probe — {source}")
    print(f"[*] euid={os.geteuid()} ({'ROOT — L1 will be N/A' if os.geteuid() == 0 else 'normal user — good'})")
    print(f"[*] results file: {results_path}")

    if args.vid is not None and args.pid is not None:
        targets = [(args.vid, args.pid, "forced --vid/--pid target (e.g. new card)")]
    else:
        present = {(d.idVendor, d.idProduct) for d in usb.core.find(find_all=True, backend=backend)}
        targets = [(v, p, desc) for (v, p, desc) in ids if (v, p) in present]

    if not targets:
        print("\n[!] No supported card found on the bus. Plug one in (and replug after installing")
        print("    the rule). To identify an unknown/new card and its kernel driver: --list-all")
        return 1

    for vid, pid, desc in targets:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is None:
            continue
        probe_device(dev, vid, pid, desc, results_path)

    print(f"\n[done] See the accumulated L2 table: "
          f".venv/bin/python3 {Path(__file__).name} --show  (file: {results_path})")
    return 0


def cmd_list_all(args) -> int:
    """Every USB device + the kernel driver bound to each interface — finds a new card's IDs."""
    if sys.platform != "linux":
        print(f"[!] Linux-only (you're on {sys.platform}).")
        return 2
    backend = get_backend()
    ids = {(v, p) for v, p, _ in load_supported_ids()[0]}
    print("[*] All USB devices (★ = already in the wifit3 registry):\n")
    for dev in usb.core.find(find_all=True, backend=backend):
        vid, pid = dev.idVendor, dev.idProduct
        star = "★" if (vid, pid) in ids else " "
        try:
            product = usb.util.get_string(dev, dev.iProduct) or ""
            vendor = usb.util.get_string(dev, dev.iManufacturer) or ""
        except Exception:  # noqa: BLE001 — string reads need access we may not have
            product = vendor = "<unreadable>"
        devdir = find_sysfs_device(vid, pid, dev.bus, dev.address)
        drivers = ", ".join(f"{n}->{d}" for n, d in interface_drivers(devdir)) if devdir else "?"
        print(f"  {star} {vid:04x}:{pid:04x}  cls=0x{dev.bDeviceClass:02x}  "
              f"{vendor} {product}".rstrip())
        print(f"       drivers: {drivers}")
    print("\n[hint] A wireless card usually shows class 0x00/0xff with a driver like rtl8xxxu,")
    print("       mt76x2u, rt2800usb, ath9k_htc, etc. Note its VID:PID for the new bring-up.")
    return 0


def cmd_show(args) -> int:
    path = Path(args.out)
    if not path.exists():
        print(f"[!] No results yet at {path}. Run the probe first.")
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Linux device-setup probe — measure non-root detach + per-module behaviour.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--install-rule", action="store_true",
                   help="install the permissive udev rule (one pkexec/UAC-style prompt)")
    p.add_argument("--remove-rule", action="store_true", help="remove the probe udev rule")
    p.add_argument("--emit-udev", action="store_true", help="print the generated rule, don't install")
    p.add_argument("--list-all", action="store_true",
                   help="dump every USB device + bound kernel driver (identify a new card)")
    p.add_argument("--show", action="store_true", help="print the accumulated L2 results table")
    p.add_argument("--perms", choices=list(_PERMS_CLAUSE), default="all",
                   help="udev perms lever (default: all = uaccess+plugdev; 'loose' = 0666 to isolate)")
    p.add_argument("--use-sudo", action="store_true", help="elevate with sudo instead of pkexec")
    p.add_argument("--print-only", action="store_true", help="print the privileged command, don't run it")
    p.add_argument("--vid", type=lambda x: int(x, 0), help="force-test this VID (e.g. 0x0bda)")
    p.add_argument("--pid", type=lambda x: int(x, 0), help="force-test this PID")
    p.add_argument("--out", default=str(DEFAULT_RESULTS), help=f"results file (default: {DEFAULT_RESULTS})")
    args = p.parse_args()

    if args.emit_udev:
        return cmd_emit_udev(args)
    if args.install_rule:
        return cmd_install_rule(args)
    if args.remove_rule:
        return cmd_remove_rule(args)
    if args.list_all:
        return cmd_list_all(args)
    if args.show:
        return cmd_show(args)
    return cmd_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
