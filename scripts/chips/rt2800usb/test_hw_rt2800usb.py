"""Hardware test for rt2800usb bring-up (RT3572 / RT5372 / RT5572).

Phase summary (mirrors the milestone breakdown):
  --phase open     : USB enumeration + chip ID + MAC + warm probe. (M1)
  --phase fw       : open + rt2870.bin upload + MCU boot. (M2a)
  --phase usbinit  : fw + rt2800usb_init_registers. (M2b-1)
  --phase macinit  : usbinit + rt2800_init_registers (~50 MAC config
                     writes). Spot-checks a few writes survived. (M2b-2)
  --phase bbpinit  : macinit + init_bbp_53xx (~30 BBP register writes
                     via BBP_CSR_CFG indirect protocol). Spot-checks
                     BBP[4] has MAC_IF_CTRL bit set. (M2b-3)
  --phase rfinit   : bbpinit + init_rfcsr_5392 (~60 RF register writes
                     + cal + normal_mode_setup + LED open-drain). (M2c)
  --phase rx       : rfinit + 3s of bulk-IN drain → decode RX URBs +
                     parse 802.11 frames. (M3)

Usage:
    uv run python scripts/chips/rt2800usb/test_hw_rt2800usb.py                  # all
    uv run python scripts/chips/rt2800usb/test_hw_rt2800usb.py --phase open     # M1
    uv run python scripts/chips/rt2800usb/test_hw_rt2800usb.py --phase fw       # M2a
    uv run python scripts/chips/rt2800usb/test_hw_rt2800usb.py --phase usbinit  # M2b-1
    uv run python scripts/chips/rt2800usb/test_hw_rt2800usb.py --phase macinit  # M2b-2
    uv run python scripts/chips/rt2800usb/test_hw_rt2800usb.py --phase bbpinit  # M2b-3
    uv run python scripts/chips/rt2800usb/test_hw_rt2800usb.py --phase rfinit   # M2c
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rt2800usb.constants import (
    MAC_CSR0,
    PBF_SYS_CTRL,
    PBF_SYS_CTRL_READY,
    USB_PID_RT3572,
    USB_PID_RT5372,
    USB_PID_RT5572,
    USB_VID_RALINK,
    WLAN_FUN_CTRL,
    WLAN_FUN_CTRL_WLAN_EN,
)
from wifit3.chips.rt2800usb.firmware import load_firmware, load_firmware_blob
from wifit3.chips.rt2800usb.chan import set_channel as _set_channel
from wifit3.chips.rt2800usb.eeprom import parse_eeprom, read_eeprom_efuse
from wifit3.chips.rt2800usb.mac import (
    enable_radio,
    is_chip_warm,
    read_chip_id,
    read_perm_mac,
    usb_init_registers,
    write_mac_address,
)
from wifit3.chips.rt2800usb.bbp import bbp_read, init_bbp, prepare_bbp
from wifit3.chips.rt2800usb.reg_init import init_registers
from wifit3.chips.rt2800usb.rfcsr import init_rfcsr, rfcsr_read
from wifit3.chips.rt2800usb.rx import (
    parse_rx_urb,
    probe_endpoints,
    read_rx_burst,
    rxwi_size_for_silicon,
)
from wifit3.dot11.parser import WlanFrameParser
from wifit3.chips.rt2800usb.transport import RT2800USBTransport


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


def open_device():
    """Find any of RT3572/RT5372/RT5572 plugged in. First-match wins."""
    backend = libusb_package.get_libusb1_backend()
    for pid, label in (
        (USB_PID_RT5372, "RT5372 (Panda PAU05)"),
        (USB_PID_RT3572, "RT3572 (ALFA AWUS051NH v2)"),
        (USB_PID_RT5572, "RT5572 (Panda PAU09 N600)"),
    ):
        dev = usb.core.find(idVendor=USB_VID_RALINK, idProduct=pid, backend=backend)
        if dev is not None:
            print(f"  Found {label} at bus {dev.bus}, address {dev.address}")
            print(f"  bcdUSB=0x{dev.bcdUSB:04x}, bcdDevice=0x{dev.bcdDevice:04x}")
            break
    else:
        fail(
            "No rt2800usb dongle found (looking for VID 0x148f, "
            "PID 0x5372/0x3572/0x5572). Plug one in, confirm Zadig "
            "bound it to WinUSB, and retry."
        )

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


def phase_open(transport: RT2800USBTransport) -> None:
    step("Read MAC_CSR0 (chip ID + revision)")
    chip = read_chip_id(transport)
    print(f"  MAC_CSR0  = 0x{chip.raw:08x}")
    print(f"  silicon   = 0x{chip.silicon_id:04x} ({chip.name})")
    print(f"  revision  = 0x{chip.revision:04x}")
    if chip.raw == 0 or chip.raw == 0xFFFFFFFF:
        fail(
            f"Implausible MAC_CSR0=0x{chip.raw:08x} — chip in bad state. "
            "Unplug, wait 5s, replug."
        )
    if not chip.is_supported:
        fail(
            f"silicon 0x{chip.silicon_id:04x} not in supported set "
            f"(RT3572=0x3572, RT5390/RT5392=0x5390/0x5392 cover RT5372, "
            f"RT5592=0x5592 covers RT5572)."
        )
    ok(f"chip = {chip.name} rev 0x{chip.revision:04x}")

    step("Read MAC_ADDR_DW0/DW1 (permanent MAC)")
    mac_bytes = read_perm_mac(transport)
    mac_str = ":".join(f"{b:02x}" for b in mac_bytes)
    print(f"  MAC = {mac_str}")
    if mac_bytes == b"\x00" * 6:
        print("  NOTE: all zeros — EEPROM probably not loaded yet (M2 will trigger it)")
    elif mac_bytes == b"\xff" * 6:
        print("  NOTE: all 0xFF — unexpected. Chip may be in a bad state.")
    else:
        ok(f"control-IN plumbing works; MAC reads back as {mac_str}")

    step("Read WLAN_FUN_CTRL + PBF_SYS_CTRL (warm probe)")
    wlan_fun = transport.read32(WLAN_FUN_CTRL)
    pbf = transport.read32(PBF_SYS_CTRL)
    print(f"  WLAN_FUN_CTRL = 0x{wlan_fun:08x}  WLAN_EN={bool(wlan_fun & WLAN_FUN_CTRL_WLAN_EN)}")
    print(f"  PBF_SYS_CTRL  = 0x{pbf:08x}      READY={bool(pbf & PBF_SYS_CTRL_READY)}")
    if is_chip_warm(transport):
        ok("chip is WARM (WLAN_EN + PBF.READY both set — prior FW boot)")
    else:
        ok("chip is COLD (WLAN_EN or PBF.READY clear — normal on first plug-in)")


def phase_fw(transport: RT2800USBTransport) -> None:
    """M2a: read chip ID then upload rt5572.bin + verify PBF.READY."""
    step("Read chip ID (need silicon_id for FW section selection)")
    chip = read_chip_id(transport)
    print(f"  chip = {chip.name} (silicon=0x{chip.silicon_id:04x})")
    if not chip.is_supported:
        fail(f"chip 0x{chip.silicon_id:04x} not supported for FW upload.")

    step("Load firmware blob from assets/rt5572.bin")
    fw_bytes = load_firmware_blob()
    print(f"  blob size = {len(fw_bytes)} bytes")

    step("Upload firmware + MCU boot")

    def progress(p: float, msg: str) -> None:
        print(f"  [{int(p*100):3d}%] {msg}")

    import time as _t
    t0 = _t.perf_counter()
    try:
        load_firmware(transport, fw_bytes, silicon_id=chip.silicon_id, progress_cb=progress)
    except IOError as e:
        fail(f"load_firmware raised: {e}")
    dt = _t.perf_counter() - t0
    ok(f"FW boot completed in {dt:.2f}s")

    step("Verify post-FW state")
    pbf = transport.read32(PBF_SYS_CTRL)
    mac_csr0 = transport.read32(MAC_CSR0)
    print(f"  PBF_SYS_CTRL  = 0x{pbf:08x}  READY={bool(pbf & PBF_SYS_CTRL_READY)}")
    print(f"  MAC_CSR0      = 0x{mac_csr0:08x}  (still readable post-FW)")
    pre_init = 1 << 13
    if pbf & pre_init:
        print("  NOTE: pre-init bit 13 still set — that's expected; M2b's init_registers clears it")
    if not (pbf & PBF_SYS_CTRL_READY):
        fail("PBF.READY not set — FW didn't boot.")
    ok("PBF.READY latched — firmware is running")


def phase_usbinit(transport: RT2800USBTransport) -> None:
    """M2b-1: run rt2800usb_init_registers after FW upload + verify
    that the PBF pre-init bit (bit 13) clears."""
    # Run fw phase first (FW must be booted before MAC init runs).
    phase_fw(transport)

    step("Run rt2800usb_init_registers (clears PBF bit 13 + USB_MODE_RESET)")
    pre = transport.read32(PBF_SYS_CTRL)
    print(f"  pre-init PBF_SYS_CTRL  = 0x{pre:08x}  pre-init-bit-13={bool(pre & (1 << 13))}")
    try:
        usb_init_registers(transport)
    except (IOError, usb.core.USBError) as e:
        fail(f"usb_init_registers raised: {e}")
    post = transport.read32(PBF_SYS_CTRL)
    print(f"  post-init PBF_SYS_CTRL = 0x{post:08x}  pre-init-bit-13={bool(post & (1 << 13))}")
    if post & (1 << 13):
        fail("pre-init bit 13 still set — usb_init_registers didn't take effect")
    if not (post & PBF_SYS_CTRL_READY):
        fail(f"PBF.READY no longer set (0x{post:08x}) — USB_MODE_RESET may have wedged the chip")
    ok("pre-init bit 13 cleared; READY still latched")

    step("Verify chip still responsive after USB_MODE_RESET")
    chip = read_chip_id(transport)
    print(f"  MAC_CSR0 = 0x{chip.raw:08x}  (chip = {chip.name})")
    if chip.raw == 0 or chip.raw == 0xFFFFFFFF:
        fail("MAC_CSR0 unreadable post-reset — chip may need a replug")
    ok("chip still responsive — M2b-1 complete")


def phase_macinit(transport: RT2800USBTransport) -> None:
    """M2b-2: run init_registers + spot-check a handful of writes
    landed (read back known-good values)."""
    # Earlier phases (FW + usb_init_registers) must run first.
    phase_usbinit(transport)

    step("Run rt2800_init_registers (~50 MAC config writes)")
    chip = read_chip_id(transport)
    import time as _t
    t0 = _t.perf_counter()
    try:
        init_registers(transport, chip.silicon_id)
    except (IOError, usb.core.USBError) as e:
        fail(f"init_registers raised: {e}")
    dt = _t.perf_counter() - t0
    ok(f"init_registers completed in {dt * 1000:.0f} ms")

    step("Spot-check known-good values")
    from wifit3.chips.rt2800usb.constants import (
        AUTO_RSP_CFG, HT_BASIC_RATE, LEGACY_BASIC_RATE,
        PBF_MAX_PCNT, TX_SW_CFG0,
    )
    checks = [
        ("LEGACY_BASIC_RATE", LEGACY_BASIC_RATE, 0x0000013F),
        ("HT_BASIC_RATE",     HT_BASIC_RATE,     0x00008003),
        ("TX_SW_CFG0",        TX_SW_CFG0,        0x00000404),
        ("PBF_MAX_PCNT",      PBF_MAX_PCNT,      0x1F3FBF9F),
    ]
    all_ok = True
    for name, addr, expected in checks:
        actual = transport.read32(addr)
        marker = "OK" if actual == expected else "MISMATCH"
        print(f"  {name:20s} = 0x{actual:08x}  (expected 0x{expected:08x}) [{marker}]")
        if actual != expected:
            all_ok = False

    # AUTO_RSP_CFG has bits 0/1/2 set (autoresponder + bac_ack + cts_40_mmode);
    # other bits depend on prior state. Just check the set bits are present.
    auto_rsp = transport.read32(AUTO_RSP_CFG)
    print(f"  AUTO_RSP_CFG         = 0x{auto_rsp:08x}  (need bits 0,1,2 set)")
    if (auto_rsp & 0x07) != 0x07:
        print("    WARNING: AUTO_RSP_CFG missing one of bits 0/1/2")
        all_ok = False

    if all_ok:
        ok("all spot-checks passed — MAC config landed cleanly")
    else:
        fail("some MAC registers don't read back as expected")


def phase_bbpinit(transport: RT2800USBTransport) -> None:
    """M2b-3: run init_bbp via BBP_CSR_CFG indirect protocol,
    then silicon-aware spot-check a handful of writes landed.

    For RT3572 we also need an EFUSE read first so init_bbp can be
    given the right txpath/rxpath; for RT5392 those args are ignored
    so we skip the EFUSE step in this phase to keep the milestone
    boundary clean. Per-silicon spot-checks come from the kernel's
    init_bbp_{53xx,3572} tables.
    """
    phase_macinit(transport)

    step("Prepare BBP (wait_bbp_rf_ready + MCU_BOOT_SIGNAL + wait_bbp_ready)")
    try:
        prepare_bbp(transport)
    except (IOError, usb.core.USBError) as e:
        fail(f"prepare_bbp raised: {e}")
    ok("BBP prepared")

    chip = read_chip_id(transport)

    # RT3572 and RT5592 init_bbp want txpath/rxpath (RT5592 also wants
    # ant_diversity from NIC_CONF1 + chip rev for rev-gated extras).
    # RT5392 ignores both — init_bbp_53xx pins 1T1R internally — but we
    # still pass the same kwargs uniformly.
    txpath = rxpath = 1
    ant_diversity = 0
    if chip.silicon_id in (0x3572, 0x5592):
        step("Reading EFUSE for txpath/rxpath / ant_diversity")
        eeprom_buf = read_eeprom_efuse(transport)
        ee = parse_eeprom(eeprom_buf)
        txpath = max(1, ee.txpath)
        rxpath = max(1, ee.rxpath)
        ant_diversity = (ee.nic_conf1 & 0x1800) >> 11
        print(
            f"  NIC_CONF0=0x{ee.nic_conf0:04x}  txpath={txpath} rxpath={rxpath}"
        )
        print(
            f"  NIC_CONF1=0x{ee.nic_conf1:04x}  ant_diversity={ant_diversity} "
            "(3 = aux antenna, else = main)"
        )

    step(f"Run init_bbp for silicon 0x{chip.silicon_id:04x}")
    import time as _t
    t0 = _t.perf_counter()
    try:
        init_bbp(
            transport, chip.silicon_id,
            txpath=txpath, rxpath=rxpath,
            ant_diversity=ant_diversity,
            chip_rev=chip.revision,
        )
    except (IOError, usb.core.USBError, ValueError, NotImplementedError) as e:
        fail(f"init_bbp raised: {e}")
    dt = _t.perf_counter() - t0
    ok(f"init_bbp completed in {dt * 1000:.0f} ms")

    step("Spot-check BBP register values")
    # Per-silicon expected values from kernel init_bbp_{53xx,3572,5592}.
    if chip.silicon_id == 0x5592:
        # init_bbp_5592: init_bbp_early sets BBP65=0x2c, then the 5592
        # body overwrites several registers. Final expected values are
        # taken from the kernel function's last write to each register.
        # BBP[4] must have MAC_IF_CTRL bit 6 set (bbp4_mac_if_ctrl
        # asserted twice). BBP[195/196] are the GLRT index/data pair —
        # we sample the last value written (BBP196 holds 0x6e for
        # offset 211, the last entry). BBP[152] bit 7 should be set
        # for main antenna (ant != 3).
        # [SRC] rt2800lib.c:6967-7039
        bbp4 = bbp_read(transport, 4)
        bbp20 = bbp_read(transport, 20)
        bbp31 = bbp_read(transport, 31)
        bbp65 = bbp_read(transport, 65)
        bbp68 = bbp_read(transport, 68)
        bbp84 = bbp_read(transport, 84)    # final write = 0x19, not 0x9a
        bbp105 = bbp_read(transport, 105)  # final write = 0x3c (clobbers MLD)
        bbp137 = bbp_read(transport, 137)
        bbp152 = bbp_read(transport, 152)
        print(f"  BBP[4]   = 0x{bbp4:02x}  (need bit 6 = 0x40 set)")
        print(f"  BBP[20]  = 0x{bbp20:02x}  (expected 0x06)")
        print(f"  BBP[31]  = 0x{bbp31:02x}  (expected 0x08)")
        print(f"  BBP[65]  = 0x{bbp65:02x}  (expected 0x2C)")
        print(f"  BBP[68]  = 0x{bbp68:02x}  (expected 0xDD)")
        print(f"  BBP[84]  = 0x{bbp84:02x}  (expected 0x19 — final value, not the 0x9A intermediate)")
        print(f"  BBP[105] = 0x{bbp105:02x}  (expected 0x3C — final value, clobbers MLD bit)")
        print(f"  BBP[137] = 0x{bbp137:02x}  (expected 0x0F)")
        print(f"  BBP[152] = 0x{bbp152:02x}  (need bit 7 set for main antenna)")
        all_ok = (
            (bbp4 & 0x40) == 0x40
            and bbp20 == 0x06
            and bbp31 == 0x08
            and bbp65 == 0x2C
            and bbp68 == 0xDD
            and bbp84 == 0x19
            and bbp105 == 0x3C
            and bbp137 == 0x0F
            and (bbp152 & 0x80) == 0x80
        )
    elif chip.silicon_id == 0x3572:
        # init_bbp_3572: BBP31=0x08, BBP65=0x2c, BBP66=0x38, BBP91=0x04,
        # BBP106=0x35, no BBP[4] MAC_IF_CTRL (it's set in usb_init_registers
        # earlier — and a R-M-W BBP[4] doesn't happen in init_bbp_3572).
        # BBP[142] is also not written by init_bbp_3572 (no init_freq_cal).
        checks = [
            ("BBP[31]",  31,  0x08),
            ("BBP[65]",  65,  0x2C),
            ("BBP[66]",  66,  0x38),
            ("BBP[91]",  91,  0x04),
            ("BBP[106]", 106, 0x35),
        ]
        all_ok = True
        for name, word, expected in checks:
            actual = bbp_read(transport, word)
            marker = "OK" if actual == expected else "MISMATCH"
            print(f"  {name:8s} = 0x{actual:02x}  (expected 0x{expected:02x}) [{marker}]")
            if actual != expected:
                all_ok = False
    else:
        # init_bbp_53xx checks (RT5392 path — unchanged).
        bbp4 = bbp_read(transport, 4)
        bbp31 = bbp_read(transport, 31)
        bbp65 = bbp_read(transport, 65)
        bbp106 = bbp_read(transport, 106)
        bbp142 = bbp_read(transport, 142)
        expected_106 = 0x12 if chip.silicon_id == 0x5392 else 0x03
        print(f"  BBP[4]   = 0x{bbp4:02x}  (need bit 6 = 0x40 set)")
        print(f"  BBP[31]  = 0x{bbp31:02x}  (expected 0x08)")
        print(f"  BBP[65]  = 0x{bbp65:02x}  (expected 0x2c)")
        print(f"  BBP[106] = 0x{bbp106:02x}  (expected 0x{expected_106:02x} for {chip.name})")
        print(f"  BBP[142] = 0x{bbp142:02x}  (expected 0x01)")
        all_ok = (
            (bbp4 & 0x40) == 0x40
            and bbp31 == 0x08
            and bbp65 == 0x2C
            and bbp106 == expected_106
            and bbp142 == 0x01
        )

    if not all_ok:
        fail("one or more BBP spot-checks failed — init_bbp may not have landed cleanly")
    ok("all BBP spot-checks passed — baseband init complete")


def phase_rfinit(transport: RT2800USBTransport) -> "object":
    """M2c: run init_rfcsr + spot-check a few RFCSR values landed.

    Per-silicon spot-checks pull from the kernel init_rfcsr_* table for
    the corresponding chip. Returns the RfFilterCal for RT3572 (None
    for RT5392) so phase_rx can replay calibration_bw20/bbp25/26 into
    set_channel.
    """
    phase_bbpinit(transport)

    chip = read_chip_id(transport)
    # For RT5592 we need EFUSE freq_offset before init_rfcsr fires.
    rf_freq_offset = 0
    if chip.silicon_id == 0x5592:
        ee = parse_eeprom(read_eeprom_efuse(transport))
        rf_freq_offset = ee.freq_offset
        print(f"  EFUSE freq_offset = {rf_freq_offset} (RT5592 init_rfcsr needs this)")
    step(f"Run init_rfcsr for silicon 0x{chip.silicon_id:04x} ({chip.name})")
    import time as _t
    t0 = _t.perf_counter()
    try:
        cal = init_rfcsr(
            transport, chip.silicon_id,
            freq_offset=rf_freq_offset,
            chip_rev=chip.revision,
        )
    except (IOError, usb.core.USBError, NotImplementedError) as e:
        fail(f"init_rfcsr raised: {e}")
    dt = _t.perf_counter() - t0
    ok(f"init_rfcsr completed in {dt * 1000:.0f} ms")

    step("Spot-check RFCSR values")
    all_ok = True
    if chip.silicon_id == 0x5592:
        # init_rfcsr_5592 table values [SRC] rt2800lib.c:8462-8503.
        # A handful of registers are diagnostic-only because the chip
        # auto-manages bits after the cal kick:
        #   RFCSR1: kernel writes 0x3F, but the chip clears the TX_PD
        #     bits while TX is disabled (we only enabled RX). Observed
        #     post-init: 0x17 (RX0|RX1 enabled, TX0|TX1 cleared). The
        #     value gets re-written to 0x3F on every channel tune, so
        #     post-init noise here is harmless.
        #   RFCSR2: kernel writes 0x80 (cal kick) + sleeps 1ms. After
        #     the cal completes the chip leaves low bits as status.
        #   RFCSR63: kernel writes 0x07. Some revs of the chip auto-
        #     clear bit 0 (observed 0x06 on rev 0x0222). The init
        #     value lands; the chip then post-processes.
        # Everything in the strict-check list maps to a single kernel
        # write that should land unchanged.
        strict_checks = [
            ("RFCSR[3]",  3,  0x08, 0xFF),
            ("RFCSR[5]",  5,  0x10, 0xFF),
            ("RFCSR[6]",  6,  0xE4, 0xFF),
            ("RFCSR[19]", 19, 0x4D, 0xFF),
            ("RFCSR[26]", 26, 0x82, 0xFF),
            ("RFCSR[33]", 33, 0xC0, 0xFF),
            ("RFCSR[35]", 35, 0x12, 0xFF),
            ("RFCSR[47]", 47, 0x0C, 0xFF),
            ("RFCSR[53]", 53, 0x22, 0xFF),
        ]
        info_checks = [
            ("RFCSR[1]",  1,  0x3F, "chip clears TX_PD bits while TX disabled (re-set by set_channel)"),
            ("RFCSR[2]",  2,  0x80, "cal kick — chip auto-clears bit 7 + may set status bits"),
            ("RFCSR[63]", 63, 0x07, "some revs clear bit 0 post-init"),
            ("RFCSR[27]", 27, None, f"chip_rev=0x{chip.revision:04x}: expect 0x03 if rev < 0x0221"),
        ]
        for name, word, expected, mask in strict_checks:
            raw = rfcsr_read(transport, word)
            actual = raw & mask
            marker = "OK" if actual == expected else "MISMATCH"
            suffix = "" if mask == 0xFF else f" (raw=0x{raw:02x}, masked 0x{mask:02x})"
            print(f"  {name:10s} = 0x{actual:02x}  (expected 0x{expected:02x}){suffix} [{marker}]")
            if actual != expected:
                all_ok = False
        for name, word, kernel_wrote, note in info_checks:
            v = rfcsr_read(transport, word)
            tag = "" if kernel_wrote is None else f"(kernel wrote 0x{kernel_wrote:02x})"
            print(f"  {name:10s} = 0x{v:02x}  {tag} — INFO: {note}")
        # RFCSR38/39 RX_LO disables (normal_mode_setup_5xxx).
        rfcsr38 = rfcsr_read(transport, 38)
        rfcsr39 = rfcsr_read(transport, 39)
        print(f"  RFCSR[38].RX_LO1_EN = {bool(rfcsr38 & 0x20)}  (expected False, full=0x{rfcsr38:02x})")
        print(f"  RFCSR[39].RX_LO2_EN = {bool(rfcsr39 & 0x80)}  (expected False, full=0x{rfcsr39:02x})")
        if rfcsr38 & 0x20:
            all_ok = False
        if rfcsr39 & 0x80:
            all_ok = False
        # RFCSR30.RX_VCM = 2 (bits[4:3]).
        rfcsr30 = rfcsr_read(transport, 30)
        rx_vcm = (rfcsr30 & 0x18) >> 3
        print(f"  RFCSR[30].RX_VCM = {rx_vcm}  (expected 2, full=0x{rfcsr30:02x})")
        if rx_vcm != 2:
            all_ok = False
        if cal is not None:
            print("  NOTE: init_rfcsr returned non-None cal for RT5592 — unexpected")
            all_ok = False
    elif chip.silicon_id == 0x3572:
        # init_rfcsr_3572 table values [SRC] rt2800lib.c:7907-7937.
        # RFCSR6 has R2=1 R-M-W applied AFTER the table write of 0x4a;
        # the table writes 0x4A then OR's 0x40 → 0x4A | 0x40 = 0x4A
        # (bit 6 already set in 0x4A); same result either way: 0x4A.
        # RFCSR24/31 get overwritten by rx_filter_calibration so we
        # can't predict their exact post-init value — skip them.
        # RFCSR17_TX_LO1_EN must be cleared by normal_mode_setup_3xxx
        # → bit 3 cleared on top of table value 0x23 → 0x23 & ~0x08 = 0x23.
        # RFCSR0 bits[1:0] are hw status bits the chip sets independent
        # of what we wrote — observed 0x70 → 0x72 on AWUS051NH v2.
        # Mask them out, same pattern as RT5392's RFCSR3 VCOCAL_EN bit.
        checks = [
            ("RFCSR[0]",  0,  0x70, 0xFC),   # bits[1:0] = hw status, masked
            ("RFCSR[1]",  1,  0x81, 0xFF),
            ("RFCSR[2]",  2,  0xF1, 0xFF),
            ("RFCSR[6]",  6,  0x4A, 0xFF),    # 0x4A from table, R2 already set
            ("RFCSR[15]", 15, 0x53, 0xFF),
            ("RFCSR[20]", 20, 0xB3, 0xFF),
            ("RFCSR[29]", 29, 0x9B, 0xFF),
        ]
        for name, word, expected, mask in checks:
            raw = rfcsr_read(transport, word)
            actual = raw & mask
            marker = "OK" if actual == expected else "MISMATCH"
            suffix = "" if mask == 0xFF else f" (raw=0x{raw:02x}, masked 0x{mask:02x})"
            print(f"  {name:10s} = 0x{actual:02x}  (expected 0x{expected:02x}){suffix} [{marker}]")
            if actual != expected:
                all_ok = False
        # RFCSR17 should have TX_LO1_EN (bit 3) cleared.
        rfcsr17 = rfcsr_read(transport, 17)
        print(f"  RFCSR[17].TX_LO1_EN = {bool(rfcsr17 & 0x08)}  (expected False, full reg = 0x{rfcsr17:02x})")
        if rfcsr17 & 0x08:
            all_ok = False
        # RfFilterCal must have non-trivial values (calibration loop ran).
        if cal is not None:
            print(
                f"  rx_filter_cal: bw20=0x{cal.calibration_bw20:02x} "
                f"bw40=0x{cal.calibration_bw40:02x} "
                f"bbp25=0x{cal.bbp25:02x} bbp26=0x{cal.bbp26:02x}"
            )
            if cal.calibration_bw20 == 0x07:
                print("    NOTE: bw20 still at initial 0x07 — calibration loop may not have converged")
        else:
            print("  NOTE: init_rfcsr returned None — expected RfFilterCal for RT3572")
            all_ok = False
    else:
        # RT5392 spot-checks (unchanged).
        checks = [
            ("RFCSR[1]",  1,  0x17, 0xFF),
            ("RFCSR[3]",  3,  0x08, 0x7F),   # bit 7 auto-clears after VCO cal
            ("RFCSR[6]",  6,  0xE0, 0xFF),
            ("RFCSR[10]", 10, 0x53, 0xFF),
            ("RFCSR[33]", 33, 0xC0, 0xFF),
            ("RFCSR[56]", 56, 0xA1, 0xFF),
        ]
        for name, word, expected, mask in checks:
            actual = rfcsr_read(transport, word) & mask
            marker = "OK" if actual == expected else "MISMATCH"
            suffix = "" if mask == 0xFF else f" (masked 0x{mask:02x})"
            print(f"  {name:10s} = 0x{actual:02x}  (expected 0x{expected:02x}){suffix} [{marker}]")
            if actual != expected:
                all_ok = False
        rfcsr30 = rfcsr_read(transport, 30)
        rx_vcm = (rfcsr30 & 0x18) >> 3
        print(f"  RFCSR[30].RX_VCM = {rx_vcm}  (expected 2, full reg = 0x{rfcsr30:02x})")
        if rx_vcm != 2:
            all_ok = False
        rfcsr38 = rfcsr_read(transport, 38)
        print(f"  RFCSR[38].RX_LO1_EN = {bool(rfcsr38 & 0x20)}  (expected False, full reg = 0x{rfcsr38:02x})")
        if rfcsr38 & 0x20:
            all_ok = False

    if not all_ok:
        fail("some RFCSR spot-checks failed — RF init may not have landed")
    ok("all RFCSR spot-checks passed — M2c RF init complete")
    return cal


def phase_diag(transport: RT2800USBTransport) -> None:
    """Dump the post-init state of every register that gates RX.

    Doesn't run any bring-up — assumes you've just run --phase rx (or
    rfinit) and want to see why RX isn't delivering.
    """
    from wifit3.chips.rt2800usb.bbp import bbp_read
    from wifit3.chips.rt2800usb.constants import (
        MAC_CSR0, MAC_SYS_CTRL, PBF_SYS_CTRL, USB_DMA_CFG,
        WPDMA_GLO_CFG,
    )
    step("Critical register dump (post-init)")
    regs = [
        ("MAC_CSR0",       MAC_CSR0,       "chip ID readback (sanity)"),
        ("PBF_SYS_CTRL",   PBF_SYS_CTRL,   "bit 7=READY, bit 13=pre_init"),
        ("MAC_STATUS_CFG", 0x1200,         "bits 0-1 = BBP_RF_BUSY_TX/RX (should be 0)"),
        ("MAC_SYS_CTRL",   MAC_SYS_CTRL,   "bit 2=ENABLE_TX, bit 3=ENABLE_RX"),
        ("WPDMA_GLO_CFG",  WPDMA_GLO_CFG,  "bit 0=TX_DMA, bit 1=TX_BUSY, bit 2=RX_DMA, bit 3=RX_BUSY"),
        ("USB_DMA_CFG",    USB_DMA_CFG,    "bit 22=RX_BULK_EN, bit 23=TX_BULK_EN, bit 30=RX_BUSY"),
        ("RX_FILTER_CFG",  0x1400,         "drop flags (lower bits = various drop conditions)"),
        ("TX_PIN_CFG",     0x1328,         "LNA/PA electrical enables — bits 1,8,9,16,18"),
        ("TX_BAND_CFG",    0x132C,         "bit 2 = BG (1 = 2.4 GHz routing)"),
        ("PBF_CFG",        0x0408,         "PBF FIFO config"),
        ("INT_TIMER_CFG",  0x1128,         "should have PRE_TBTT_TIMER set"),
    ]
    for name, addr, desc in regs:
        val = transport.read32(addr)
        print(f"  {name:16s} (0x{addr:04x}) = 0x{val:08x}  — {desc}")

    step("RFCSR synth/tune registers — did channel-tune writes actually land?")
    rfcsr_regs = [
        (2,  "RFCSR2  — synth N (channel-specific; ch1=241=0xF1, ch6=243=0xF3, ch11=246=0xF6)"),
        (3,  "RFCSR3  — synth K (ch1/3/5/7/9/11/13=2; ch2/4/6/8/10/12=7; ch14=4)"),
        (5,  "RFCSR5  — R1 (bits[3:2]=1 for 2.4G, 2 for 5G)"),
        (6,  "RFCSR6  — R1+TXDIV+R2 (ch-specific R1, TXDIV=2 for 2.4G, R2=1 from init)"),
        (7,  "RFCSR7  — 2.4G branch value 0xD8 + RF_TUNING bit"),
        (8,  "RFCSR8  — AGC kick: 0 during tune, 0x80 at AGC init end"),
        (9,  "RFCSR9  — 2.4G value 0xC3"),
        (11, "RFCSR11 — 2.4G value 0xB9"),
        (17, "RFCSR17 — TX_LO1_EN bit cleared by normal_mode_setup_3xxx"),
        (23, "RFCSR23 — FREQ_OFFSET (low 7 bits, EEPROM-derived)"),
        (24, "RFCSR24 — RX filter cal value (bw20)"),
        (31, "RFCSR31 — RX_H20M cleared for BW20"),
    ]
    from wifit3.chips.rt2800usb.rfcsr import rfcsr_read as _rfcsr_read
    for word, desc in rfcsr_regs:
        val = _rfcsr_read(transport, word)
        print(f"  RFCSR[{word:3d}] = 0x{val:02x}  — {desc}")

    step("BBP RX-path registers")
    bbp_regs = [
        (3,   "BBP3   — AGC + RX antenna selector"),
        (4,   "BBP4   — MAC_IF_CTRL bit 6 + bandwidth bits"),
        (27,  "BBP27  — RX_CHAIN_SEL (which chain BBP66 fans out to)"),
        (62,  "BBP62  — noise floor low gain (we wrote 0x37-lna_gain)"),
        (63,  "BBP63  — noise floor mid gain"),
        (64,  "BBP64  — noise floor high gain"),
        (66,  "BBP66  — RX AGC"),
        (75,  "BBP75  — RX filter (RT3572 path writes 0x50 here)"),
        (82,  "BBP82  — RX filter (RT3572 path writes 0x84 here)"),
        (86,  "BBP86  — noise-floor extension (RT3572 writes 0)"),
        (105, "BBP105 — RX-path agc/mics"),
        (138, "BBP138 — DAC/ADC power-down (1T1R: bit 0 set, bit 5 set)"),
        (152, "BBP152 — RX_DEFAULT_ANT (bit 7) — RT3572 leaves 0"),
    ]
    for word, desc in bbp_regs:
        val = bbp_read(transport, word)
        print(f"  BBP[{word:3d}] = 0x{val:02x}  — {desc}")

    step("RX/TX packet counters — does the chip see frames at all?")
    from wifit3.chips.rt2800usb.constants import (
        RX_STA_CNT0, RX_STA_CNT1, RX_STA_CNT2,
        TX_STA_CNT0, TX_STA_CNT1,
    )
    counters = [
        ("RX_STA_CNT0", RX_STA_CNT0, "low=CRC_err_count, high=PHY_err_count"),
        ("RX_STA_CNT1", RX_STA_CNT1, "low=FALSE_CCA_count, high=rx_plcp_err"),
        ("RX_STA_CNT2", RX_STA_CNT2, "low=rx_dup, high=rx_overflow"),
        ("TX_STA_CNT0", TX_STA_CNT0, "tx fail / retry"),
        ("TX_STA_CNT1", TX_STA_CNT1, "tx success"),
    ]
    for name, addr, desc in counters:
        v = transport.read32(addr)
        print(f"  {name:12s} (0x{addr:04x}) = 0x{v:08x}  — {desc}")
    print("  NOTE: reading these clears them. Re-read after a short pause")
    print("        to see if they're incrementing (chip is RX-ing) or stuck")
    print("        at 0 (chip is genuinely silent / off-channel).")

    step("RX counter re-read after 1s pause")
    import time as _t
    _t.sleep(1.0)
    for name, addr, desc in counters:
        v = transport.read32(addr)
        print(f"  {name:12s} (0x{addr:04x}) = 0x{v:08x}")
    print()


def phase_rx(
    dev,
    transport: RT2800USBTransport,
    channel: int = 1,
    freq_offset_override: Optional[int] = None,
) -> None:
    """M3: full bring-up + EFUSE + enable_radio + set_channel(N) + 10s RX decode.

    ``channel`` lets the caller compare RX rates across different
    channels (1, 6, 11) to confirm the chip is actually tuning rather
    than stuck on one frequency.

    ``freq_offset_override`` overrides the EEPROM-derived freq_offset
    (used for finding the right per-chip crystal calibration when EEPROM
    is unburned — sweep 0, 10, 20, 30, 40, 50, 60 to find the value
    that gives the highest URB count).
    """
    cal = phase_rfinit(transport)

    chip = read_chip_id(transport)

    step("Read EFUSE (MAC + LNA + freq offset + path counts)")
    eeprom_buf = read_eeprom_efuse(transport)
    ee = parse_eeprom(eeprom_buf)
    mac_str = ":".join(f"{b:02x}" for b in ee.mac_address)
    print(f"  MAC          = {mac_str}")
    print(f"  lna_gain_bg  = {ee.lna_gain_bg}")
    print(f"  freq_offset  = {ee.freq_offset}")
    print(f"  nic_conf0    = 0x{ee.nic_conf0:04x}  txpath={ee.txpath} rxpath={ee.rxpath}")
    print(f"  nic_conf1    = 0x{ee.nic_conf1:04x}")
    if ee.mac_address == b"\x00" * 6 or ee.mac_address == b"\xff" * 6:
        print("  NOTE: EFUSE MAC is all 0x00 or all 0xff — chip never burned?")
    ok("EFUSE read")

    step("Enable radio (MAC TX/RX + WPDMA + USB DMA + RT3572 MCU_CURRENT)")
    enable_radio(transport, silicon_id=chip.silicon_id)
    ok("radio enabled")

    step("Program MAC into MAC_ADDR_DW0/DW1")
    write_mac_address(transport, ee.mac_address)
    ok("MAC programmed")

    effective_freq_offset = (
        freq_offset_override if freq_offset_override is not None
        else ee.freq_offset
    )
    is_2g = channel <= 14
    lna_gain = ee.lna_gain_bg if is_2g else ee.lna_gain_a
    step(
        f"Tune to channel {channel} ({'2.4G' if is_2g else '5G'}, "
        f"lna_gain={lna_gain}, freq_offset={effective_freq_offset}"
        f"{' [OVERRIDE]' if freq_offset_override is not None else ''})"
    )
    channel_kwargs = {
        "lna_gain": lna_gain,
        "freq_offset": effective_freq_offset,
    }
    if chip.silicon_id == 0x3572:
        channel_kwargs.update(
            cal_result=cal,
            tx_chain_num=ee.txpath,    # ee handles unburned-EFUSE default
            rx_chain_num=ee.rxpath,
            has_cap_bt_coexist=ee.has_cap_bt_coexist,
            has_cap_external_lna_a=ee.has_cap_external_lna_a,
        )
    elif chip.silicon_id == 0x5592:
        from wifit3.chips.rt2800usb.chan import is_xtal_40mhz
        xtal_40 = is_xtal_40mhz(transport)
        print(f"  MAC_DEBUG_INDEX.XTAL → xtal_40mhz={xtal_40}")
        channel_kwargs.update(
            tx_chain_num=ee.txpath,
            rx_chain_num=ee.rxpath,
            has_cap_bt_coexist=ee.has_cap_bt_coexist,
            has_cap_external_lna_a=ee.has_cap_external_lna_a,
            xtal_40mhz=xtal_40,
            iq_cal=ee.iq_cal,
        )
    _set_channel(transport, chip.silicon_id, channel, **channel_kwargs)
    ok(f"tuned to ch {channel}")

    rxwi_size = rxwi_size_for_silicon(chip.silicon_id)
    eps = probe_endpoints(dev)
    ep = eps.primary_bulk_in

    # Dump state before we start polling — if RX is dead, this tells us why.
    phase_diag(transport)

    step(f"Decode 10s of RX URBs (bulk-IN EP 0x{ep:02x}, RXWI={rxwi_size}B)")
    n_urbs = 0
    raw_drops = 0
    fcs_errors = 0
    rssi_samples: list[int] = []
    type_counts: dict[str, int] = {}
    bssids: set[str] = set()
    # Dump first few URBs as hex for diagnostic.
    DUMP_FIRST_N = 5
    dumped = 0

    import time as _t
    deadline = _t.perf_counter() + 10.0
    while _t.perf_counter() < deadline:
        buf = read_rx_burst(dev, ep, timeout_ms=100)
        if buf is None:
            continue
        n_urbs += 1
        if dumped < DUMP_FIRST_N:
            print(f"\n  URB #{n_urbs} raw ({len(buf)} bytes):")
            for i in range(0, min(len(buf), 96), 16):
                row = buf[i:i+16]
                hex_part = " ".join(f"{b:02x}" for b in row)
                ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                print(f"    {i:04x}: {hex_part:<48s}  {ascii_part}")
            if len(buf) > 96:
                print(f"    ... ({len(buf) - 96} more bytes)")
            dumped += 1
        rx = parse_rx_urb(buf, rxwi_size=rxwi_size)
        if rx is None:
            raw_drops += 1
            continue
        if dumped <= DUMP_FIRST_N:
            print(f"  → parsed: rx_pkt_len={getattr(rx, 'rx_pkt_len', '?')} "
                  f"mpdu_len={len(rx.mpdu)} rssi={rx.rssi_dbm} crc_err={rx.has_fcs_error}")
        if rx.has_fcs_error:
            fcs_errors += 1
            continue
        rssi_samples.append(rx.rssi_dbm)
        parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
        if parsed is None:
            continue
        ftype = parsed.type
        type_counts[ftype] = type_counts.get(ftype, 0) + 1
        bssid = parsed.bssid
        if bssid:
            bssids.add(bssid)

    print(f"\n  URBs received     = {n_urbs}")
    print(f"  Raw-parse drops   = {raw_drops}")
    print(f"  FCS-error frames  = {fcs_errors}")
    print(f"  Parsed frames     = {sum(type_counts.values())}")
    print(f"  Frame types       = {type_counts}")
    if rssi_samples:
        print(
            "  RSSI dBm          = min={} max={} mean={:.1f}".format(
                min(rssi_samples),
                max(rssi_samples),
                sum(rssi_samples) / len(rssi_samples),
            )
        )
    print(f"  Unique BSSIDs     = {len(bssids)}")
    for b in sorted(bssids)[:10]:
        print(f"    {b}")

    if sum(type_counts.values()) == 0:
        fail(
            "No parsed frames in 3s. Possible causes: chip tuned to "
            "empty channel (set_channel is M4 — chip uses RF init default "
            "which may not be channel 1), RXWI size wrong for this silicon, "
            "or descriptor math off."
        )
    ok(f"Decoded {sum(type_counts.values())} frames across {len(bssids)} BSSIDs")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--phase",
        choices=["open", "fw", "usbinit", "macinit", "bbpinit", "rfinit", "rx", "all"],
        default="all",
        help="Which phase to run (default: all available phases).",
    )
    p.add_argument("--debug", action="store_true", help="Enable DEBUG-level logging.")
    p.add_argument(
        "--channel", type=int, default=1,
        help="Channel to tune to for --phase rx (default: 1; useful for "
             "comparing rates across channels 1/6/11 when debugging RT3572).",
    )
    p.add_argument(
        "--freq-offset", type=int, default=None,
        help="Override EFUSE-derived freq_offset for --phase rx. Useful for "
             "RT3572 dongles with unburned EFUSE: sweep 0/10/20/30/40/50/60 "
             "and find the value that maximises URB count (chip's crystal "
             "calibration without EFUSE help).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.debug)

    dev = open_device()
    transport = RT2800USBTransport(dev)
    try:
        if args.phase == "open":
            phase_open(transport)
        elif args.phase == "fw":
            phase_open(transport)
            phase_fw(transport)
        elif args.phase == "usbinit":
            phase_open(transport)
            phase_usbinit(transport)
        elif args.phase == "macinit":
            phase_open(transport)
            phase_macinit(transport)
        elif args.phase == "bbpinit":
            phase_open(transport)
            phase_bbpinit(transport)
        elif args.phase == "rfinit":
            phase_open(transport)
            phase_rfinit(transport)
        elif args.phase in ("rx", "all"):
            phase_open(transport)
            phase_rx(dev, transport, channel=args.channel,
                     freq_offset_override=args.freq_offset)
        print("\n=== phases all PASSED ===")
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  USB release warning: {e}")


if __name__ == "__main__":
    main()
