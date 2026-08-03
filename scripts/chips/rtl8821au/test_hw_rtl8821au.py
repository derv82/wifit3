"""Hardware test for RTL8821AU bring-up.

  --phase open     : USB enumeration + control transfers (chip-ID read).
  --phase fw       : power-on + FW upload + poll for BIT_FWDL_CHK_RPT ACK.
  --phase validate : fw, then reset CPU and poll FW_READY_LEGACY (proves
                     the wlan CPU is *running* the firmware, not just
                     accepting bytes).
  --phase mac_init : validate + LLT init + the MAC-only chunk of
                     rtw88xxa_power_on (lines 1083..1175). Verifies REG_CR
                     ends with BIT_MACTXEN|BIT_MACRXEN set. (M4b)
  --phase phy      : mac_init + mac_tbl load + BB/RF table loads +
                     switch_band(2G, 20MHz) + post-PHY inline pokes. (M4c)
  --phase channel  : phy + set_channel_2g_20mhz(1). (M6)
  --phase beacon   : channel + a few seconds of RX polling, decoding any
                     frames that show up. (M5)
  --phase all      : open through beacon (default).

Usage:
    uv run python scripts/chips/rtl8821au/test_hw_rtl8821au.py                 # all
    uv run python scripts/chips/rtl8821au/test_hw_rtl8821au.py --phase open
    uv run python scripts/chips/rtl8821au/test_hw_rtl8821au.py --phase fw --debug
    uv run python scripts/chips/rtl8821au/test_hw_rtl8821au.py --phase mac_init --debug
    uv run python scripts/chips/rtl8821au/test_hw_rtl8821au.py --phase phy --debug
    uv run python scripts/chips/rtl8821au/test_hw_rtl8821au.py --phase channel --debug
    uv run python scripts/chips/rtl8821au/test_hw_rtl8821au.py --phase beacon --debug
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821au.constants import (
    BIT_FWDL_CHK_RPT,
    BIT_MACRXEN,
    BIT_MACTXEN,
    BIT_MCUFWDL_EN,
    REG_CR,
    REG_MCUFW_CTRL,
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    USB_PID_AWUS036ACS,
    USB_VID_REALTEK,
)
from wifit3.chips.rtl8821au.firmware import (
    download_firmware_legacy,
    download_firmware_validate_legacy,
    en_download_firmware_legacy,
    load_firmware_blob,
)
from wifit3.chips.rtl8821au.chan import set_channel_2g_20mhz
from wifit3.chips.rtl8821au.mac import (
    mac_power_on,
    post_fw_mac_init,
    pre_fw_init,
)
from wifit3.chips.rtl8821au.phy import EfuseDefaults, post_mac_init_phy
from wifit3.chips.rtl8821au.rx import (
    iter_bulk_frames,
    probe_endpoints,
    read_rx_burst,
)
from wifit3.chips.rtl8821au.transport import RTL8821AUTransport
from wifit3.dot11.parser import WlanFrameParser


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
    dev = usb.core.find(
        idVendor=USB_VID_REALTEK, idProduct=USB_PID_AWUS036ACS, backend=backend
    )
    if dev is None:
        fail(
            "AWUS036ACS not found (VID=0x0bda PID=0x0811). "
            "Plug it in, confirm Zadig bound it to WinUSB, and retry."
        )
    print(f"  Found AWUS036ACS at bus {dev.bus}, address {dev.address}")

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


def phase_open(transport: RTL8821AUTransport) -> None:
    step("Read REG_SYS_CFG1 (0x00F0)")
    val = transport.read32(REG_SYS_CFG1)
    print(f"  REG_SYS_CFG1 = 0x{val:08x}")
    if val == 0 or val == 0xFFFFFFFF:
        fail(
            f"Implausible value 0x{val:08x} — device may be in a bad state. "
            "Unplug, wait 5s, replug, and rerun."
        )
    ok("control-transfer plumbing works (capture-1 ground truth was 0x04412135)")

    val2 = transport.read32(REG_SYS_CFG2)
    print(f"  REG_SYS_CFG2 = 0x{val2:08x}  (capture-1: 0x00000005)")


def phase_fw(transport: RTL8821AUTransport, debug: bool):
    """Returns the FifoConf created during pre_fw_init, for downstream use."""
    step("MAC power-on (pre-cfg + pwr_seq + init-sys-cfg)")
    try:
        mac_power_on(transport)
    except (IOError, NotImplementedError) as e:
        fail(f"mac_power_on failed: {e}")
    ok("mac_power_on completed (device ready for FW upload)")
    print(f"  REG_MCUFW_CTRL = 0x{transport.read32(REG_MCUFW_CTRL):08x} (pre-FW)")

    step("Pre-FW init (set_trx_fifo_info + llt_init + DROP_DATA_EN)")
    import time as _t
    t0 = _t.perf_counter()
    try:
        fifo = pre_fw_init(transport)
    except IOError as e:
        fail(f"pre_fw_init failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"LLT init done in {dt_ms:.1f} ms (rsvd_boundary={fifo.rsvd_boundary})")

    step("Enable FW download mode")
    try:
        en_download_firmware_legacy(transport, True)
    except IOError as e:
        fail(f"en_download_firmware_legacy(True) failed: {e}")
    ok("BIT_MCUFWDL_EN latched, BIT_ROM_DLEN cleared")
    print(f"  REG_MCUFW_CTRL = 0x{transport.read32(REG_MCUFW_CTRL):08x} (FW-dl enabled)")

    step("Upload firmware blob")
    fw = load_firmware_blob()
    body = len(fw) - 32
    print(f"  Loaded {len(fw)} bytes ({body} body + 32 header)")

    last_pct = -1

    def progress(page: int, total: int) -> None:
        nonlocal last_pct
        pct = int(page * 100 / total)
        if pct != last_pct:
            last_pct = pct
            print(f"  [{pct:3d}%] page {page}/{total}")

    try:
        ack = download_firmware_legacy(transport, fw, progress_cb=progress, debug_log=debug)
    except Exception as e:
        fail(f"download_firmware_legacy raised: {type(e).__name__}: {e}")

    mcufw = transport.read32(REG_MCUFW_CTRL)
    print(f"  REG_MCUFW_CTRL = 0x{mcufw:08x} (post-upload)")
    print(f"    BIT_FWDL_CHK_RPT = {bool(mcufw & BIT_FWDL_CHK_RPT)}")
    print(f"    BIT_MCUFWDL_EN   = {bool(mcufw & BIT_MCUFWDL_EN)}")

    if not ack:
        fail("FW upload finished but BIT_FWDL_CHK_RPT never came back set.")
    ok("BIT_FWDL_CHK_RPT set — device ACKed the firmware upload.")

    step("Disable FW download mode")
    en_download_firmware_legacy(transport, False)
    ok("BIT_MCUFWDL_EN cleared")
    return fifo


def phase_validate(transport: RTL8821AUTransport) -> None:
    step("Validate FW is running (CPU reset + FW_READY_LEGACY poll)")
    ok_run, last = download_firmware_validate_legacy(transport)
    print(f"  REG_MCUFW_CTRL = 0x{last:08x}")
    bits = []
    if last & 0x02:  bits.append("MCUFWDL_RDY")
    if last & 0x04:  bits.append("FWDL_CHK_RPT")
    if last & 0x40:  bits.append("WINTINI_RDY")
    if last & 0x80:  bits.append("RAM_DL_SEL")
    print(f"  Set bits in FW_READY_LEGACY mask: {bits or '(none)'}")
    if not ok_run:
        fail("FW_READY_LEGACY never satisfied — firmware not running.")
    ok("FW_READY_LEGACY satisfied — wlan CPU is running the firmware.")


def phase_mac_init(transport: RTL8821AUTransport, fifo) -> None:
    step("Post-FW MAC init (rtw88xxa_power_on lines 1083..1175)")
    import time as _t
    t0 = _t.perf_counter()
    try:
        post_fw_mac_init(transport, fifo)
    except IOError as e:
        fail(f"post_fw_mac_init failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"MAC init completed in {dt_ms:.1f} ms")

    cr = transport.read32(REG_CR)
    print(f"  REG_CR = 0x{cr:08x}")
    print(f"    BIT_MACTXEN (bit 6) = {bool(cr & BIT_MACTXEN)}")
    print(f"    BIT_MACRXEN (bit 7) = {bool(cr & BIT_MACRXEN)}")
    if not (cr & BIT_MACTXEN):
        fail("BIT_MACTXEN not set after init — MAC TX not enabled.")
    if not (cr & BIT_MACRXEN):
        fail("BIT_MACRXEN not set after init — MAC RX not enabled.")
    ok("REG_CR has MACTXEN | MACRXEN set — MAC is enabled.")


def phase_phy(transport: RTL8821AUTransport) -> EfuseDefaults:
    step("Post-MAC PHY init (mac_tbl + BB/RF tables + switch_band 2G/20MHz)")
    efuse = EfuseDefaults()
    import time as _t
    t0 = _t.perf_counter()
    try:
        post_mac_init_phy(transport, efuse)
    except IOError as e:
        fail(f"post_mac_init_phy failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"PHY init completed in {dt_ms:.1f} ms")
    return efuse


def phase_channel(transport: RTL8821AUTransport, channel: int = 1) -> None:
    step(f"Set channel {channel} @ 20 MHz")
    import time as _t
    t0 = _t.perf_counter()
    try:
        set_channel_2g_20mhz(transport, channel)
    except IOError as e:
        fail(f"set_channel failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"Channel set in {dt_ms:.1f} ms")


def phase_beacon(dev, transport: RTL8821AUTransport, duration_s: float = 5.0) -> None:
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
                # Try to extract SSID from beacon body (mpdu[24:] is the
                # 802.11 hdr+ts+intv+caps then SSID IE)
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


def _extract_ssid(mpdu: bytes) -> str:
    """Extract SSID from a beacon frame.

    Frame layout: 24B 802.11 MAC hdr + 8B timestamp + 2B beacon interval +
    2B capabilities = 36B fixed, then tagged IEs starting with SSID (id=0).
    """
    if len(mpdu) < 38:
        return ""
    ies_off = 24 + 12   # MAC hdr + fixed beacon body
    if mpdu[ies_off] != 0:   # SSID IE id is 0
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
        choices=("open", "fw", "validate", "mac_init", "phy", "channel", "beacon", "all"),
        default="all",
    )
    p.add_argument("--debug", action="store_true", help="verbose USB + FW logging")
    p.add_argument("--channel", type=int, default=1, help="channel to tune (1..13)")
    p.add_argument("--beacon-secs", type=float, default=5.0,
                   help="how long to listen for beacons (phase=beacon)")
    args = p.parse_args()
    setup_logging(args.debug)

    step("USB discovery + claim")
    dev = open_device()
    ok("Interface 0 claimed")
    transport = RTL8821AUTransport(dev)

    fifo = None
    needs_fw = ("fw", "validate", "mac_init", "phy", "channel", "beacon", "all")
    needs_validate = ("validate", "mac_init", "phy", "channel", "beacon", "all")
    needs_mac_init = ("mac_init", "phy", "channel", "beacon", "all")
    needs_phy = ("phy", "channel", "beacon", "all")
    needs_channel = ("channel", "beacon", "all")
    needs_beacon = ("beacon", "all")

    try:
        if args.phase in ("open", "all"):
            phase_open(transport)
        if args.phase in needs_fw:
            fifo = phase_fw(transport, args.debug)
        if args.phase in needs_validate:
            phase_validate(transport)
        if args.phase in needs_mac_init:
            phase_mac_init(transport, fifo)
        if args.phase in needs_phy:
            phase_phy(transport)
        if args.phase in needs_channel:
            phase_channel(transport, args.channel)
        if args.phase in needs_beacon:
            phase_beacon(dev, transport, args.beacon_secs)
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
