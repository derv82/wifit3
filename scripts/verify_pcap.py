"""One entry point to byte-diff a driver's bring-up against its vendor cold-boot capture.

    uv run python scripts/verify_pcap.py <chip> [capture]
    uv run python scripts/verify_pcap.py --list

A PASS means only that, for the given capture, the port emits the same USB bytes the
vendor driver did -- a Pcap Replay against partial-port divergence, NOT a
correctness proof (see each chip's verify_pcap.py header).

This dispatcher only ROUTES <chip> to that chip's recipe in scripts/<chip>/verify_pcap.py
(exposing ``run(capture) -> int``); the chip-specific bring-up call sequence stays sealed
in that one module. The wire-format op-extractor + ReplayTransport are shared per USB
family -- ``rtw88_pcap_replay.py`` for Realtek (vendor bRequest 0x05); a Ralink
``rt2x00_pcap_replay.py`` (bRequest 0x06/0x07); an mt76-USB ``mt76usb_pcap_replay.py``
(MediaTek bRequest 0x06/0x07, shared by mt76x2u + mt76x0u).

mt76 (MediaTek) is in scope: mt76x2u / mt76x0u do register I/O over vendor bRequest 0x06/0x07
(the mt76-USB codec) and a single-cursor walk reproduces cold-boot + FW + MCU + TX; mt7921au
is connac2's unified bus with its own decoder; ar9271_v2 is event-driven HTC/WMI with firmware
re-enumeration and its own decoder (ar9271_v2/verify_pcap.py).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Windows consoles default to cp1252; recipes print arrows / em-dashes in their status lines
# (e.g. rt2800usb / rt5572). Force UTF-8 so a non-ASCII char can never crash a run mid-walk.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


class Chip:
    """One registry entry: a runnable recipe, or a pointer to a separate harness."""

    def __init__(self, key: str, script: str | None, desc: str, pointer: str | None = None):
        self.key = key
        self.script = script        # scripts/<script> exposing run(capture) -> int
        self.desc = desc
        self.pointer = pointer      # set => not a register byte-diff; print and exit 0

    def run(self, cap: str | None) -> int:
        if self.pointer:
            print(f"{self.key}: {self.pointer}")
            return 0
        path = SCRIPTS / self.script
        if not path.exists():
            print(f"{self.key}: recipe not found at {path}")
            return 2
        return _load(self.key, path).run(cap)


def _load(key: str, path: Path):
    """Load a chip's verify_pcap.py by file path under a unique module name. Every recipe
    file is named verify_pcap.py, so a plain ``import`` would collide in sys.modules."""
    spec = importlib.util.spec_from_file_location(f"_recipe_{key}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REGISTRY: dict[str, Chip] = {
    "rtl8812au_dkms": Chip("rtl8812au_dkms", "rtl8812au_dkms/verify_pcap.py",
                           "Realtek rtw88 2T2R DKMS (vendor 0x05)"),
    "rtl8814au_dkms": Chip("rtl8814au_dkms", "rtl8814au_dkms/verify_pcap.py",
                           "Realtek rtw88 4T4R DKMS (vendor 0x05)"),
    "rtl8821au_dkms": Chip("rtl8821au_dkms", "rtl8821au_dkms/verify_pcap.py",
                           "Realtek rtw88 1T1R DKMS (vendor 0x05)"),
    "rtl8822bu_dkms": Chip("rtl8822bu_dkms", "rtl8822bu_dkms/verify_pcap.py",
                           "Realtek rtl88x2bu 2T2R DKMS (vendor 0x05) — SCAFFOLD, WIP"),
    "rtl8821cu_dkms": Chip("rtl8821cu_dkms", "rtl8821cu_dkms/verify_pcap.py",
                           "Realtek RTL8821CU 1T1R 11ac DKMS (vendor 0x05) — SCAFFOLD, M1 power-on"),
    "rt2500usb": Chip("rt2500usb", "rt2500usb/verify_pcap.py",
                      "Ralink RT2570 rt2x00 (vendor 0x06/0x07)"),
    "rt2800usb": Chip("rt2800usb", "rt2800usb/verify_pcap.py",
                      "Ralink RT3572/RT5372/RT5572 rt2x00 (vendor 0x06/0x07)"),
    "rt3070": Chip("rt3070", "rt3070/verify_pcap.py",
                   "Ralink RT3070 / ALFA AWUS036NH clean-room (vendor 0x06/0x07)"),
    "rt5370": Chip("rt5370", "rt5370/verify_pcap.py",
                   "Ralink RT5370 (RT5390) 1T1R clean-room (vendor 0x06/0x07)"),
    "rt5372": Chip("rt5372", "rt5372/verify_pcap.py",
                   "Ralink RT5372 (RT5392) / Panda PAU05+PAU06 clean-room (vendor 0x06/0x07)"),
    "rt5572": Chip("rt5572", "rt5572/verify_pcap.py",
                   "Ralink RT5572 (RF5592) / Panda PAU09 — standalone; gate drives the real bring_up()"),
    "rtl8188eus": Chip("rtl8188eus", "rtl8188eus/verify_pcap.py",
                       "Realtek RTL8188EUS / rtl8xxxu (vendor 0x05)"),
    "rtl8188eus_dkms": Chip("rtl8188eus_dkms", "rtl8188eus_dkms/verify_pcap.py",
                            "Realtek RTL8188EUS 1T1R DKMS (vendor 0x05)"),
    "rtl8187": Chip("rtl8187", "rtl8187/verify_pcap.py",
                    "Realtek RTL8187L / rtl818x (vendor 0x05)"),
    "mt7921au": Chip("mt7921au", "mt7921au/verify_pcap.py",
                     "MediaTek MT7921AU connac2 unified-bus (cold-boot + FW + MCU + TX)"),
    "mt76x2u": Chip("mt76x2u", "mt76x2u/verify_pcap.py",
                    "MediaTek MT7612U mt76-USB (vendor 0x06/0x07; cold-boot + FW + MCU + TX)"),
    "mt76x0u": Chip("mt76x0u", "mt76x0u/verify_pcap.py",
                    "MediaTek MT7610U mt76-USB (vendor 0x06/0x07; cold-boot + FW + 2.4 GHz TX)"),
    "ar9271_v2": Chip("ar9271_v2", "ar9271_v2/verify_pcap.py",
                      "Atheros AR9271 ath9k_htc clean-room re-port (firmware + HTC/WMI; WIP)"),
}


def _print_chips() -> None:
    for c in REGISTRY.values():
        tag = "  (pointer)" if c.pointer else ""
        print(f"  {c.key:18} {c.desc}{tag}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("chips:")
        _print_chips()
        return 0
    if argv[0] == "--list":
        _print_chips()
        return 0
    key = argv[0]
    if key not in REGISTRY:
        print(f"unknown chip '{key}'. --list to see registered chips.")
        return 2
    cap = argv[1] if len(argv) > 1 else None
    return REGISTRY[key].run(cap)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
