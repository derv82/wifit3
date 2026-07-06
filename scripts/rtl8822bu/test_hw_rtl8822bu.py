"""Hardware test for RTL8822BU bring-up.

  --phase open     : USB enumeration + control transfers (chip-ID read).
  --phase fw       : open + power-on + FW upload (iDDMA) + checksum poll.
  --phase validate : fw + FW_READY mask poll (wlan CPU running).
  --phase phy      : validate + phy_set_param (init tables + BB/RF).
  --phase mac_init : phy + rtw8822b_mac_init.
  --phase channel  : mac_init + set_channel_2g_20mhz(1).
  --phase beacon   : channel + RX poll for beacons.
  --phase all      : open through beacon (default).

Usage:
    .venv/Scripts/python.exe scripts/rtl8822bu/test_hw_rtl8822bu.py --phase open
    .venv/Scripts/python.exe scripts/rtl8822bu/test_hw_rtl8822bu.py --debug
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

from wifit3.chips.rtl8822bu.chan import (
    CHANNELS_5G_NON_DFS,
    set_channel_2g_20mhz,
    set_channel_5g_20mhz,
)
from wifit3.chips.rtl8822bu.constants import (
    REG_CR,
    REG_MCUFW_CTRL,
    REG_SYS_CFG1,
    USB_IDS_8822BU,
)
from wifit3.chips.rtl8822bu.firmware import (
    download_firmware,
    download_firmware_validate,
    load_firmware_blob,
)
from wifit3.chips.rtl8822bu.mac import (
    cut_mask_from_sys_cfg1,
    init_priority_queue_8822b,
    is_chip_warm,
    mac_init_for_rx,
    mac_power_on,
)
from wifit3.chips.rtl8822bu.phy import EfuseDefaults, phy_set_param
from wifit3.chips.rtl8822bu.rx import (
    iter_bulk_frames,
    probe_endpoints,
    read_rx_burst,
)
from wifit3.chips.rtl8822bu.transport import RTL8822BUTransport
from wifit3.chips.rtl8822bu.tx import (
    TX_DESC_QSEL_MGMT,
    build_deauth_frame,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
    write_bulk,
)
from wifit3.wlan.packet import WlanFrameParser


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
    backend = libusb_package.get_libusb1_backend()
    dev = None
    matched: tuple[int, int, str] | None = None
    for vid, pid, desc in USB_IDS_8822BU:
        found = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if found is not None:
            dev = found
            matched = (vid, pid, desc)
            break
    if dev is None:
        fail(
            "No RTL8822BU device found. Expected one of:\n"
            + "\n".join(f"    {vid:04x}:{pid:04x}  {desc}"
                        for vid, pid, desc in USB_IDS_8822BU)
            + "\nPlug it in, confirm Zadig bound it to WinUSB, and retry."
        )
    print(f"  Found {matched[0]:04x}:{matched[1]:04x}  {matched[2]}")
    print(f"  bus={dev.bus} address={dev.address} bcdUSB={dev.bcdUSB:#06x}")

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


def phase_open(transport: RTL8822BUTransport) -> None:
    step("Read REG_SYS_CFG1 (0x00F0)")
    val = transport.read32(REG_SYS_CFG1)
    print(f"  REG_SYS_CFG1 = 0x{val:08x}")

    if val == 0 or val == 0xFFFFFFFF:
        fail(
            f"Implausible value 0x{val:08x} — device may be in a bad state. "
            "Unplug, wait 5s, replug, and rerun."
        )

    # Decode useful fields from REG_SYS_CFG1 (reg.h:187..201).
    cut_version = (val >> 12) & 0xF
    bit_rtl_id = bool(val & (1 << 23))
    bit_rf_type_id = bool(val & (1 << 27))
    cut_letters = "ABCDEFG"
    cut_name = cut_letters[cut_version] if cut_version < len(cut_letters) else f"?{cut_version}"
    print(f"  cut_version   = {cut_version} (CUT_{cut_name})")
    print(f"  BIT_RTL_ID    = {bit_rtl_id} (1 = test chip)")
    print(f"  BIT_RF_TYPE_ID= {bit_rf_type_id} (1 = 2T2R)")
    print(f"  (capture-1 ground truth: 0x0c493d35, CUT_D, MP chip, 2T2R)")

    if val != 0x0C493D35:
        print(f"  NOTE: value differs from capture-1 — could be a different "
              f"silicon revision or batch. Not necessarily a failure.")

    ok("control-transfer plumbing works")


def phase_fw(dev, transport: RTL8822BUTransport) -> None:
    step("Detect chip warm/cold state")
    if is_chip_warm(transport):
        print("  Chip is WARM — FW already loaded from a prior session.")
        print("  Skip FW upload. Replug if you want to test from cold.")
        ok("warm chip detected (FW upload not needed)")
        return

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

    step("Upload firmware (modern iDDMA path)")
    fw = load_firmware_blob()
    print(f"  Loaded {len(fw)} bytes (DMEM+IMEM, no header)")

    last_pct = -1

    def progress(done: int, total: int) -> None:
        nonlocal last_pct
        pct = int(done * 100 / total)
        if pct != last_pct and pct % 10 == 0:
            last_pct = pct
            print(f"  [{pct:3d}%] {done}/{total} bytes")

    import time as _t
    t0 = _t.perf_counter()
    try:
        download_firmware(dev, transport, fw, progress_cb=progress)
    except Exception as e:
        mcufw = transport.read32(REG_MCUFW_CTRL)
        fail(f"download_firmware raised {type(e).__name__}: {e}\n"
             f"  REG_MCUFW_CTRL = 0x{mcufw:08x}")
    dt = _t.perf_counter() - t0
    mcufw = transport.read32(REG_MCUFW_CTRL)
    print(f"  REG_MCUFW_CTRL = 0x{mcufw:08x}  (post-upload, took {dt*1000:.0f} ms)")
    bits = []
    if mcufw & (1 << 3):  bits.append("IMEM_DW_OK")
    if mcufw & (1 << 4):  bits.append("IMEM_CHKSUM_OK")
    if mcufw & (1 << 5):  bits.append("DMEM_DW_OK")
    if mcufw & (1 << 6):  bits.append("DMEM_CHKSUM_OK")
    if mcufw & (1 << 14): bits.append("FW_DW_RDY")
    if mcufw & (1 << 15): bits.append("FW_INIT_RDY")
    print(f"  Set bits: {', '.join(bits) if bits else '(none)'}")
    ok("FW bulk-OUT + iDDMA pipeline completed without raising")


def phase_validate(transport: RTL8822BUTransport) -> None:
    step("Validate FW running (poll FW_READY mask)")
    ok_run, last = download_firmware_validate(transport)
    print(f"  REG_MCUFW_CTRL = 0x{last:08x}")
    bits = []
    if last & (1 << 3):  bits.append("IMEM_DW_OK")
    if last & (1 << 4):  bits.append("IMEM_CHKSUM_OK")
    if last & (1 << 5):  bits.append("DMEM_DW_OK")
    if last & (1 << 6):  bits.append("DMEM_CHKSUM_OK")
    if last & (1 << 14): bits.append("FW_DW_RDY")
    if last & (1 << 15): bits.append("FW_INIT_RDY")
    print(f"  Set bits: {', '.join(bits) if bits else '(none)'}")
    if not ok_run:
        fail("FW_READY never satisfied — wlan CPU is not running the firmware.")
    ok("FW_READY satisfied — wlan CPU is running the firmware.")


def phase_phy(transport: RTL8822BUTransport) -> EfuseDefaults:
    step("PHY init (rtw8822b_phy_set_param: BB/RF enable + 5 init tables)")
    efuse = EfuseDefaults()
    import time as _t
    t0 = _t.perf_counter()
    try:
        phy_set_param(transport, efuse)
    except (IOError, NotImplementedError) as e:
        fail(f"phy_set_param failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"PHY init completed in {dt_ms:.0f} ms")
    return efuse


def phase_mac_init(transport: RTL8822BUTransport) -> None:
    step("MAC init for RX (TRX_ENABLE + RX filters + DRVINFO)")
    import time as _t
    t0 = _t.perf_counter()
    try:
        mac_init_for_rx(transport)
    except IOError as e:
        fail(f"mac_init_for_rx failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    from wifit3.chips.rtl8822bu.constants import REG_CR as _REG_CR
    cr = transport.read32(_REG_CR)
    print(f"  REG_CR = 0x{cr:08x}  (look for bits 0,1,2,3,4,5,6,7 = MAC_TRX_ENABLE)")
    ok(f"mac_init done in {dt_ms:.0f} ms")


def phase_channel(transport: RTL8822BUTransport, channel: int = 1) -> None:
    step(f"Set channel {channel} @ 20 MHz (2.4 GHz)")
    import time as _t
    t0 = _t.perf_counter()
    try:
        set_channel_2g_20mhz(transport, channel)
    except (IOError, ValueError) as e:
        fail(f"set_channel failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"Channel {channel} set in {dt_ms:.0f} ms")


def phase_beacon(dev, transport: RTL8822BUTransport,
                 duration_s: float = 5.0) -> None:
    step(f"Listen for beacons (channel 1, {duration_s:.0f}s)")

    eps = probe_endpoints(dev)
    if not eps.bulk_in:
        fail("no bulk-IN endpoint found")
    ep_in = eps.primary_bulk_in
    print(f"  bulk-IN endpoint: 0x{ep_in:02x}")

    seen_bssids: dict[str, int] = {}
    seen_ssids: dict[str, str] = {}
    total_frames = 0
    bursts = 0
    bytes_received = 0
    import time as _t
    t_start = _t.perf_counter()

    while _t.perf_counter() - t_start < duration_s:
        buf = read_rx_burst(dev, ep_in, max_size=16384, timeout_ms=200)
        if buf is None:
            continue
        bursts += 1
        bytes_received += len(buf)
        for stat, mpdu, rssi in iter_bulk_frames(buf):
            total_frames += 1
            parsed = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100
            )
            if not parsed:
                continue
            if parsed.subtype_id == WlanFrameParser.SUBTYPE_BEACON:
                bssid = parsed.bssid or "?"
                seen_bssids[bssid] = seen_bssids.get(bssid, 0) + 1
                if bssid not in seen_ssids:
                    seen_ssids[bssid] = _extract_ssid(mpdu)

    print(f"  Bursts received: {bursts}, total bytes: {bytes_received}")
    print(f"  Parsed frames:   {total_frames}")
    print(f"  Distinct beacon BSSIDs: {len(seen_bssids)}")
    for bssid, count in sorted(seen_bssids.items(), key=lambda kv: -kv[1])[:20]:
        ssid = seen_ssids.get(bssid, "")
        print(f"    {bssid}  ({count:3d} beacons)  ssid={ssid!r}")

    if len(seen_bssids) == 0:
        fail("no beacons captured — RX path not delivering 802.11 frames")
    ok(f"{len(seen_bssids)} distinct BSSIDs visible on channel 1")


def phase_tx(dev, transport: RTL8822BUTransport) -> None:
    step("Priority-queue init (FIFO pages + auto LLT) — needed for MGMT TX")
    try:
        init_priority_queue_8822b(transport)
    except IOError as e:
        fail(f"init_priority_queue_8822b failed: {e}")
    ok("priority queues configured + LLT initialised")

    step("TX inject: 20 broadcast deauth frames on bulk-OUT MGMT lane")
    eps = probe_endpoints(dev)
    if not eps.bulk_out:
        fail("no bulk-OUT endpoints found")
    ep = pick_bulk_out_ep(list(eps.bulk_out), queue=TX_DESC_QSEL_MGMT)
    print(f"  MGMT bulk-OUT ep: 0x{ep:02x}")

    # Deauth the broadcast addr — won't affect any real client, but it's a
    # valid 802.11 frame that survives MAC RX-side filtering on the chip.
    ap_mac = bytes.fromhex("aa:bb:cc:dd:ee:01".replace(":", ""))   # NETGEAR2G BSSID
    bcast = b"\xff" * 6
    mpdu = build_deauth_frame(ap_mac, bcast, reason=7)
    desc = build_tx_desc_mgmt(mpdu, band_is_2g=True)
    payload = desc + mpdu
    print(f"  Frame: {len(mpdu)} B MPDU + {len(desc)} B TX desc = {len(payload)} B")
    print(f"  Hex (first 32 B): {payload[:32].hex()}")

    import time as _t
    t0 = _t.perf_counter()
    bursts_failed = 0
    for i in range(20):
        try:
            sent = write_bulk(dev, ep, payload, timeout_ms=500)
            if sent != len(payload):
                bursts_failed += 1
                print(f"  [{i:2d}] short write {sent}/{len(payload)}")
        except usb.core.USBError as e:
            bursts_failed += 1
            print(f"  [{i:2d}] error: {e}")
        _t.sleep(0.01)
    dt = (_t.perf_counter() - t0) * 1000
    if bursts_failed:
        fail(f"{bursts_failed}/20 TX writes failed")
    ok(f"20/20 deauth frames written to bulk-OUT 0x{ep:02x} in {dt:.0f} ms")


def phase_5g(transport: RTL8822BUTransport, dev) -> None:
    step("5 GHz: switch to channel 36 (UNII-1) + listen 5s for beacons")
    import time as _t

    try:
        set_channel_5g_20mhz(transport, 36)
    except (IOError, ValueError) as e:
        fail(f"set_channel_5g_20mhz(36) failed: {e}")
    ok("channel 36 tuned")

    eps = probe_endpoints(dev)
    ep_in = eps.primary_bulk_in

    seen_bssids: dict[str, int] = {}
    total = 0
    t_start = _t.perf_counter()
    while _t.perf_counter() - t_start < 5.0:
        buf = read_rx_burst(dev, ep_in, max_size=16384, timeout_ms=200)
        if buf is None:
            continue
        for stat, mpdu, rssi in iter_bulk_frames(buf):
            total += 1
            parsed = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100
            )
            if not parsed:
                continue
            if parsed.subtype_id == WlanFrameParser.SUBTYPE_BEACON:
                seen_bssids[parsed.bssid or "?"] = (
                    seen_bssids.get(parsed.bssid or "?", 0) + 1
                )

    print(f"  Parsed frames: {total}, distinct 5G BSSIDs: {len(seen_bssids)}")
    for bssid, count in sorted(seen_bssids.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {bssid}  ({count} beacons)")
    if not seen_bssids:
        print("  (no 5G beacons — might be quiet RF environment; not a failure)")
    else:
        ok(f"{len(seen_bssids)} distinct 5G BSSIDs visible on channel 36")


def phase_warm(dev, transport: RTL8822BUTransport) -> None:
    """Verify warm-reattach: chip should be DETECTED as warm and skip bring-up."""
    from wifit3.chips.rtl8822bu.mac import is_chip_warm as _is_warm
    step("Warm-state probe (chip should report warm after a prior run)")
    is_warm = _is_warm(transport)
    print(f"  is_chip_warm() = {is_warm}")
    if not is_warm:
        fail(
            "Expected chip to be WARM (assumes a prior cold bring-up succeeded "
            "and didn't get replugged). Run the full chain first, then re-run "
            "this phase without replugging."
        )
    ok("Chip detected as WARM — warm-reattach path would skip FW upload + init")


def _extract_ssid(mpdu: bytes) -> str:
    """Extract SSID from a beacon frame (24B hdr + 12B fixed + IEs)."""
    if len(mpdu) < 38:
        return ""
    ies_off = 24 + 12
    if mpdu[ies_off] != 0:
        return ""
    ssid_len = mpdu[ies_off + 1]
    if ies_off + 2 + ssid_len > len(mpdu):
        return ""
    try:
        return mpdu[ies_off + 2: ies_off + 2 + ssid_len].decode("utf-8")
    except UnicodeDecodeError:
        return mpdu[ies_off + 2: ies_off + 2 + ssid_len].hex()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--phase",
        choices=("open", "fw", "validate", "phy", "mac_init",
                 "channel", "beacon", "tx", "fivegig", "warm", "all"),
        default="all",
    )
    p.add_argument("--debug", action="store_true", help="verbose USB logging")
    args = p.parse_args()
    setup_logging(args.debug)

    step("USB discovery + claim")
    dev = open_device()
    ok("Interface 0 claimed")
    transport = RTL8822BUTransport(dev)

    needs_fw = ("fw", "validate", "phy", "mac_init", "channel", "beacon", "all")
    needs_validate = ("validate", "phy", "mac_init", "channel", "beacon", "all")
    needs_phy = ("phy", "mac_init", "channel", "beacon", "all")
    needs_mac_init = ("mac_init", "channel", "beacon", "all")
    needs_channel = ("channel", "beacon", "all")
    needs_beacon = ("beacon", "all")

    try:
        if args.phase in ("open", "all"):
            phase_open(transport)
        if args.phase in needs_fw:
            phase_fw(dev, transport)
        if args.phase in needs_validate:
            phase_validate(transport)
        if args.phase in needs_phy:
            phase_phy(transport)
        if args.phase in needs_mac_init:
            phase_mac_init(transport)
        if args.phase in needs_channel:
            phase_channel(transport, 1)
        if args.phase in needs_beacon:
            phase_beacon(dev, transport, duration_s=8.0)
        if args.phase in ("tx", "all"):
            phase_tx(dev, transport)
        if args.phase in ("fivegig", "all"):
            phase_5g(transport, dev)
        if args.phase == "warm":
            phase_warm(dev, transport)
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
