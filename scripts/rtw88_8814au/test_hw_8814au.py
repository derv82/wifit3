"""Hardware test for RTL8814AU (Alfa AWUS1900) bring-up.

Milestone M1 = firmware upload + FW_READY ACK. Phases implemented so far:

  --phase open     : USB enumeration + control transfers (REG_SYS_CFG1 read).
  --phase fw       : open + power-on + FW upload (iDDMA, DMEM+IMEM).
  --phase validate : fw + FW_READY mask poll (wlan CPU running).  [M1 GATE]
  --phase all      : open -> fw -> validate (default).

Later milestones (phy/mac_init/channel/beacon/tx) are stubbed and will be
filled in as M2..M6 land.

Run (unplug + replug first for a clean cold boot):
    .venv/Scripts/python.exe scripts/rtw88_8814au/test_hw_8814au.py
    .venv/Scripts/python.exe scripts/rtw88_8814au/test_hw_8814au.py --phase fw --debug
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtw88_8814au.constants import (
    REG_CR,
    REG_MCUFW_CTRL,
    REG_SYS_CFG1,
    USB_IDS_8814AU,
)
from wifit3.chips.rtw88_8814au.firmware import (
    download_firmware,
    download_firmware_validate,
    load_firmware_blob,
    parse_fw_header,
)
from wifit3.chips.rtw88_8814au.fifo import (
    count_bulk_out_eps,
    rtw_init_trx_cfg,
)
from wifit3.chips.rtw88_8814au.mac import (
    cut_mask_from_sys_cfg1,
    is_chip_warm,
    mac_power_on,
)
from wifit3.chips.rtw88_8814au.transport import RTL8814AUTransport


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


def _decode_mcufw_bits(val: int) -> str:
    flags = [
        (3, "IMEM_DW_OK"), (4, "IMEM_CHKSUM_OK"),
        (5, "DMEM_DW_OK"), (6, "DMEM_CHKSUM_OK"),
        (14, "FW_DW_RDY"), (15, "FW_INIT_RDY"),
    ]
    bits = [name for bit, name in flags if val & (1 << bit)]
    return ", ".join(bits) if bits else "(none)"


def open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = None
    matched = None
    for vid, pid, desc in USB_IDS_8814AU:
        found = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if found is not None:
            dev, matched = found, (vid, pid, desc)
            break
    if dev is None:
        fail(
            "No RTL8814AU device found. Expected one of:\n"
            + "\n".join(f"    {vid:04x}:{pid:04x}  {desc}"
                        for vid, pid, desc in USB_IDS_8814AU)
            + "\nPlug it in, confirm Zadig bound it to WinUSB, and retry."
        )
    print(f"  Found {matched[0]:04x}:{matched[1]:04x}  {matched[2]}")
    print(f"  bus={dev.bus} address={dev.address} bcdUSB={dev.bcdUSB:#06x}")
    # USB-speed sanity (AWUS1900 is USB-3 branded; confirm what we actually got).
    if dev.bcdUSB < 0x0300:
        print(f"  NOTE: enumerated at USB {dev.bcdUSB:#06x} (not SuperSpeed)")

    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            print("  Detached kernel driver from interface 0")
    except (NotImplementedError, usb.core.USBError) as e:
        print(f"  Skipping kernel-driver detach: {e}")

    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        fail(f"set_configuration() failed: {e}")

    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        fail(f"claim_interface(0) failed: {e}")
    return dev


def phase_open(transport: RTL8814AUTransport) -> None:
    step("Read REG_SYS_CFG1 (0x00F0)")
    val = transport.read32(REG_SYS_CFG1)
    print(f"  REG_SYS_CFG1 = 0x{val:08x}")
    if val in (0, 0xFFFFFFFF):
        fail(f"Implausible value 0x{val:08x} — bad state. Unplug, wait 5s, replug.")
    cut_version = (val >> 12) & 0xF
    cut_letters = "ABCDEFG"
    cut_name = cut_letters[cut_version] if cut_version < len(cut_letters) else f"?{cut_version}"
    print(f"  cut_version = {cut_version} (CUT_{cut_name})")
    print(f"  cut_mask    = 0x{cut_mask_from_sys_cfg1(val):02x}")
    ok("control-transfer plumbing works")


def phase_fw(dev, transport: RTL8814AUTransport) -> None:
    step("Detect chip warm/cold state")
    if is_chip_warm(transport):
        print("  Chip is WARM — FW already loaded from a prior session.")
        print("  Skipping FW upload. Replug to test from cold.")
        ok("warm chip detected (FW upload not needed)")
        return

    step("Inspect FW blob header")
    fw = load_firmware_blob()
    sections = parse_fw_header(fw)
    print(f"  Blob: {len(fw)} bytes (with 64-byte header)")
    for name, off, dst, size in sections:
        print(f"    {name}: {size} bytes -> 0x{dst:08x} (file offset {off})")

    step("MAC power-on (pre-cfg + pwr_seq + system-cfg)")
    chip_version = transport.read32(REG_SYS_CFG1)
    cut_mask = cut_mask_from_sys_cfg1(chip_version)
    print(f"  REG_SYS_CFG1 = 0x{chip_version:08x}, cut_mask = 0x{cut_mask:02x}")
    try:
        mac_power_on(transport, cut_mask=cut_mask)
    except (IOError, NotImplementedError) as e:
        fail(f"mac_power_on failed: {e}")
    ok("mac_power_on completed")
    print(f"  REG_MCUFW_CTRL = 0x{transport.read32(REG_MCUFW_CTRL):08x}  (pre-FW)")
    print(f"  REG_CR         = 0x{transport.read32(REG_CR):08x}  (pre-FW)")

    step("Upload firmware (iDDMA: DMEM + IMEM)")
    last_pct = -1

    def progress(done: int, total: int) -> None:
        nonlocal last_pct
        pct = int(done * 100 / total)
        if pct != last_pct and pct % 10 == 0:
            last_pct = pct
            print(f"  [{pct:3d}%] {done}/{total} bytes")

    t0 = time.perf_counter()
    try:
        download_firmware(dev, transport, fw, progress_cb=progress)
    except Exception as e:
        mcufw = transport.read32(REG_MCUFW_CTRL)
        fail(f"download_firmware raised {type(e).__name__}: {e}\n"
             f"  REG_MCUFW_CTRL = 0x{mcufw:08x}")
    dt = (time.perf_counter() - t0) * 1000
    mcufw = transport.read32(REG_MCUFW_CTRL)
    print(f"  REG_MCUFW_CTRL = 0x{mcufw:08x}  (post-upload, {dt:.0f} ms)")
    print(f"  Set bits: {_decode_mcufw_bits(mcufw)}")
    ok("FW bulk-OUT + iDDMA pipeline completed without raising")


def phase_validate(transport: RTL8814AUTransport) -> None:
    step("Validate FW running (poll FW_READY mask)  [M1 GATE]")
    ok_run, last = download_firmware_validate(transport)
    print(f"  REG_MCUFW_CTRL = 0x{last:08x}")
    print(f"  Set bits: {_decode_mcufw_bits(last)}")
    if not ok_run:
        fail("FW_READY never satisfied — wlan CPU is not running the firmware.")
    ok("FW_READY satisfied — wlan CPU is running the firmware. M1 COMPLETE.")


def phase_mac_init(dev, transport: RTL8814AUTransport) -> None:
    step("TRX init: queue mapping + FIFO/priority queues + LLT  [M2 GATE]")
    bulkout = count_bulk_out_eps(dev)
    print(f"  bulk-OUT endpoints detected: {bulkout} "
          f"(selects rqpn_table_8814a[{bulkout}])")
    if bulkout not in (2, 3, 4):
        fail(f"unexpected bulk-OUT count {bulkout} (expected 2/3/4)")
    t0 = time.perf_counter()
    try:
        fifo = rtw_init_trx_cfg(transport, bulkout)
    except IOError as e:
        fail(f"rtw_init_trx_cfg failed: {e}")
    dt = (time.perf_counter() - t0) * 1000
    print(f"  rsvd_boundary   = {fifo['rsvd_boundary']} pages")
    print(f"  rsvd_h2cq_addr  = {fifo['rsvd_h2cq_addr']} pages")
    print(f"  REG_CR          = 0x{transport.read32(REG_CR):08x} "
          f"(MAC_TRX_ENABLE = low byte 0xff)")
    ok(f"LLT auto-init completed + H2C ring verified in {dt:.0f} ms. M2 COMPLETE.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--phase",
                   choices=("open", "fw", "validate", "mac_init", "all"),
                   default="all")
    p.add_argument("--debug", action="store_true", help="verbose USB logging")
    args = p.parse_args()
    setup_logging(args.debug)

    step("USB discovery + claim")
    dev = open_device()
    ok("Interface 0 claimed")
    transport = RTL8814AUTransport(dev)

    needs_fw = ("fw", "validate", "mac_init", "all")
    needs_validate = ("validate", "mac_init", "all")
    needs_mac_init = ("mac_init", "all")

    try:
        if args.phase in ("open", "all"):
            phase_open(transport)
        if args.phase in needs_fw:
            phase_fw(dev, transport)
        if args.phase in needs_validate:
            phase_validate(transport)
        if args.phase in needs_mac_init:
            phase_mac_init(dev, transport)
    finally:
        step("Release interface")
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
            print("  Released cleanly")
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
