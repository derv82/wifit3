#!/usr/bin/env python3
"""Un-wedge a PCIe/M.2 Wi-Fi card whose monitor-mode RX has stalled.

mt7921e (and similar) can stop delivering frames after repeated monitor<->managed
flips; the kernel-driver equivalent of the USB "replug" is a module reload. This
reloads the module bound to each wifit3 netdev card, then hands the interfaces back
to NetworkManager.

    uv run python scripts/recover_netdev.py            # all wifit3 PCIe cards
    uv run python scripts/recover_netdev.py wlp62s0     # one interface

Needs root; it escalates via sudo/doas/pkexec like the rest of wifit3's Linux setup.
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from wifit3.device.manager import devices
from wifit3.setup.linux import _choose_escalation, run_privileged


def _netdev_ifaces() -> list[str]:
    return [d.extras["iface"] for d in devices()
            if d.extras.get("bus_type") == "pci" and d.extras.get("iface")]


def _bound_module(iface: str) -> str | None:
    link = Path(f"/sys/class/net/{iface}/device/driver")
    try:
        return link.resolve().name
    except OSError:
        return None


def _run(cmd: str) -> int:
    # Unprivileged first (already root, e.g. under doas); escalate only if that fails.
    if subprocess.run(["sh", "-c", cmd]).returncode == 0:
        return 0
    esc = _choose_escalation()
    if esc is None:
        return 1
    return run_privileged(cmd, esc)


def recover(ifaces: list[str]) -> bool:
    modules = {m for i in ifaces if (m := _bound_module(i))}
    if not modules:
        print(f"no kernel module found for {ifaces}", file=sys.stderr)
        return False

    reload_cmd = " && ".join(f"modprobe -r {m} && modprobe {m}" for m in sorted(modules))
    print(f"reloading: {', '.join(sorted(modules))}")
    if _run(reload_cmd) != 0:
        print("module reload failed", file=sys.stderr)
        return False

    time.sleep(3)  # let the driver re-enumerate the PCI device and recreate the netdev
    if shutil.which("nmcli"):
        _run(" && ".join(f"nmcli dev set {i} managed yes" for i in ifaces))

    present = [i for i in ifaces if Path(f"/sys/class/net/{i}").exists()]
    ok = len(present) == len(ifaces)
    print(f"interfaces back: {present}" + ("" if ok else f"  MISSING: {set(ifaces) - set(present)}"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Reload the kernel module to un-wedge a PCIe Wi-Fi card.")
    ap.add_argument("iface", nargs="*", help="interface(s) to recover; default: all wifit3 PCIe cards")
    args = ap.parse_args()
    ifaces = args.iface or _netdev_ifaces()
    if not ifaces:
        print("no PCIe/M.2 Wi-Fi cards found", file=sys.stderr)
        return 1
    return 0 if recover(ifaces) else 1


if __name__ == "__main__":
    raise SystemExit(main())
