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
from wifit3.chips.rtw88_8814au import chan, rf as rf8814
from wifit3.chips.rtw88_8814au.constants import RF_RCK1_V1
from wifit3.chips.rtw88_8814au.efuse import read_efuse
from wifit3.chips.rtw88_8814au.phy import (
    EfuseDefaults,
    defaults_from_efuse,
    load_mac_table,
    phy_set_param,
)
from wifit3.chips.rtw88_8814au import rx as rx8814
from wifit3.chips.rtw88_8814au.transport import RTL8814AUTransport
from wifit3.wlan.packet import WlanFrameParser
import dataclasses


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def step(label: str) -> None:
    print(f"\n--- {label} ---")


_CR_TRAIL: list[tuple[str, int]] = []


def cr_checkpoint(transport, label: str) -> None:
    """Bisect helper: read+record REG_CR. 0xEA(EAEAEAEA) = MAC powered off."""
    cr = transport.read32(REG_CR)
    _CR_TRAIL.append((label, cr))
    state = "MAC OFF (0xEA)" if cr == 0xEAEAEAEA or (cr & 0xFF) == 0xEA else "alive"
    print(f"  [CR-CHECK after {label}] REG_CR=0x{cr:08x}  -> {state}")


def print_cr_trail() -> None:
    """Dump the whole CR bisect trail together (survives scrollback)."""
    print("\n=== CR bisect trail (first 'MAC OFF' names the culprit) ===")
    for label, cr in _CR_TRAIL:
        state = "MAC OFF" if cr == 0xEAEAEAEA or (cr & 0xFF) == 0xEA else "alive"
        print(f"    after {label:24s}: REG_CR=0x{cr:08x}  {state}")


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
    cr_checkpoint(transport, "M2 mac_init")


# MAC-table register read-backs (addr -> expected byte), picked from
# rtw8814a_mac[] — timing regs not touched by M1/M2, so a clean walker proof.
_MAC_TBL_CHECKS = [
    (0x428, 0x0A), (0x429, 0x10), (0x434, 0x04),
    (0x435, 0x05), (0x436, 0x07), (0x437, 0x08),
]


def phase_tables(transport: RTL8814AUTransport) -> None:
    step("Replay MAC init table via phy_cond walker  [M3.a GATE]")
    t0 = time.perf_counter()
    try:
        n = load_mac_table(transport, EfuseDefaults())
    except (IOError, ValueError) as e:
        fail(f"load_mac_table failed: {e}")
    dt = (time.perf_counter() - t0) * 1000
    print(f"  MAC table: {n} register writes in {dt:.0f} ms")

    step("Read back sample MAC registers (must match table values)")
    bad = 0
    for addr, expect in _MAC_TBL_CHECKS:
        got = transport.read8(addr)
        flag = "ok" if got == expect else "MISMATCH"
        if got != expect:
            bad += 1
        print(f"    0x{addr:03x}: wrote 0x{expect:02x}, read 0x{got:02x}  [{flag}]")
    if bad:
        fail(f"{bad}/{len(_MAC_TBL_CHECKS)} MAC reg read-backs mismatched — "
             "table walker or write path is wrong")
    ok("MAC table replayed and read back correctly. M3.a COMPLETE.")


def phase_efuse(transport: RTL8814AUTransport):
    step("Read EFUSE (rfe_option, MAC, crystal_cap, board option)  [M4 GATE]")
    t0 = time.perf_counter()
    try:
        er = read_efuse(transport)
    except IOError as e:
        fail(f"read_efuse failed: {e}")
    dt = (time.perf_counter() - t0) * 1000
    mac = ":".join(f"{b:02x}" for b in er.mac_addr)
    print(f"  rfe_option   = {er.rfe_option} (raw 0x{er.rfe_option_raw:02x})")
    print(f"  rf_board_opt = 0x{er.rf_board_option:02x}")
    print(f"  trx_antenna  = 0x{er.trx_antenna_option:02x}")
    print(f"  channel_plan = 0x{er.channel_plan:02x}")
    print(f"  crystal_cap  = 0x{er.crystal_cap:02x}")
    print(f"  MAC address  = {mac}")
    print(f"  (read in {dt:.0f} ms)")
    if er.mac_addr in (b"\x00" * 6, b"\xff" * 6):
        fail("MAC address is all-zero/all-FF — EFUSE read or de-map is wrong")
    if er.rfe_option_raw == 0xFF:
        fail("rfe_option raw is 0xFF — EFUSE likely not read (blank logical map)")
    ok(f"EFUSE decoded: rfe_option={er.rfe_option}, MAC {mac}. M4 COMPLETE.")
    cr_checkpoint(transport, "M4 efuse read")
    return er


def phase_phy(transport: RTL8814AUTransport) -> None:
    step("phy_set_param: BB/RF enable + BB/AGC/RF(A-D) tables + RCK  [M3.b GATE]")
    cut = (transport.read32(REG_SYS_CFG1) >> 12) & 0xF
    try:
        er = read_efuse(transport)
        efuse = defaults_from_efuse(er, cut=cut)
    except IOError:
        efuse = dataclasses.replace(EfuseDefaults(), cut=cut)
    print(f"  cut={cut} (CUT_{'ABCDEFG'[cut] if cut < 7 else '?'}), "
          f"rfe_option={efuse.rfe_option} (from EFUSE)")
    t0 = time.perf_counter()
    try:
        phy_set_param(transport, efuse)
    except (IOError, ValueError) as e:
        fail(f"phy_set_param failed: {e}")
    dt = (time.perf_counter() - t0) * 1000
    ok(f"phy_set_param completed in {dt:.0f} ms")

    step("Read back RF_RCK1_V1 on all 4 paths (A=B=C=D after RCK copy, non-garbage)")
    vals = []
    for path in range(4):
        v = rf8814.read_rf(transport, path, RF_RCK1_V1, rf8814.RFREG_MASK)
        vals.append(v)
        print(f"    path {'ABCD'[path]}: RF[0x1c] = 0x{v:05x}")
    if any(v in (0x00000, 0xFFFFF) for v in vals):
        fail("an RF path read back 0x00000/0xfffff — RF not responding "
             "(suspect wrong rfe_option or BB/RF not powered)")
    if len(set(vals)) != 1:
        fail(f"RF_RCK1_V1 differs across paths {[hex(v) for v in vals]} — "
             "RCK copy or per-path RF access is wrong")
    ok(f"all 4 RF paths read back 0x{vals[0]:05x} (consistent, non-garbage). "
       "M3.b COMPLETE.")
    cr_checkpoint(transport, "M3.b phy_set_param")


def phase_channel(transport: RTL8814AUTransport) -> None:
    step("Channel tune: ch1 / ch36 / ch149 execute cleanly across bands  [M3.c GATE]")
    # NOTE: this chip's channel/PHY config registers are write-and-forget — the
    # kernel never reads them back (confirmed against the cold-boot pcap), and
    # several (RF_CFGCH, CCK_CHECK, CLKTRK, AGC_TABLE) don't read back as
    # written. The captures are also init-only, so there's no on-air ground
    # truth. So this gate confirms the tune SEQUENCE runs cleanly across 2G ->
    # 5G -> 5G-high; the functional proof (RF actually receiving) is M5 RX.
    rfe = 1  # confirmed from EFUSE in M4
    for idx, ch in enumerate([1, 36, 149]):
        try:
            chan.set_channel(transport, ch, rfe_option=rfe, force_band=(idx == 0))
        except (IOError, ValueError, NotImplementedError) as ex:
            fail(f"set_channel({ch}) failed: {ex}")
        band = "5G" if ch > 14 else "2G"
        print(f"  ch{ch:3d} ({band}): tune sequence executed OK")
    ok("ch1/36/149 tuned cleanly across 2G/5G/5G-high without error. "
       "M3.c sequence COMPLETE (RF on-air validation in M5).")
    cr_checkpoint(transport, "M3.c channel tune")


def _extract_ssid(mpdu: bytes) -> str:
    if len(mpdu) < 38 or mpdu[24 + 12] != 0:
        return ""
    ies_off = 24 + 12
    slen = mpdu[ies_off + 1]
    if ies_off + 2 + slen > len(mpdu):
        return ""
    try:
        return mpdu[ies_off + 2: ies_off + 2 + slen].decode("utf-8")
    except UnicodeDecodeError:
        return mpdu[ies_off + 2: ies_off + 2 + slen].hex()


def phase_rx(dev, transport: RTL8814AUTransport) -> None:
    step("MAC power/register sanity before RX")
    cr = transport.read32(REG_CR)
    rcr_pre = transport.read32(0x0608)
    sfe = transport.read16(0x0002)
    cr_checkpoint(transport, "M5 rx entry")
    print(f"  REG_CR=0x{cr:08x}  REG_RCR(pre)=0x{rcr_pre:08x}  SYS_FUNC_EN=0x{sfe:04x}")
    if (cr & 0xFF) == 0xEA or cr == 0xEAEAEAEA:
        print_cr_trail()
        fail("REG_CR reads 0xEA — the MAC is powered OFF/reset by the time RX "
             "starts. The CR trail above names the culprit phase.")
    if (cr & 0xFF) != 0xFF:
        print(f"  WARN: REG_CR low byte 0x{cr & 0xFF:02x} != MAC_TRX_ENABLE(0xff) "
              "— MAC TRX not fully enabled")

    step("RX monitor init + listen for beacons on ch1/ch6  [M5 GATE]")
    try:
        rx8814.mac_init_for_rx(transport)
        rx8814.apply_monitor_rcr(transport)
    except IOError as e:
        fail(f"mac_init_for_rx failed: {e}")
    eps = rx8814.probe_endpoints(dev)
    if not eps.bulk_in:
        fail("no bulk-IN endpoint found")
    ep_in = eps.primary_bulk_in
    print(f"  bulk-IN endpoint: 0x{ep_in:02x}")

    seen_bssids: dict[str, int] = {}
    seen_ssids: dict[str, str] = {}
    total_frames = bursts = bytes_rx = 0

    for ch in (1, 6, 11):
        chan.set_channel(transport, ch, rfe_option=1)
        t_end = time.perf_counter() + 3.0
        while time.perf_counter() < t_end:
            buf = rx8814.read_rx_burst(dev, ep_in, max_size=16384, timeout_ms=200)
            if buf is None:
                continue
            bursts += 1
            bytes_rx += len(buf)
            for _stat, mpdu, rssi in rx8814.iter_bulk_frames(buf):
                total_frames += 1
                parsed = WlanFrameParser.parse_80211_frame(
                    mpdu, rssi if rssi is not None else -100)
                if parsed and parsed.get("subtype_id") == WlanFrameParser.SUBTYPE_BEACON:
                    bssid = parsed.get("bssid") or "?"
                    seen_bssids[bssid] = seen_bssids.get(bssid, 0) + 1
                    seen_ssids.setdefault(bssid, _extract_ssid(mpdu))

    print(f"  bursts={bursts} bytes={bytes_rx} parsed_frames={total_frames}")
    print(f"  distinct beacon BSSIDs: {len(seen_bssids)}")
    for bssid, count in sorted(seen_bssids.items(), key=lambda kv: -kv[1])[:15]:
        print(f"    {bssid}  ({count:3d})  ssid={seen_ssids.get(bssid, '')!r}")
    if not seen_bssids:
        fail("no beacons captured — RX path not delivering 802.11 frames "
             "(or no APs in range). Confirm APs are nearby and retry.")
    ok(f"{len(seen_bssids)} BSSIDs visible across ch1/6/11 — RX WORKS. "
       "M5 COMPLETE (this also confirms M3.c tune on-air).")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--phase",
        choices=("open", "fw", "validate", "mac_init", "tables", "efuse", "phy",
                 "channel", "rx", "all"),
        default="all")
    p.add_argument("--debug", action="store_true", help="verbose USB logging")
    args = p.parse_args()
    setup_logging(args.debug)

    step("USB discovery + claim")
    dev = open_device()
    ok("Interface 0 claimed")
    transport = RTL8814AUTransport(dev)

    # Ordered bring-up chain: running phase X runs every phase up to and
    # including X (so prerequisites can never be silently skipped). "tables"
    # (M3.a) is a standalone smoke that needs M1/M2 but not efuse/phy.
    runners = {
        "open": lambda: phase_open(transport),
        "fw": lambda: phase_fw(dev, transport),
        "validate": lambda: phase_validate(transport),
        "mac_init": lambda: phase_mac_init(dev, transport),
        "efuse": lambda: phase_efuse(transport),
        "phy": lambda: phase_phy(transport),
        "channel": lambda: phase_channel(transport),
        "rx": lambda: phase_rx(dev, transport),
    }
    chain = ["open", "fw", "validate", "mac_init", "efuse", "phy", "channel", "rx"]
    target = "rx" if args.phase == "all" else args.phase

    try:
        if target == "tables":
            for ph in ("open", "fw", "validate", "mac_init"):
                runners[ph]()
            phase_tables(transport)
        else:
            for ph in chain[:chain.index(target) + 1]:
                runners[ph]()
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
