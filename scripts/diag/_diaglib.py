"""Shared helpers for the diag CLIs (baseline-wifit3 / baseline-linux / beacon_watch / sweep).

Two jobs the sweep asked every diag script to share:
  * ``--card <substr>``: pick ONE adapter by a case-insensitive substring of its name/description,
    so several cards can be plugged in at once and each script still targets the right one.
    ``pick_interface`` is the wifit3-driver (USB) side; ``pick_kernel_iface`` the Linux-netdev side.
  * reference APs: the fixed 2.4 / 5 GHz beacon sources the A/B pins its beacon-rate line to.
    BSSIDs never enter git, so they load from ``driver_sources/reference_aps.txt`` (gitignored) with
    ``--bssid2g/--channel2g/--bssid5g/--channel5g`` overriding (same flag names as capture.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent.parent / "driver_sources"
_REF_FILE = _DATA / "reference_aps.txt"


def _emit(log, msg: str) -> None:
    (log or (lambda m: print(m, file=sys.stderr)))(msg)


# ---- USB (wifit3-driver) interface selection -------------------------------
def _parse_instance(instance: str) -> tuple[int, int] | None:
    """``"BUS:ADDR"`` (decimal, as pyusb reports dev.bus/dev.address) to ``(bus, addr)``, or None
    if malformed."""
    try:
        bus, _, addr = instance.partition(":")
        return int(bus), int(addr)
    except ValueError:
        return None


def pick_interface(ifaces, card: str = "", *, instance: str = "", log=None):
    """The interface to bring up. An explicit ``instance`` (``"BUS:ADDR"``) wins: it names one exact
    physical card, the only way to tell two identical VID:PIDs apart (soak_all launches one subprocess
    per card this way). Else the interface whose ``"<name> <description>"`` contains ``card``
    (case-insensitive), or ``ifaces[0]`` when both are blank. Returns None (after printing the roster)
    if a non-blank ``instance``/``card`` matches nothing."""
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
        return ifaces[0]
    matches = [i for i in ifaces if card.lower() in f"{i.name} {i.description}".lower()]
    if not matches:
        roster = ", ".join(f"{i.name} ({i.description})" for i in ifaces)
        _emit(log, f"[-] no card matches '{card}'. Found: {roster}")
        return None
    if len(matches) > 1:
        _emit(log, f"[!] '{card}' matched {len(matches)}; using {matches[0].name}.")
    return matches[0]


# ---- Linux netdev selection (kernel-driver side, baseline-linux) ------------
def list_wireless_ifaces() -> list[tuple[str, str | None]]:
    """``[(ifname, kernel_driver_or_None)]`` for every 802.11 netdev (has ``phy80211/``)."""
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
    """Resolve the base netdev for baseline-linux: an explicit ``--iface`` wins; else a
    ``--card`` substring over ``"<ifname> <driver>"``; else the sole wireless iface. Returns
    None (after printing the roster) if ``--card`` matches nothing or the choice is ambiguous."""
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


# ---- reference APs ---------------------------------------------------------
def add_reference_args(parser) -> None:
    """Add the reference-AP override flags (same names as capture.py) to an argparse parser."""
    parser.add_argument("--bssid2g", default=None,
                        help="2.4 GHz reference AP BSSID (pins the A/B beacon-rate line).")
    parser.add_argument("--channel2g", type=int, default=None, help="channel for --bssid2g.")
    parser.add_argument("--bssid5g", default=None,
                        help="5 GHz reference AP BSSID (pins the A/B beacon-rate line).")
    parser.add_argument("--channel5g", type=int, default=None, help="channel for --bssid5g.")


def _load_ref_file() -> dict:
    out: dict = {}
    if not _REF_FILE.exists():
        return out
    for line in _REF_FILE.read_text().splitlines():
        if line.strip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def load_reference_aps(args=None) -> dict:
    """``{"2.4": {"bssid","channel"}, "5": {...}}`` from reference_aps.txt, CLI-overridden.
    A band is present only if it has a BSSID."""
    f = _load_ref_file()

    def pick(cli_attr: str, file_key: str):
        val = getattr(args, cli_attr, None) if args is not None else None
        return val if val not in (None, "") else f.get(file_key)

    refs: dict = {}
    b2, c2 = pick("bssid2g", "bssid2g"), pick("channel2g", "channel2g")
    b5, c5 = pick("bssid5g", "bssid5g"), pick("channel5g", "channel5g")
    if b2:
        refs["2.4"] = {"bssid": b2.lower(), "channel": int(c2) if c2 else 1}
    if b5:
        refs["5"] = {"bssid": b5.lower(), "channel": int(c5) if c5 else 36}
    return refs


def ref_channels(refs: dict) -> list[int]:
    return [r["channel"] for r in refs.values()]


def ref_bssids(refs: dict) -> list[str]:
    return [r["bssid"] for r in refs.values()]
