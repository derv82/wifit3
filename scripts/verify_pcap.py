"""One entry point to byte-diff a driver's bring-up against its vendor cold-boot capture.

    uv run python scripts/verify_pcap.py <chip> [capture]
    uv run python scripts/verify_pcap.py --list

A PASS means only that, for the given capture, the port emits the same USB bytes the
vendor driver did -- a faithfulness gate against partial-port divergence, NOT a
correctness proof (see each chip's verify_pcap.py header).

This dispatcher only ROUTES <chip> to that chip's recipe in scripts/<chip>/verify_pcap.py
(exposing ``run(capture) -> int``); the chip-specific bring-up call sequence stays sealed
in that one module. The wire-format op-extractor + ReplayTransport are shared per USB
family -- ``rtw88_pcap_replay.py`` for Realtek (vendor bRequest 0x05); a Ralink
``rt2x00_pcap_replay.py`` (bRequest 0x06/0x07) lands with those recipes.

Out of scope here: mt76 (MediaTek) bring-up is MCU command/response, not register writes,
so its PHY/calibration is not a register byte-diff (deferred). AR9271 is event-driven
HTC/WMI with firmware re-enumeration -- it has its own harness
(scripts/ar9271/build_template.py) and is listed below only as a pointer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    "rt2500usb": Chip("rt2500usb", "rt2500usb/verify_pcap.py",
                      "Ralink RT2570 rt2x00 (vendor 0x06/0x07)"),
    "rt2800usb": Chip("rt2800usb", "rt2800usb/verify_pcap.py",
                      "Ralink RT3572/RT5372/RT5572 rt2x00 (vendor 0x06/0x07)"),
    "rtl8188eus": Chip("rtl8188eus", "rtl8188eus/verify_pcap.py",
                       "Realtek RTL8188EUS / rtl8xxxu (vendor 0x05)"),
    # Legacy Realtek recipe still to come (reuses the 0x05 codec): rtl8187.
    "ar9271": Chip("ar9271", None, "Atheros AR9271 (HTC/WMI)",
                   pointer="event-driven HTC/WMI + firmware re-enumeration -- not a "
                           "register byte-diff; use scripts/ar9271/build_template.py"),
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
