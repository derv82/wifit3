"""Hardware test for RTL8188EUS M1-M5.

Phases:
  --phase open    : USB enumeration + claim + read REG_SYS_CFG (chip ID).
  --phase power   : open + rtl8188eu_power_on (disabled->emu->active + REG_CR).
  --phase fw      : power + download_firmware + start_firmware (poll
                    MCU_WINT_INIT_READY). M1 success bar.
  --phase mac     : fw + post_fw_mac_init (mactable + LLT + flip
                    CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE). M2 success bar.
  --phase phy     : mac + post_mac_init_phy (BB + AGC + RF path A tables
                    = 192 + 130 + 95 = 417 register writes). M3 success bar.
  --phase channel : phy + set_channel_2g_20mhz(--channel). Round-trip
                    SIPI read on RF MODE_AG proves channel + BW bits.
                    M4 success bar.
  --phase beacon  : channel + enable_rx_data_path + listen for
                    --beacon-secs on the channel; report distinct BSSIDs
                    with SSIDs + RSSIs + beacon counts. M5 success bar.
  --phase tx      : channel + enable_rf + send --count deauths to
                    --bssid (--client default broadcast). M6 success bar:
                    all N sends return without USBError.
  --phase warm    : verify is_chip_warm() is True after a previous run
                    has left the chip alive. M7 success bar (run TWICE:
                    first cold, then warm with --phase warm).
  --phase efuse   : (post-fw) dump the raw EFUSE map + parsed defaults.
                    Run AFTER --phase fw — EFUSE access needs FW running.
                    M8 diagnostic — confirms per-channel TX power values
                    are non-default before chasing TX issues.
  --phase all     : open -> ... -> beacon (default; does NOT include tx/warm).

Usage:
    uv run python scripts/rtl8188eus/test_hw_rtl8188eus.py                # all
    uv run python scripts/rtl8188eus/test_hw_rtl8188eus.py --phase beacon --debug
    uv run python scripts/rtl8188eus/test_hw_rtl8188eus.py --phase beacon --channel 6 --beacon-secs 10
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

from wifit3.chips.rtl8188eus.constants import (
    CR_INIT_POWER_ON,
    CR_MAC_RX_ENABLE,
    CR_MAC_TX_ENABLE,
    MCU_WINT_INIT_READY,
    REG_CR,
    REG_MCU_FW_DL,
    REG_RQPN,
    REG_SYS_CFG,
    REG_TRXFF_BNDY,
    TOTAL_PAGE_NUM_8188E,
    TRXFF_BOUNDARY_8188E,
)
from wifit3.chips.rtl8188eus.firmware import (
    download_firmware,
    load_firmware_blob,
    start_firmware,
)
from wifit3.chips.rtl8188eus.chan import read_rfreg, set_channel_2g_20mhz
from wifit3.chips.rtl8188eus.efuse import EfuseDefaults, read_and_parse, read_efuse_map
from wifit3.chips.rtl8188eus.constants import (
    MODE_AG_BW_20MHZ_8723B,
    MODE_AG_BW_MASK,
    MODE_AG_CHANNEL_MASK,
    RF6052_REG_MODE_AG,
)
from wifit3.chips.rtl8188eus.mac import enable_rx_data_path, is_chip_warm, post_fw_mac_init
from wifit3.chips.rtl8188eus.phy import RF_A, enable_cck_ofdm_block, enable_rf, post_mac_init_phy
from wifit3.chips.rtl8188eus.rx import (
    iter_bulk_frames,
    probe_endpoints,
    read_rx_burst,
)
from wifit3.chips.rtl8188eus.tx import (
    build_deauth,
    pick_bulk_out_mgmt,
    send_mgmt_frame,
)
from wifit3.dot11.parser import WlanFrameParser
from wifit3.chips.rtl8188eus.phy_tables import (
    AGC_TABLE_8188E,
    PHY_INIT_TABLE_8188E,
    RADIO_A_INIT_TABLE_8188E,
)
from wifit3.chips.rtl8188eus.transport import RTL8188EUSTransport

USB_VID_TPLINK = 0x2357
USB_PID_TL_WN722N_V2 = 0x010C


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
        idVendor=USB_VID_TPLINK, idProduct=USB_PID_TL_WN722N_V2, backend=backend
    )
    if dev is None:
        fail(
            "TL-WN722N v2/v3 not found (VID=0x2357 PID=0x010C). "
            "Plug it in, confirm Zadig bound it to WinUSB, and retry."
        )
    print(f"  Found TL-WN722N v2/v3 at bus {dev.bus}, address {dev.address}")
    print(f"  bcdUSB = 0x{dev.bcdUSB:04x}")

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


def phase_open(transport: RTL8188EUSTransport) -> None:
    step("Read REG_SYS_CFG (0x00F0)")
    val = transport.read32(REG_SYS_CFG)
    print(f"  REG_SYS_CFG = 0x{val:08x}")
    if val == 0 or val == 0xFFFFFFFF:
        fail(
            f"Implausible value 0x{val:08x} — device may be in a bad state. "
            "Unplug, wait 5s, replug, and rerun."
        )
    ok("vendor-control reads work (chip is responsive)")


def phase_power(transport: RTL8188EUSTransport) -> None:
    from wifit3.chips.rtl8188eus.driver import RTL8188EUSDriver
    step("rtl8188eu_power_on (disabled->emu->active + REG_CR init)")
    # We don't have a full driver instance here, so call the helper bits
    # by hand. driver._power_on is a private method but the kernel path
    # is short enough to drive directly.
    drv = RTL8188EUSDriver.__new__(RTL8188EUSDriver)
    drv.transport = transport
    t0 = time.perf_counter()
    try:
        drv._power_on()  # type: ignore[attr-defined]
    except IOError as e:
        fail(f"power_on failed: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    ok(f"power_on completed in {dt_ms:.1f} ms")

    cr = transport.read16(REG_CR)
    print(f"  REG_CR = 0x{cr:04x}")
    if cr != CR_INIT_POWER_ON:
        # Some bits may already be set; just sanity-check the ones we set.
        if (cr & CR_INIT_POWER_ON) != CR_INIT_POWER_ON:
            fail(f"REG_CR=0x{cr:04x} missing some CR_INIT_POWER_ON bits (expected 0x{CR_INIT_POWER_ON:04x})")
    ok("REG_CR has CR_INIT_POWER_ON bits set (DMA + protocol + sched + sec + caltimer)")


def phase_fw(transport: RTL8188EUSTransport, debug: bool) -> None:
    step("Load firmware blob")
    fw = load_firmware_blob()
    print(f"  Loaded {len(fw)} bytes ({len(fw)-32} body + 32 header)")
    ok("firmware blob loaded (sha256-verified against linux-firmware)")

    step("download_firmware (4 pages, 196-B chunks)")
    t0 = time.perf_counter()
    try:
        download_firmware(transport, fw)
    except Exception as e:
        fail(f"download_firmware raised: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    ok(f"FW upload completed in {dt_ms:.1f} ms (capture-1 ground truth: ~1.0 ms)")

    step("start_firmware (poll MCU_FW_DL_CSUM_REPORT + MCU_WINT_INIT_READY)")
    t0 = time.perf_counter()
    try:
        start_firmware(transport)
    except TimeoutError as e:
        # Show the last value so we can debug which poll failed.
        last = transport.read32(REG_MCU_FW_DL)
        print(f"  REG_MCU_FW_DL (last) = 0x{last:08x}")
        bits = []
        if last & 0x01:
            bits.append("FW_DL_ENABLE")
        if last & 0x02:
            bits.append("FW_DL_READY")
        if last & 0x04:
            bits.append("FW_DL_CSUM_REPORT")
        if last & 0x40:
            bits.append("WINT_INIT_READY")
        if last & 0x80:
            bits.append("FW_RAM_SEL")
        print(f"  Set bits: {bits or '(none)'}")
        fail(f"start_firmware timed out: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000

    final = transport.read32(REG_MCU_FW_DL)
    print(f"  REG_MCU_FW_DL (final) = 0x{final:08x}")
    print(f"    MCU_WINT_INIT_READY = {bool(final & MCU_WINT_INIT_READY)}")
    if not (final & MCU_WINT_INIT_READY):
        fail("MCU_WINT_INIT_READY not set after start_firmware returned")
    ok(f"MCU_WINT_INIT_READY set within {dt_ms:.1f} ms — 8051 is running the firmware.")


def phase_mac(transport: RTL8188EUSTransport) -> None:
    step("post_fw_mac_init (mactable + queue_rsvd + TRXFF_BNDY + LLT + MAC_TX/RX flip)")
    t0 = time.perf_counter()
    try:
        post_fw_mac_init(transport)
    except (IOError, OSError) as e:
        cr = transport.read16(REG_CR)
        rqpn = transport.read32(REG_RQPN)
        trxff = transport.read32(REG_TRXFF_BNDY)
        print(f"  REG_CR        = 0x{cr:04x}")
        print(f"  REG_RQPN      = 0x{rqpn:08x}")
        print(f"  REG_TRXFF_BNDY= 0x{trxff:08x}")
        fail(f"post_fw_mac_init raised: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    ok(f"post_fw_mac_init completed in {dt_ms:.1f} ms")

    cr = transport.read16(REG_CR)
    trxff = transport.read32(REG_TRXFF_BNDY)
    rqpn = transport.read32(REG_RQPN)
    print(f"  REG_CR         = 0x{cr:04x}")
    print(f"    CR_MAC_TX_ENABLE = {bool(cr & CR_MAC_TX_ENABLE)}")
    print(f"    CR_MAC_RX_ENABLE = {bool(cr & CR_MAC_RX_ENABLE)}")
    print(f"  REG_TRXFF_BNDY = 0x{trxff:08x}  (low byte = 0x{(TOTAL_PAGE_NUM_8188E + 1) & 0xFF:02x}, +2 = 0x{TRXFF_BOUNDARY_8188E:04x})")
    print(f"  REG_RQPN       = 0x{rqpn:08x}  (RQPN_LOAD + queue page assignments)")
    if not (cr & CR_MAC_TX_ENABLE):
        fail("CR_MAC_TX_ENABLE not set after post_fw_mac_init")
    if not (cr & CR_MAC_RX_ENABLE):
        fail("CR_MAC_RX_ENABLE not set after post_fw_mac_init")
    ok("REG_CR has MAC_TX_ENABLE | MAC_RX_ENABLE — MAC is enabled.")


def phase_phy(transport: RTL8188EUSTransport) -> None:
    step("post_mac_init_phy (BB %d + AGC %d + RF-A %d = %d register writes)" % (
        len(PHY_INIT_TABLE_8188E),
        len(AGC_TABLE_8188E),
        len(RADIO_A_INIT_TABLE_8188E),
        len(PHY_INIT_TABLE_8188E) + len(AGC_TABLE_8188E) + len(RADIO_A_INIT_TABLE_8188E),
    ))
    cr_before = transport.read16(REG_CR)
    t0 = time.perf_counter()
    try:
        post_mac_init_phy(transport, EfuseDefaults())
    except (IOError, OSError) as e:
        cr = transport.read16(REG_CR)
        print(f"  REG_CR = 0x{cr:04x}  (was 0x{cr_before:04x} before PHY init)")
        fail(f"post_mac_init_phy raised: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    ok(f"PHY init completed in {dt_ms:.1f} ms")

    # Verify the chip is still responsive after ~400 register writes.
    cr_after = transport.read16(REG_CR)
    print(f"  REG_CR (pre-PHY)  = 0x{cr_before:04x}")
    print(f"  REG_CR (post-PHY) = 0x{cr_after:04x}")
    if cr_after == 0xFFFF or cr_after == 0x0000:
        fail(f"REG_CR readback is bogus (0x{cr_after:04x}) — chip likely wedged")
    if cr_after != cr_before:
        # Not a hard failure — some PHY writes may toggle CR bits — but worth noting.
        print(f"  NOTE: REG_CR changed during PHY init: 0x{cr_before:04x} -> 0x{cr_after:04x}")
    ok("REG_CR still readable post-PHY-init — chip is responsive.")


def phase_channel(transport: RTL8188EUSTransport, channel: int) -> None:
    step(f"set_channel_2g_20mhz({channel})")
    t0 = time.perf_counter()
    try:
        set_channel_2g_20mhz(transport, channel)
    except (ValueError, IOError, OSError) as e:
        fail(f"set_channel_2g_20mhz raised: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    ok(f"channel set completed in {dt_ms:.1f} ms")

    # Round-trip readback: re-read RF MODE_AG and verify channel + BW bits.
    val = read_rfreg(transport, RF_A, RF6052_REG_MODE_AG)
    ch_field = val & MODE_AG_CHANNEL_MASK
    bw_field = val & MODE_AG_BW_MASK
    print(f"  RF MODE_AG (0x18) = 0x{val:05x}")
    print(f"    channel bits[9:0]   = {ch_field}  (expected {channel})")
    print(f"    BW bits[11:10]      = 0x{bw_field:x}  (expected 0x{MODE_AG_BW_20MHZ_8723B:x})")
    if ch_field != channel:
        fail(
            f"RF MODE_AG channel field is {ch_field}, expected {channel}. "
            "Either the SIPI write didn't land, or the readback path is wrong."
        )
    if bw_field != MODE_AG_BW_20MHZ_8723B:
        fail(
            f"RF MODE_AG BW field is 0x{bw_field:x}, expected 0x{MODE_AG_BW_20MHZ_8723B:x}."
        )
    ok(
        f"round-trip SIPI read+write confirmed — RF tuned to channel {channel} @ 20 MHz."
    )


def _extract_ssid(mpdu: bytes) -> str:
    """Decode the SSID IE (tag 0) from a beacon body.

    Frame layout: 24B 802.11 MAC hdr + 8B timestamp + 2B beacon interval +
    2B capabilities = 36B fixed, then tagged IEs.
    """
    if len(mpdu) < 38:
        return ""
    ies_off = 24 + 12
    if mpdu[ies_off] != 0:
        return ""
    ssid_len = mpdu[ies_off + 1]
    if ies_off + 2 + ssid_len > len(mpdu):
        return ""
    try:
        return mpdu[ies_off + 2 : ies_off + 2 + ssid_len].decode("utf-8")
    except UnicodeDecodeError:
        return mpdu[ies_off + 2 : ies_off + 2 + ssid_len].hex()


def phase_beacon(
    dev,
    transport: RTL8188EUSTransport,
    duration_s: float,
    channel: int,
) -> None:
    step("enable_rx_data_path (REG_RCR + DRVINFO_SZ + interrupts + GPIO_MUXCFG)")
    t0 = time.perf_counter()
    try:
        enable_rx_data_path(transport)
    except (IOError, OSError) as e:
        fail(f"enable_rx_data_path raised: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    ok(f"RX path enabled in {dt_ms:.1f} ms")

    step("enable_cck_ofdm_block (REG_FPGA0_RF_MODE |= CCK | OFDM)")
    try:
        enable_cck_ofdm_block(transport)
    except (IOError, OSError) as e:
        fail(f"enable_cck_ofdm_block raised: {type(e).__name__}: {e}")
    ok("CCK + OFDM baseband blocks enabled")

    step("enable_rf (OFDM TRX path A + REG_RF_CTRL + REG_TXPAUSE=0)")
    t0 = time.perf_counter()
    try:
        enable_rf(transport)
    except (IOError, OSError) as e:
        fail(f"enable_rf raised: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    ok(f"RF armed in {dt_ms:.1f} ms")

    step("Probe USB endpoints")
    eps = probe_endpoints(dev)
    if not eps.bulk_in:
        fail("no bulk-IN endpoint found on the device descriptor")
    ep_in = eps.primary_bulk_in
    print(f"  using bulk-IN endpoint 0x{ep_in:02x}")

    step(f"Listen on channel {channel} for {duration_s:.0f} s")
    seen_bssids: dict[str, int] = {}
    seen_ssids: dict[str, str] = {}
    seen_rssis: dict[str, int] = {}
    total_frames = 0
    parse_failures = 0
    subtype_hist: dict[tuple[int, int], int] = {}  # (type_id, subtype_id) -> count
    first_frame_dumped = False
    bursts = 0
    bytes_received = 0
    t_start = time.perf_counter()

    while time.perf_counter() - t_start < duration_s:
        buf = read_rx_burst(dev, ep_in, max_size=16384, timeout_ms=200)
        if buf is None:
            continue
        bursts += 1
        bytes_received += len(buf)

        # Dump the very first URB raw, once, so we can sanity-check rxdesc16 layout.
        if not first_frame_dumped and bursts == 1:
            print(f"  -- first URB: {len(buf)} bytes --")
            print(f"     first 64 B hex: {buf[:64].hex()}")
            first_frame_dumped = True

        for desc, mpdu, rssi in iter_bulk_frames(buf):
            total_frames += 1

            # Dump the first frame across all bursts so we can see what
            # iter_bulk_frames is yielding.
            if total_frames == 1:
                print(
                    f"  -- first yielded frame: pkt_len={desc.pkt_len} "
                    f"drv_info_sz={desc.drv_info_sz_bytes} shift={desc.shift} "
                    f"phy_stats={desc.phy_stats_present} rpt_sel={desc.rpt_sel} "
                    f"rxmcs={desc.rxmcs} rssi={rssi}"
                )
                print(f"     mpdu[:24] hex: {mpdu[:24].hex()}")
                print(f"     fc0=0x{mpdu[0]:02x} fc1=0x{mpdu[1]:02x}  "
                      f"(proto_ver={mpdu[0] & 0x03}, type={(mpdu[0] & 0x0C) >> 2}, "
                      f"subtype={(mpdu[0] & 0xF0) >> 4})")

            # Always tally the (type, subtype) decoded directly from FC byte,
            # even if WlanFrameParser later rejects (so we see what we're
            # actually receiving).
            if len(mpdu) >= 2:
                raw_type = (mpdu[0] & 0x0C) >> 2
                raw_subtype = (mpdu[0] & 0xF0) >> 4
                subtype_hist[(raw_type, raw_subtype)] = (
                    subtype_hist.get((raw_type, raw_subtype), 0) + 1
                )

            parsed = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100
            )
            if not parsed:
                parse_failures += 1
                continue
            if parsed.subtype_id == WlanFrameParser.SUBTYPE_BEACON:
                bssid = parsed.bssid or "?"
                seen_bssids[bssid] = seen_bssids.get(bssid, 0) + 1
                if bssid not in seen_ssids:
                    seen_ssids[bssid] = _extract_ssid(mpdu)
                if rssi is not None:
                    # Track best (highest) RSSI seen for this BSSID.
                    if bssid not in seen_rssis or rssi > seen_rssis[bssid]:
                        seen_rssis[bssid] = rssi

    print(f"  Bursts received: {bursts}, total bytes: {bytes_received}")
    print(f"  Parsed frames:   {total_frames}  (parse failures: {parse_failures})")
    print("  Raw FC type/subtype histogram:")
    for (tp, sub), count in sorted(subtype_hist.items(), key=lambda kv: -kv[1])[:10]:
        type_name = {0: "MGMT", 1: "CTRL", 2: "DATA", 3: "EXT"}.get(tp, f"?{tp}")
        print(f"     type={tp}({type_name})  subtype={sub:#x}  count={count}")
    print(f"  Distinct beacon BSSIDs: {len(seen_bssids)}")
    for bssid, count in sorted(seen_bssids.items(), key=lambda kv: -kv[1])[:25]:
        ssid = seen_ssids.get(bssid, "")
        rssi = seen_rssis.get(bssid)
        rssi_str = f"{rssi:+4d} dBm" if rssi is not None else "  --  "
        print(f"    {bssid}  {rssi_str}  ({count:3d} beacons)  ssid={ssid!r}")

    if len(seen_bssids) == 0:
        fail(
            "no beacons captured — RX path is not delivering 802.11 frames. "
            "Check: REG_RCR write, bulk-IN endpoint correctness, channel set."
        )
    ok(f"{len(seen_bssids)} distinct BSSIDs visible on channel {channel}")


def _parse_mac(s: str) -> bytes:
    """Parse 'aa:bb:cc:dd:ee:ff' (or no separators) into a 6-byte MAC."""
    h = s.replace(":", "").replace("-", "").replace(" ", "")
    if len(h) != 12:
        raise argparse.ArgumentTypeError(f"MAC must be 12 hex chars, got {len(h)}: {s!r}")
    return bytes.fromhex(h)


def phase_tx(
    dev,
    transport: RTL8188EUSTransport,
    bssid: bytes,
    client: bytes,
    count: int,
    channel: int,
) -> None:
    step("enable_rx_data_path + enable_rf (need RF on to TX)")
    try:
        enable_rx_data_path(transport)
        enable_cck_ofdm_block(transport)
        enable_rf(transport)
    except (IOError, OSError) as e:
        fail(f"RX/RF enable raised: {type(e).__name__}: {e}")
    ok("RF armed")

    step("Probe USB endpoints + pick MGMT bulk-OUT")
    eps = probe_endpoints(dev)
    if not eps.bulk_out:
        fail("no bulk-OUT endpoint found")
    ep_out = pick_bulk_out_mgmt(eps.bulk_out)
    print(f"  MGMT bulk-OUT endpoint: 0x{ep_out:02x}  (bulk_out={[f'0x{e:02x}' for e in eps.bulk_out]})")

    step(f"Build deauth: BSSID={bssid.hex(':')} -> client={client.hex(':')}, reason=0x07")
    frame = build_deauth(bssid, client)
    print(f"  frame ({len(frame)} bytes): {frame.hex()}")

    is_bcast = (client[0] & 0x01) != 0

    step(f"Send {count} deauth frames via EP 0x{ep_out:02x}")
    t0 = time.perf_counter()
    sent_ok = 0
    sent_fail = 0
    for i in range(count):
        try:
            send_mgmt_frame(dev, ep_out, frame, is_broadcast=is_bcast)
            sent_ok += 1
        except Exception as e:
            sent_fail += 1
            print(f"  send {i+1}/{count} failed: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    rate = sent_ok / (dt_ms / 1000) if dt_ms > 0 else 0
    print(f"  Result: {sent_ok}/{count} sent ok, {sent_fail} failed in {dt_ms:.1f} ms ({rate:.0f} fps)")
    if sent_fail:
        fail(f"{sent_fail} of {count} sends failed — TX path NOT working cleanly")
    ok(f"All {count} deauths accepted by the chip (no USBError, no pipe stall).")


def phase_efuse(transport: RTL8188EUSTransport) -> None:
    step("Read full EFUSE map (512 B)")
    t0 = time.perf_counter()
    raw = read_efuse_map(transport)
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"  EFUSE read completed in {dt_ms:.0f} ms ({len(raw)} bytes)")
    # Dump first 256 bytes hex (where all the useful fields live).
    print("  EFUSE map [0x00..0xFF]:")
    for off in range(0, 256, 16):
        chunk = raw[off:off + 16]
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"    {off:04x}  {chunk.hex(' ')}  |{ascii_part}|")

    step("Parse 8188eu EFUSE fields")
    parsed = read_and_parse(transport)
    print(f"  MAC address          : {parsed.mac_address.hex(':') if parsed.mac_address else '(unprogrammed/0xFF)'}")
    print(f"  CCK TX power (per group)    : {[f'0x{x:02x}' for x in parsed.cck_tx_power_index_A]}")
    print(f"  HT40-1s TX power (per group): {[f'0x{x:02x}' for x in parsed.ht40_1s_tx_power_index_A]}")
    print(f"  OFDM diff (path A): {parsed.ofdm_tx_power_diff_a:+d}")
    print(f"  HT20 diff (path A): {parsed.ht20_tx_power_diff_a:+d}")
    print(f"  HT40 diff (path A): {parsed.ht40_tx_power_diff_a:+d}")
    if all(x == 0x22 for x in parsed.cck_tx_power_index_A):
        print("  NOTE: all power indices are fallback default (0x22) — "
              "EFUSE is unprogrammed or all-0xFF.")
    ok("EFUSE parse complete.")


def phase_warm(transport: RTL8188EUSTransport) -> None:
    step("is_chip_warm() probe")
    warm = is_chip_warm(transport)
    mcu_fw = transport.read32(REG_MCU_FW_DL)
    cr = transport.read16(REG_CR)
    print(f"  REG_MCU_FW_DL = 0x{mcu_fw:08x}  (MCU_WINT_INIT_READY bit 6 = {bool(mcu_fw & (1 << 6))})")
    print(f"  REG_CR        = 0x{cr:04x}      (MAC_TX_ENABLE bit 6 = {bool(cr & CR_MAC_TX_ENABLE)}, MAC_RX_ENABLE bit 7 = {bool(cr & CR_MAC_RX_ENABLE)})")
    print(f"  is_chip_warm() = {warm}")
    if warm:
        ok("Chip is WARM — a previous wifit3 session left FW running + MAC enabled.")
        print("  → driver.connect() will skip FW upload + MAC/PHY init + channel set + enable_rf.")
    else:
        ok("Chip is COLD — no prior wifit3 state detected. Full bring-up needed.")
        print("  → Run --phase all first, then re-run --phase warm to verify warm path.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--phase",
        choices=("open", "power", "fw", "mac", "phy", "channel", "beacon", "tx", "warm", "efuse", "all"),
        default="all",
    )
    p.add_argument("--debug", action="store_true", help="verbose USB + FW logging")
    p.add_argument("--channel", type=int, default=1, help="channel to tune (1..13)")
    p.add_argument(
        "--beacon-secs",
        type=float,
        default=5.0,
        help="how long to listen for beacons (phase=beacon)",
    )
    # TX phase args
    p.add_argument("--bssid", type=_parse_mac,
                   default=b"\xff\xff\xff\xff\xff\xff",
                   help="target BSSID for phase=tx (default broadcast)")
    p.add_argument("--client", type=_parse_mac,
                   default=b"\xff\xff\xff\xff\xff\xff",
                   help="target client MAC for phase=tx (default broadcast)")
    p.add_argument("--count", type=int, default=20,
                   help="deauth frame count for phase=tx (default 20)")
    args = p.parse_args()
    setup_logging(args.debug)

    step("USB discovery + claim")
    dev = open_device()
    ok("Interface 0 claimed")
    transport = RTL8188EUSTransport(dev)

    needs_power = ("power", "fw", "mac", "phy", "channel", "beacon", "tx", "all")
    needs_fw = ("fw", "mac", "phy", "channel", "beacon", "tx", "all")
    needs_mac = ("mac", "phy", "channel", "beacon", "tx", "all")
    needs_phy = ("phy", "channel", "beacon", "tx", "all")
    needs_channel = ("channel", "beacon", "tx", "all")
    needs_beacon = ("beacon", "all")
    needs_tx = ("tx",)

    try:
        if args.phase in ("open", "all"):
            phase_open(transport)
        if args.phase in needs_power:
            phase_power(transport)
        if args.phase in needs_fw:
            phase_fw(transport, args.debug)
        if args.phase in needs_mac:
            phase_mac(transport)
        if args.phase in needs_phy:
            phase_phy(transport)
        if args.phase in needs_channel:
            phase_channel(transport, args.channel)
        if args.phase in needs_beacon:
            phase_beacon(dev, transport, args.beacon_secs, args.channel)
        if args.phase in needs_tx:
            phase_tx(dev, transport, args.bssid, args.client, args.count, args.channel)
        if args.phase == "warm":
            phase_warm(transport)
        if args.phase == "efuse":
            # EFUSE needs FW running — run the FW-upload chain first if cold.
            from wifit3.chips.rtl8188eus.mac import is_chip_warm as _ic_warm
            if not _ic_warm(transport):
                phase_power(transport)
                phase_fw(transport, args.debug)
            phase_efuse(transport)
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
