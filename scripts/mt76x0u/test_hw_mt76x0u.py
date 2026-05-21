"""Hardware test for MT76x0U / MT7610U bring-up.

  --phase probe : USB enumeration + claim + MAC_CSR0 read (M0).
  --phase fw    : probe + FW upload + FW_READY ack (M1).
  --phase all   : runs the latest milestone.

Usage:
    uv run python scripts/mt76x0u/test_hw_mt76x0u.py --phase probe
    uv run python scripts/mt76x0u/test_hw_mt76x0u.py --phase fw [--debug]

Per CLAUDE.md "Hardware testing is the USER's job" — this script is run by
the operator. Output paste-back drives the next iteration.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.mt76x0u.constants import (
    MT_MAC_CSR0,
    MT_MCU_COM_REG0,
    MT_MCU_COM_REG0_FW_READY,
    USB_IDS_MT76X0U,
)
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.engine.protocols import DeviceID


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def step(label: str) -> None:
    print(f"\n--- {label} ---")


def ok(msg: str) -> None:
    print(f"[PASS] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[ ..  ] {msg}")


def open_device():
    backend = libusb_package.get_libusb1_backend()
    for vid, pid, desc in USB_IDS_MT76X0U:
        found = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if found is not None:
            print(f"[*] Matched device: {desc} ({vid:04x}:{pid:04x})")
            return found, DeviceID(vid, pid, desc)
    fail(
        "No supported MT76x0U device found. Expected one of: "
        + ", ".join(f"{v:04x}:{p:04x}" for v, p, _ in USB_IDS_MT76X0U)
    )


def progress(pct: float, msg: str) -> None:
    print(f"  [{pct * 100:5.1f}%] {msg}")


async def phase_probe(driver: MT76x0UDriver) -> None:
    """M0 — claim interface, read MAC_CSR0 + MT_MCU_COM_REG0.

    Read-only against the chip. Safe on warm AND cold boot.
    """
    step("M0 — interface claim + register read")
    try:
        driver.transport.claim()
    except RuntimeError as e:
        fail(f"interface claim: {e}")
    ok("interface claimed")

    try:
        mac_csr0 = driver.transport.read32(MT_MAC_CSR0)
    except Exception as e:
        fail(f"MAC_CSR0 read: {type(e).__name__}: {e}")
    if mac_csr0 in (0, 0xFFFFFFFF):
        info(f"MAC_CSR0 = 0x{mac_csr0:08x} (chip not yet powered — expected on cold boot)")
    else:
        ok(f"MAC_CSR0 = 0x{mac_csr0:08x} (chip is alive — likely warm)")

    try:
        com_reg0 = driver.transport.read32(MT_MCU_COM_REG0)
    except Exception as e:
        fail(f"MT_MCU_COM_REG0 read: {type(e).__name__}: {e}")
    fw_running = bool(com_reg0 & MT_MCU_COM_REG0_FW_READY)
    ok(
        f"MT_MCU_COM_REG0 = 0x{com_reg0:08x}  "
        f"(FW_READY {'set — warm chip with FW already running' if fw_running else 'clear — cold boot, FW upload needed'})"
    )


async def phase_fw(driver: MT76x0UDriver) -> None:
    """M1 — full bring-up: chip on + FW upload + FW_READY ack."""
    step("M1 — firmware upload to FW_READY ack")
    success = await driver.connect(progress_cb=progress)
    if not success:
        fail("driver.connect() returned False — see logs above")
    if driver.fw_info is None:
        fail("driver.connect() succeeded but fw_info is None")
    h = driver.fw_info.get("header")
    if driver.fw_info["skipped"]:
        ok("Warm boot — FW was already running (no upload needed)")
        return
    ok(f"FW version: {h['fw_ver_str']}  build 0x{h['build_ver']:04x}  time {h['build_time']!r}")
    ok(f"FW_READY ack after {driver.fw_info['polls']} poll(s)")
    ok("M1 complete")


def main() -> int:
    parser = argparse.ArgumentParser(description="MT76x0U hardware test driver")
    parser.add_argument(
        "--phase",
        choices=["probe", "fw", "all"],
        default="all",
        help="Which milestone to test (default: all)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    setup_logging(args.debug)

    dev, id_entry = open_device()
    driver = MT76x0UDriver.from_usb_device(dev, id_entry)

    try:
        if args.phase in ("probe", "all"):
            asyncio.run(phase_probe(driver))
            # If we're running "all", we want the chip in a known state for
            # the next phase. Probe is read-only so we just continue.
        if args.phase in ("fw", "all"):
            # When --phase=fw alone, we still need to have claimed the
            # interface; driver.connect() claims again (idempotent in transport).
            asyncio.run(phase_fw(driver))
        return 0
    finally:
        try:
            asyncio.run(driver.close())
        except Exception as e:
            print(f"[WARN] driver.close() raised: {e}")


if __name__ == "__main__":
    sys.exit(main())
