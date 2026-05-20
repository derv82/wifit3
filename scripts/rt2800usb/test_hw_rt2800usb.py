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
    uv run python scripts/rt2800usb/test_hw_rt2800usb.py                  # all
    uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase open     # M1
    uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase fw       # M2a
    uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase usbinit  # M2b-1
    uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase macinit  # M2b-2
    uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase bbpinit  # M2b-3
    uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase rfinit   # M2c
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

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
from wifit3.chips.rt2800usb.bbp import bbp_read, init_bbp_53xx, prepare_bbp
from wifit3.chips.rt2800usb.reg_init import init_registers
from wifit3.chips.rt2800usb.rfcsr import init_rfcsr, rfcsr_read
from wifit3.chips.rt2800usb.rx import (
    parse_rx_urb,
    probe_endpoints,
    read_rx_burst,
    rxwi_size_for_silicon,
)
from wifit3.wlan.packet import WlanFrameParser
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
    """M2b-3: run init_bbp_53xx via BBP_CSR_CFG indirect protocol,
    then spot-check BBP[4] has the MAC_IF_CTRL bit set."""
    phase_macinit(transport)

    step("Prepare BBP (wait_bbp_rf_ready + MCU_BOOT_SIGNAL + wait_bbp_ready)")
    try:
        prepare_bbp(transport)
    except (IOError, usb.core.USBError) as e:
        fail(f"prepare_bbp raised: {e}")
    ok("BBP prepared")

    step("Run init_bbp_53xx (~30 BBP register writes)")
    chip = read_chip_id(transport)
    import time as _t
    t0 = _t.perf_counter()
    try:
        init_bbp_53xx(transport, chip.silicon_id)
    except (IOError, usb.core.USBError, ValueError) as e:
        fail(f"init_bbp_53xx raised: {e}")
    dt = _t.perf_counter() - t0
    ok(f"init_bbp_53xx completed in {dt * 1000:.0f} ms")

    step("Spot-check BBP register values")
    # BBP[4] should have MAC_IF_CTRL bit (0x40) set after bbp4_mac_if_ctrl
    bbp4 = bbp_read(transport, 4)
    print(f"  BBP[4]   = 0x{bbp4:02x}  (need bit 6 = 0x40 set)")
    # BBP[31] should be 0x08 (a direct write near top of init_bbp_53xx)
    bbp31 = bbp_read(transport, 31)
    print(f"  BBP[31]  = 0x{bbp31:02x}  (expected 0x08)")
    # BBP[65] should be 0x2C
    bbp65 = bbp_read(transport, 65)
    print(f"  BBP[65]  = 0x{bbp65:02x}  (expected 0x2c)")
    # BBP[106] — depends on silicon: 0x12 for RT5392, 0x03 for RT5390
    bbp106 = bbp_read(transport, 106)
    expected_106 = 0x12 if chip.silicon_id == 0x5392 else 0x03
    print(f"  BBP[106] = 0x{bbp106:02x}  (expected 0x{expected_106:02x} for {chip.name})")
    # BBP[142] should be 1 (from init_freq_calibration)
    bbp142 = bbp_read(transport, 142)
    print(f"  BBP[142] = 0x{bbp142:02x}  (expected 0x01)")

    all_ok = (
        (bbp4 & 0x40) == 0x40
        and bbp31 == 0x08
        and bbp65 == 0x2C
        and bbp106 == expected_106
        and bbp142 == 0x01
    )
    if not all_ok:
        fail("one or more BBP spot-checks failed — init_bbp_53xx may not have landed cleanly")
    ok("all BBP spot-checks passed — baseband init complete")


def phase_rfinit(transport: RT2800USBTransport) -> None:
    """M2c: run init_rfcsr_5392 + spot-check a few RFCSR values landed."""
    phase_bbpinit(transport)

    step("Run init_rfcsr (RF chain init for RT5392)")
    chip = read_chip_id(transport)
    import time as _t
    t0 = _t.perf_counter()
    try:
        init_rfcsr(transport, chip.silicon_id)
    except (IOError, usb.core.USBError, NotImplementedError) as e:
        fail(f"init_rfcsr raised: {e}")
    dt = _t.perf_counter() - t0
    ok(f"init_rfcsr completed in {dt * 1000:.0f} ms")

    step("Spot-check RFCSR values")
    # NB: RFCSR[3] bit 7 = VCOCAL_EN is auto-cleared by HW once the
    # VCO calibration triggered by the write completes. Kernel writes
    # 0x88 but a post-write readback shows 0x08 — we mask the bit out.
    checks = [
        ("RFCSR[1]",  1,  0x17, 0xFF),
        ("RFCSR[3]",  3,  0x08, 0x7F),   # bit 7 auto-clears after VCO cal
        ("RFCSR[6]",  6,  0xE0, 0xFF),
        ("RFCSR[10]", 10, 0x53, 0xFF),
        ("RFCSR[33]", 33, 0xC0, 0xFF),
        ("RFCSR[56]", 56, 0xA1, 0xFF),
    ]
    all_ok = True
    for name, word, expected, mask in checks:
        actual = rfcsr_read(transport, word) & mask
        marker = "OK" if actual == expected else "MISMATCH"
        suffix = "" if mask == 0xFF else f" (masked 0x{mask:02x})"
        print(f"  {name:10s} = 0x{actual:02x}  (expected 0x{expected:02x}){suffix} [{marker}]")
        if actual != expected:
            all_ok = False

    # Verify the RFCSR30 RX_VCM=2 write from normal_mode_setup_5xxx landed.
    # RFCSR30 has multiple bit-fields — check just the RX_VCM nibble.
    rfcsr30 = rfcsr_read(transport, 30)
    rx_vcm = (rfcsr30 & 0x18) >> 3
    print(f"  RFCSR[30].RX_VCM = {rx_vcm}  (expected 2, full reg = 0x{rfcsr30:02x})")
    if rx_vcm != 2:
        all_ok = False

    # Verify RFCSR38 RX_LO1_EN cleared.
    rfcsr38 = rfcsr_read(transport, 38)
    print(f"  RFCSR[38].RX_LO1_EN = {bool(rfcsr38 & 0x20)}  (expected False, full reg = 0x{rfcsr38:02x})")
    if rfcsr38 & 0x20:
        all_ok = False

    if not all_ok:
        fail("some RFCSR spot-checks failed — RF init may not have landed")
    ok("all RFCSR spot-checks passed — M2c RF init complete")


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

    step("BBP RX-path registers")
    bbp_regs = [
        (3,   "BBP3   — AGC + RX antenna selector"),
        (4,   "BBP4   — MAC_IF_CTRL bit 6 + bandwidth bits"),
        (62,  "BBP62  — noise floor low gain (we wrote 0x37)"),
        (63,  "BBP63  — noise floor mid gain (we wrote 0x37)"),
        (64,  "BBP64  — noise floor high gain (we wrote 0x37)"),
        (66,  "BBP66  — RX AGC"),
        (105, "BBP105 — RX-path agc/mics"),
        (138, "BBP138 — DAC/ADC power-down (kernel reads EEPROM)"),
        (152, "BBP152 — RX_DEFAULT_ANT (bit 7) — should be 1"),
    ]
    for word, desc in bbp_regs:
        val = bbp_read(transport, word)
        print(f"  BBP[{word:3d}] = 0x{val:02x}  — {desc}")
    print()


def phase_rx(dev, transport: RT2800USBTransport) -> None:
    """M3: full bring-up + EFUSE + enable_radio + set_channel(1) + 3s RX decode."""
    phase_rfinit(transport)

    chip = read_chip_id(transport)

    step("Read EFUSE (MAC + LNA + freq offset)")
    eeprom_buf = read_eeprom_efuse(transport)
    ee = parse_eeprom(eeprom_buf)
    mac_str = ":".join(f"{b:02x}" for b in ee.mac_address)
    print(f"  MAC          = {mac_str}")
    print(f"  lna_gain_bg  = {ee.lna_gain_bg}")
    print(f"  freq_offset  = {ee.freq_offset}")
    print(f"  nic_conf0    = 0x{ee.nic_conf0:04x}")
    print(f"  nic_conf1    = 0x{ee.nic_conf1:04x}")
    if ee.mac_address == b"\x00" * 6 or ee.mac_address == b"\xff" * 6:
        print("  NOTE: EFUSE MAC is all 0x00 or all 0xff — chip never burned?")
    ok("EFUSE read")

    step("Enable radio (MAC TX/RX + WPDMA + USB DMA bulk endpoints)")
    enable_radio(transport)
    ok("radio enabled")

    step("Program MAC into MAC_ADDR_DW0/DW1")
    write_mac_address(transport, ee.mac_address)
    ok("MAC programmed")

    step("Tune to channel 1 (with real EEPROM lna_gain + freq_offset)")
    _set_channel(transport, chip.silicon_id, 1,
                 lna_gain=ee.lna_gain_bg, freq_offset=ee.freq_offset)
    ok("tuned")

    rxwi_size = rxwi_size_for_silicon(chip.silicon_id)
    eps = probe_endpoints(dev)
    ep = eps.primary_bulk_in

    # Dump state before we start polling — if RX is dead, this tells us why.
    phase_diag(transport)

    step(f"Decode 3s of RX URBs (bulk-IN EP 0x{ep:02x}, RXWI={rxwi_size}B)")
    n_urbs = 0
    raw_drops = 0
    fcs_errors = 0
    rssi_samples: list[int] = []
    type_counts: dict[str, int] = {}
    bssids: set[str] = set()

    import time as _t
    deadline = _t.perf_counter() + 3.0
    while _t.perf_counter() < deadline:
        buf = read_rx_burst(dev, ep, timeout_ms=100)
        if buf is None:
            continue
        n_urbs += 1
        rx = parse_rx_urb(buf, rxwi_size=rxwi_size)
        if rx is None:
            raw_drops += 1
            continue
        if rx.has_fcs_error:
            fcs_errors += 1
            continue
        rssi_samples.append(rx.rssi_dbm)
        parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
        if parsed is None:
            continue
        ftype = parsed.get("type", "?")
        type_counts[ftype] = type_counts.get(ftype, 0) + 1
        bssid = parsed.get("bssid")
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
            phase_rx(dev, transport)
        print("\n=== phases all PASSED ===")
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  USB release warning: {e}")


if __name__ == "__main__":
    main()
