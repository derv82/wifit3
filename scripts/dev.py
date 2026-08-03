"""Shared dev-tooling helpers, importable by any script under scripts/.

Currently: interface selection. Pick one plugged-in card by a name substring or an exact bus:addr,
so several cards can be connected at once and each script still targets the right one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _emit(log, msg: str) -> None:
    (log or (lambda m: print(m, file=sys.stderr)))(msg)


def _parse_instance(instance: str) -> tuple[int, int] | None:
    """Parse "BUS:ADDR" (decimal, as pyusb reports dev.bus/dev.address) to (bus, addr)."""
    try:
        bus, _, addr = instance.partition(":")
        return int(bus), int(addr)
    except ValueError:
        return None


def select_device(ifaces, card: str = "", *, instance: str = "", log=None):
    """Select one wifit3 (USB) interface: exact ``instance`` "BUS:ADDR" wins (the only way to tell two
    identical VID:PIDs apart), else a ``card`` name/description substring, else ifaces[0]; None
    (roster printed) if a non-blank instance/card matches nothing."""
    if instance:
        want = _parse_instance(instance)
        matches = [i for i in ifaces if (i.bus, i.address) == want] if want else []
        if not matches:
            roster = ", ".join(f"{i.bus}:{i.address} ({i.description})" for i in ifaces)
            _emit(log, f"[-] no card at instance '{instance}'. Found: {roster}")
            return None
        return matches[0]
    if not card:
        if len(ifaces) > 1:
            _emit(log, f"[!] {len(ifaces)} interfaces; using {ifaces[0].name}. "
                       f"Pass --card <substr> to pick another.")
        return ifaces[0] if ifaces else None
    matches = [i for i in ifaces if card.lower() in f"{i.name} {i.description}".lower()]
    if not matches:
        roster = ", ".join(f"{i.name} ({i.description})" for i in ifaces)
        _emit(log, f"[-] no card matches '{card}'. Found: {roster}")
        return None
    if len(matches) > 1:
        _emit(log, f"[!] '{card}' matched {len(matches)}; using {matches[0].name}.")
    return matches[0]


def list_wireless_ifaces() -> list[tuple[str, str | None]]:
    """[(ifname, kernel_driver_or_None)] for every 802.11 netdev (has phy80211/)."""
    base = Path("/sys/class/net")
    out: list[tuple[str, str | None]] = []
    if not base.exists():
        return out
    for p in sorted(base.iterdir()):
        if not (p / "phy80211").exists():
            continue
        drv = None
        link = p / "device" / "driver"
        try:
            if link.exists():
                drv = os.path.basename(os.readlink(link))
        except OSError:
            pass
        out.append((p.name, drv))
    return out


def pick_kernel_iface(iface: str = "", card: str = "", *, log=None):
    """Pick one Linux 802.11 netdev (the kernel-driver side): explicit ``iface`` wins, else a ``card``
    substring of "<ifname> <driver>", else the sole wireless iface; None (roster printed) on a miss
    or an ambiguous choice."""
    if iface:
        return iface
    ifaces = list_wireless_ifaces()
    if card:
        matches = [n for n, d in ifaces if card.lower() in f"{n} {d or ''}".lower()]
        if not matches:
            _emit(log, f"[-] no wireless iface matches '{card}'. Found: "
                       f"{', '.join(f'{n}({d})' for n, d in ifaces) or 'none'}")
            return None
        return matches[0]
    if len(ifaces) == 1:
        return ifaces[0][0]
    if not ifaces:
        _emit(log, "[-] no wireless interfaces found (is the card bound to a kernel driver?)")
        return None
    _emit(log, f"[!] {len(ifaces)} wireless ifaces: "
               f"{', '.join(f'{n}({d})' for n, d in ifaces)}. Pass --iface or --card.")
    return None
