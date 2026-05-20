"""Hardware test for RTL8812AU (AWUS036ACH).

This is the USER-run harness. It does no UI work and exits with status 1
on any failure so the output can be piped/grepped.

Phases:
  --phase open     : USB enumeration + a vendor read of REG_SYS_CFG1 to
                     confirm WinUSB control transfers work + warm/cold probe.
  --phase fw       : open + cold-boot + power_seq + LLT init + FW upload +
                     poll BIT_FWDL_CHK_RPT ACK. Skipped on a warm chip.
  --phase validate : fw + CPU reset + poll FW_READY_LEGACY (proves the
                     wlan CPU is *running* the firmware). M1 pass-line.
  --phase mac_init : validate + post-FW MAC init. Pass-line: REG_CR has
                     BIT_MACTXEN | BIT_MACRXEN set. M2-b pass-line.
  --phase phy      : mac_init + 5 init tables + switch_band(2G/20MHz). Pass-
                     line: BB enable bits stuck. M2-d pass-line.
  --phase channel  : phy + set_channel_2g_20mhz(1). Pass-line: REG_BWINDICATION
                     reads back correctly. M3-a pass-line.
  --phase beacon   : channel + ~5s of RX polling, prints distinct BSSIDs.
                     Pass-line: >=1 beacon seen. M3-b pass-line.
  --phase all      : open through beacon (default).

Usage:
    .venv\\Scripts\\python.exe scratch\\test_hw_rtl8812au.py             # all
    .venv\\Scripts\\python.exe scratch\\test_hw_rtl8812au.py --phase open
    .venv\\Scripts\\python.exe scratch\\test_hw_rtl8812au.py --debug

If you see "USB pipe error" or the chip seems wedged: unplug, wait a few
seconds, replug, and rerun. WinUSB can't always recover bulk pipes from
a previous session.
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

from wifit3.chips.rtl8812au.constants import (
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    USB_PID_AWUS036ACH,
    USB_VID_REALTEK,
)
from wifit3.chips.rtl8812au.firmware import (
    download_firmware_legacy,
    download_firmware_validate_legacy,
    en_download_firmware_legacy,
    load_firmware_blob,
)
from wifit3.chips.rtl8812au.constants import BIT_MACRXEN, BIT_MACTXEN, REG_CR
from wifit3.chips.rtl8812au.mac import (
    ChipState,
    is_chip_warm,
    mac_power_on,
    post_fw_mac_init,
    pre_fw_init,
    probe_chip_state,
)
from wifit3.chips.rtl8812au.chan import (
    CHANNELS_5G_ALL,
    channel_band_is_2g,
    set_channel_2g_20mhz,
    set_channel_5g_20mhz,
)
from wifit3.chips.rtl8812au.efuse import efuse_defaults_from_read, read_efuse_8812a
from wifit3.chips.rtl8812au.phy import (
    EfuseDefaults,
    post_mac_init_phy,
    switch_band_2g_20mhz,
    switch_band_5g_20mhz,
)
from wifit3.chips.rtl8812au.rx import iter_bulk_frames, probe_endpoints, read_rx_burst
from wifit3.chips.rtl8812au.transport import RTL8812AUTransport
from wifit3.chips.rtl8812au.tx import (
    TX_DESC_QSEL_MGMT,
    build_deauth_frame,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
    write_bulk,
)
from wifit3.wlan.packet import WlanFrameParser


def _parse_mac(s: str) -> bytes:
    parts = s.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"bad MAC: {s}")
    return bytes(int(p, 16) for p in parts)
from wifit3.chips.rtw88_base.firmware_legacy import FW_READY_LEGACY
from wifit3.chips.rtw88_base.registers import (
    BIT_FWDL_CHK_RPT,
    BIT_MCUFWDL_EN,
    REG_MCUFW_CTRL,
)


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
        idVendor=USB_VID_REALTEK, idProduct=USB_PID_AWUS036ACH, backend=backend
    )
    if dev is None:
        fail(
            f"AWUS036ACH not found (VID=0x{USB_VID_REALTEK:04x} "
            f"PID=0x{USB_PID_AWUS036ACH:04x}). Plug it in, confirm Zadig "
            "bound it to WinUSB, and retry."
        )
    print(f"  Found AWUS036ACH at bus {dev.bus}, address {dev.address}")
    try:
        speed = dev.speed
    except Exception:
        speed = "?"
    speed_name = {1: "LOW", 2: "FULL", 3: "HIGH", 4: "SUPER", 5: "SUPER_PLUS"}.get(
        speed, "?"
    )
    print(f"  USB speed: {speed} ({speed_name})  bcdUSB: 0x{dev.bcdUSB:04x}")
    if speed == 4 or speed == 5:
        print(
            "  WARNING: device negotiated SuperSpeed. The 8812au usually runs as "
            "USB 2.0 HighSpeed; SuperSpeed introduces WinUSB caveats. Test "
            "anyway, but watch for stalls."
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


def phase_open(transport: RTL8812AUTransport) -> None:
    step("Read REG_SYS_CFG1 (0x00F0) via vendor control")
    val = transport.read32(REG_SYS_CFG1)
    print(f"  REG_SYS_CFG1 = 0x{val:08x}")
    if val == 0 or val == 0xFFFFFFFF:
        fail(
            f"Implausible value 0x{val:08x} — device may be in a bad state. "
            "Unplug, wait 5s, replug, and rerun."
        )
    ok("control-transfer plumbing works")

    val2 = transport.read32(REG_SYS_CFG2)
    print(f"  REG_SYS_CFG2 = 0x{val2:08x}")

    step("Warm-state probe")
    mcufw = transport.read32(REG_MCUFW_CTRL)
    cr = transport.read8(REG_CR)
    print(f"  REG_MCUFW_CTRL = 0x{mcufw:08x}")
    print(f"  REG_CR (byte0) = 0x{cr:02x}  "
          f"MACTXEN={bool(cr & BIT_MACTXEN)} MACRXEN={bool(cr & BIT_MACRXEN)}")
    state = probe_chip_state(transport)
    print(f"  Chip state: {state.value.upper()}")


def phase_efuse(transport: RTL8812AUTransport) -> EfuseDefaults:
    step("EFUSE read (rfe_option, ext_lna/pa, crystal_cap, MAC addr)")
    import time as _t
    t0 = _t.perf_counter()
    try:
        read = read_efuse_8812a(transport)
    except IOError as e:
        fail(f"EFUSE read failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    print(f"  Took {dt_ms:.0f} ms.")
    print(f"  Raw fields:")
    print(f"    pa_type        = 0x{read.pa_type:02x}")
    print(f"    lna_type_2g    = 0x{read.lna_type_2g:02x}")
    print(f"    lna_type_5g    = 0x{read.lna_type_5g:02x}")
    print(f"    rfe_option_raw = 0x{read.rfe_option_raw:02x}")
    print(f"    rf_board_opt   = 0x{read.rf_board_option:02x}")
    print(f"    crystal_cap    = 0x{read.crystal_cap:02x}")
    print(f"    mac_addr       = {':'.join(f'{b:02x}' for b in read.mac_addr)}")
    print(f"  Derived:")
    print(f"    ext_pa_2g      = {read.ext_pa_2g}")
    print(f"    ext_lna_2g     = {read.ext_lna_2g}")
    print(f"    ext_pa_5g      = {read.ext_pa_5g}")
    print(f"    ext_lna_5g     = {read.ext_lna_5g}")
    print(f"    btcoex         = {read.btcoex}")
    print(f"    rfe_option     = {read.rfe_option}   (resolved from raw)")
    ok("EFUSE read OK — these values now drive phy/RF config on cold boots.")
    return efuse_defaults_from_read(read, rf_path_num=2)


def phase_fw(transport: RTL8812AUTransport, debug: bool):
    """Cold or warm path. If FW is already running, skip the upload.

    Returns FifoConf so phase_mac_init has the queue layout. On a warm
    chip we recompute it (pure-Python, no hardware I/O).
    """
    from wifit3.chips.rtl8812au.fifo import set_trx_fifo_info
    state = probe_chip_state(transport)
    if state is not ChipState.COLD:
        step(f"Chip is {state.value.upper()} — skipping FW upload + validate")
        ok("FW already running; nothing to do.")
        return set_trx_fifo_info()
    return _phase_fw_cold(transport, debug)


def _phase_fw_cold(transport: RTL8812AUTransport, debug: bool):
    step("MAC power-on (rf_reset + pre-sys-cfg + pwr_seq + init-sys-cfg)")
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
        fifo_local = pre_fw_init(transport)
    except IOError as e:
        fail(f"pre_fw_init failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"LLT init done in {dt_ms:.1f} ms (rsvd_boundary={fifo_local.rsvd_boundary})")
    fifo = fifo_local

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
    print(f"  Loaded {len(fw)} bytes ({body} body + 32B stub header)")

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


def phase_validate(transport: RTL8812AUTransport) -> None:
    # On a warm chip, this is also fine — it re-toggles the CPU and
    # re-polls FW_READY_LEGACY. Idempotent.
    step("Validate FW is running (CPU reset + FW_READY_LEGACY poll)")
    ok_run, last = download_firmware_validate_legacy(transport)
    print(f"  REG_MCUFW_CTRL = 0x{last:08x}")
    bits = []
    if last & 0x02: bits.append("MCUFWDL_RDY")
    if last & 0x04: bits.append("FWDL_CHK_RPT")
    if last & 0x40: bits.append("WINTINI_RDY")
    if last & 0x80: bits.append("RAM_DL_SEL")
    print(f"  Set bits in FW_READY_LEGACY mask: {bits or '(none)'}")
    if not ok_run:
        fail(
            f"FW_READY_LEGACY (mask 0x{FW_READY_LEGACY:02x}) never satisfied — "
            "firmware not running."
        )
    ok("FW_READY_LEGACY satisfied — wlan CPU is running the firmware. M1 DONE.")


def phase_mac_init(transport: RTL8812AUTransport, fifo) -> None:
    if probe_chip_state(transport) is ChipState.FULLY_WARM:
        step("Chip is already FULLY_WARM — skipping MAC init")
        ok("REG_CR already has MACTXEN | MACRXEN set.")
        return

    step("Post-FW MAC init (queue tables + EDCA + ARFB + MACTXEN/RXEN)")
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
    ok("REG_CR has MACTXEN | MACRXEN set — MAC enabled. M2-b DONE.")


def phase_phy(transport: RTL8812AUTransport, efuse: EfuseDefaults | None = None) -> EfuseDefaults:
    step("Post-MAC PHY init (5 tables + switch_band 2G/20MHz)")
    if efuse is None:
        efuse = EfuseDefaults()
    print(
        f"  EfuseDefaults: rfe_option={efuse.rfe_option}  "
        f"ext_lna_2g={efuse.ext_lna_2g}  ext_pa_2g={efuse.ext_pa_2g}  "
        f"ext_lna_5g={efuse.ext_lna_5g}  ext_pa_5g={efuse.ext_pa_5g}"
    )
    import time as _t
    t0 = _t.perf_counter()
    try:
        post_mac_init_phy(transport, efuse)
    except IOError as e:
        fail(f"post_mac_init_phy failed: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"PHY init completed in {dt_ms:.1f} ms")

    # Sentinel: RF path A and path B should respond to a SIPI write.
    # The LSSI_WRITE_{A,B} registers should now reflect *something*
    # other than zero (they were written many times during table load).
    # Lighter check: read REG_SYS_FUNC_EN and confirm BB reset bits stuck.
    from wifit3.chips.rtl8812au.constants import (
        BIT_FEN_BB_GLB_RST, BIT_FEN_BB_RSTB, BIT_FEN_USBA, REG_SYS_FUNC_EN,
    )
    sfe = transport.read8(REG_SYS_FUNC_EN)
    print(f"  REG_SYS_FUNC_EN = 0x{sfe:02x}")
    print(f"    BIT_FEN_USBA       = {bool(sfe & BIT_FEN_USBA)}")
    print(f"    BIT_FEN_BB_RSTB    = {bool(sfe & BIT_FEN_BB_RSTB)}")
    print(f"    BIT_FEN_BB_GLB_RST = {bool(sfe & BIT_FEN_BB_GLB_RST)}")
    missing = []
    if not (sfe & BIT_FEN_USBA): missing.append("USBA")
    if not (sfe & BIT_FEN_BB_RSTB): missing.append("BB_RSTB")
    if not (sfe & BIT_FEN_BB_GLB_RST): missing.append("BB_GLB_RST")
    if missing:
        fail(f"BB enable bits not set: {missing}. phy_bb_config didn't stick?")
    ok("BB enable bits set — PHY tables loaded successfully. M2-d DONE.")

    # Post-PHY readback: did the rfe pinmux writes actually land?
    from wifit3.chips.rtl8812au.constants import (
        REG_RFE_INV_A, REG_RFE_INV_B, REG_RFE_PINMUX_A, REG_RFE_PINMUX_B,
        REG_TXSCALE_A, REG_TXSCALE_B,
    )
    pin_a = transport.read32(REG_RFE_PINMUX_A)
    pin_b = transport.read32(REG_RFE_PINMUX_B)
    inv_a = transport.read32(REG_RFE_INV_A)
    inv_b = transport.read32(REG_RFE_INV_B)
    sca_a = transport.read32(REG_TXSCALE_A)
    sca_b = transport.read32(REG_TXSCALE_B)
    from wifit3.chips.rtl8812au.constants import (
        REG_RCR, REG_RX_DRVINFO_SZ as RDR_SZ,
    )
    rcr = transport.read32(REG_RCR)
    drvsz = transport.read8(RDR_SZ)
    print(f"  REG_RCR          = 0x{rcr:08x}  "
          f"APP_PHYSTS={bool(rcr & (1 << 28))} "
          f"AAP={bool(rcr & 1)} AB={bool(rcr & (1<<3))} AM={bool(rcr & (1<<2))} APM={bool(rcr & (1<<1))}")
    print(f"  REG_RX_DRVINFO_SZ = 0x{drvsz:02x}  (expected 0x04)")
    print(f"  REG_RFE_PINMUX_A = 0x{pin_a:08x}  REG_RFE_PINMUX_B = 0x{pin_b:08x}")
    print(f"  REG_RFE_INV_A    = 0x{inv_a:08x}  REG_RFE_INV_B    = 0x{inv_b:08x}")
    print(f"  REG_TXSCALE_A    = 0x{sca_a:08x}  REG_TXSCALE_B    = 0x{sca_b:08x}")
    print(f"  Expected for rfe={efuse.rfe_option}:")
    if efuse.rfe_option == 0:
        print(f"    PINMUX_A/B = 0x77777777  INV_A/B & 0x3FF00000 = 0x00000000")
    elif efuse.rfe_option == 3:
        print(f"    PINMUX_A/B = 0x54337770  INV_A/B & 0x3FF00000 = 0x01000000")
    elif efuse.rfe_option == 4:
        print(f"    PINMUX_A/B = 0x77777777  INV_A/B & 0x3FF00000 = 0x00100000")
    return efuse


def phase_channel(transport: RTL8812AUTransport, channel: int = 1,
                  efuse: EfuseDefaults | None = None) -> None:
    is_2g = channel_band_is_2g(channel)
    band = "2.4 GHz" if is_2g else "5 GHz"
    step(f"Set channel {channel} @ 20 MHz / {band} (2T2R)")
    if efuse is None:
        efuse = EfuseDefaults()
    import time as _t
    t0 = _t.perf_counter()
    try:
        # post_mac_init_phy leaves the chip in 2G state; if asked for 5G,
        # band-switch first.
        if not is_2g:
            switch_band_5g_20mhz(transport, efuse)
            set_channel_5g_20mhz(transport, channel)
        else:
            set_channel_2g_20mhz(transport, channel)
    except IOError as e:
        fail(f"set_channel failed: {e}")
    except ValueError as e:
        fail(f"unsupported channel: {e}")
    dt_ms = (_t.perf_counter() - t0) * 1000
    ok(f"Channel set in {dt_ms:.1f} ms")

    # Sanity: REG_BWINDICATION bits[1:0] = 1 (2G) or 2 (5G).
    from wifit3.chips.rtl8812au.constants import REG_BWINDICATION
    bw = transport.read32(REG_BWINDICATION)
    expected = 1 if is_2g else 2
    print(f"  REG_BWINDICATION = 0x{bw:08x}  (bits[1:0]={bw & 0x3}, expected {expected})")
    if (bw & 0x3) != expected:
        fail(
            f"REG_BWINDICATION bits[1:0] should be {expected} after "
            f"switch_band_{('2g' if is_2g else '5g')}_20mhz, got {bw & 0x3}"
        )
    ok(f"Channel + band sanity OK. {'M3-a' if is_2g else 'MX-d'} DONE.")


def phase_beacon(dev, transport: RTL8812AUTransport, duration_s: float = 5.0) -> None:
    step(f"Listen for beacons (channel 1, {duration_s:.0f}s)")

    eps = probe_endpoints(dev)
    if not eps.bulk_in:
        fail("no bulk-IN endpoint found")
    ep_in = eps.primary_bulk_in
    print(f"  bulk-IN endpoint: 0x{ep_in:02x}")

    seen_bssids: dict[str, int] = {}
    seen_ssids: dict[str, str] = {}
    rssi_by_bssid: dict[str, list[int]] = {}
    subtype_counts: dict[int, int] = {}
    total_frames = 0
    bursts = 0
    bytes_received = 0
    crc_errors = 0
    import time as _t
    t_start = _t.perf_counter()

    first_burst_dump_done = False
    while _t.perf_counter() - t_start < duration_s:
        buf = read_rx_burst(dev, ep_in, max_size=16384, timeout_ms=200)
        if buf is None:
            continue
        bursts += 1
        bytes_received += len(buf)
        if not first_burst_dump_done:
            print(f"  First-burst raw dump ({len(buf)} bytes):")
            print(f"    rx_pkt_desc[0..24]   = {buf[:24].hex()}")
            if len(buf) >= 24:
                w0 = int.from_bytes(buf[0:4], "little")
                print(f"    w0 = 0x{w0:08x}  "
                      f"pkt_len={w0 & 0x3FFF}  "
                      f"drv_info_sz_field={(w0 >> 16) & 0xF}  "
                      f"phy_status_present={bool(w0 & (1 << 26))}  "
                      f"shift={(w0 >> 24) & 0x3}")
            if len(buf) >= 56:
                print(f"    phy_status[24..56]   = {buf[24:56].hex()}")
            first_burst_dump_done = True
        for stat, mpdu, rssi in iter_bulk_frames(buf):
            total_frames += 1
            if stat.crc_err:
                crc_errors += 1
            # Track subtype-id of EVERY frame, not just successfully-parsed.
            if len(mpdu) >= 1:
                fc0 = mpdu[0]
                subtype = (fc0 >> 4) & 0x0F
                ftype = (fc0 >> 2) & 0x03
                key = (ftype << 4) | subtype
                subtype_counts[key] = subtype_counts.get(key, 0) + 1
            parsed = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100
            )
            if not parsed:
                continue
            if parsed.get("subtype_id") == WlanFrameParser.SUBTYPE_BEACON:
                bssid = parsed.get("bssid") or "?"
                seen_bssids[bssid] = seen_bssids.get(bssid, 0) + 1
                if bssid not in seen_ssids:
                    seen_ssids[bssid] = _extract_ssid(mpdu)
                if rssi is not None:
                    rssi_by_bssid.setdefault(bssid, []).append(rssi)

    print(f"  Bursts received: {bursts}, total bytes: {bytes_received}")
    print(f"  Parsed frames:   {total_frames}  (CRC-errored: {crc_errors})")
    if subtype_counts:
        print(f"  Frame-type histogram (type<<4 | subtype):")
        for k, v in sorted(subtype_counts.items(), key=lambda kv: -kv[1])[:8]:
            ftype = (k >> 4) & 0x3
            subtype = k & 0xF
            ftype_name = {0: "MGMT", 1: "CTL", 2: "DATA", 3: "EXT"}.get(ftype, "?")
            print(f"    {ftype_name}/sub={subtype}: {v}")
    print(f"  Distinct beacon BSSIDs: {len(seen_bssids)}")
    for bssid, count in sorted(seen_bssids.items(), key=lambda kv: -kv[1])[:20]:
        ssid = seen_ssids.get(bssid, "")
        rssis = rssi_by_bssid.get(bssid, [])
        if rssis:
            rssi_str = f"  rssi=[{min(rssis):d}..{max(rssis):d}] dBm"
        else:
            rssi_str = "  rssi=?"
        print(f"    {bssid}  ({count:3d} beacons){rssi_str}  ssid={ssid!r}")

    if len(seen_bssids) == 0:
        fail("no beacons captured — RX path not delivering 802.11 frames")
    ok(f"{len(seen_bssids)} distinct BSSIDs visible on channel 1. M3-b DONE.")


def phase_tx(dev, transport: RTL8812AUTransport, target_bssid: str,
             target_client: str, count: int) -> None:
    step(f"Send {count} deauth frame(s)  BSSID={target_bssid}  client={target_client}")
    try:
        ap = _parse_mac(target_bssid)
        client = _parse_mac(target_client)
    except ValueError as e:
        fail(f"bad MAC: {e}")

    # Pre-TX state dump — verify the chip is actually configured to TX.
    from wifit3.chips.rtl8812au.constants import (
        BIT_MACRXEN, BIT_MACTXEN, REG_CR, REG_RQPN, REG_RQPN_NPQ,
        REG_TXDMA_PQ_MAP,
    )
    cr = transport.read32(REG_CR)
    rqpn = transport.read32(REG_RQPN)
    rqpn_npq = transport.read32(REG_RQPN_NPQ)
    pq_map = transport.read16(REG_TXDMA_PQ_MAP)
    print(f"  Pre-TX state:")
    print(f"    REG_CR             = 0x{cr:08x}  MACTXEN={bool(cr & BIT_MACTXEN)} MACRXEN={bool(cr & BIT_MACRXEN)}")
    print(f"    REG_RQPN           = 0x{rqpn:08x}  (BIT31=LD_RQPN, [7:0]=hq, [15:8]=lq, [23:16]=pubq)")
    print(f"    REG_RQPN_NPQ       = 0x{rqpn_npq:08x}  ([7:0]=nq, [23:16]=exq)")
    print(f"    REG_TXDMA_PQ_MAP   = 0x{pq_map:04x}  (bits 4..15 hold VOQ/VIQ/BEQ/BKQ/MGQ/HIQ lane assignments)")
    if not (cr & BIT_MACTXEN):
        fail("BIT_MACTXEN clear — MAC TX path disabled. post_fw_mac_init didn't run or got clobbered.")

    # Defensive: re-arm queue priority + reserved-page allocation just before
    # TX. The pre-TX readback consistently shows REG_RQPN=0 and REG_TXDMA_PQ_MAP=0
    # which strongly suggests these are write-once "load" registers OR
    # something later in init clears them. Re-running shouldn't hurt and
    # might unstick a wedged queue.
    print(f"  Re-arming queue priority + reserved page mapping...")
    from wifit3.chips.rtl8812au.fifo import set_trx_fifo_info
    from wifit3.chips.rtl8812au.mac import (
        init_queue_priority,
        init_queue_reserved_page,
        init_tx_buffer_boundary,
    )
    fifo = set_trx_fifo_info()
    init_queue_reserved_page(transport, fifo)
    init_tx_buffer_boundary(transport, fifo)
    init_queue_priority(transport)
    rqpn2 = transport.read32(REG_RQPN)
    pq_map2 = transport.read16(REG_TXDMA_PQ_MAP)
    print(f"  Post-rearm: REG_RQPN=0x{rqpn2:08x}  REG_TXDMA_PQ_MAP=0x{pq_map2:04x}")

    eps = probe_endpoints(dev)
    if not eps.bulk_out:
        fail("no bulk-OUT endpoints found")
    ep = pick_bulk_out_ep(eps.bulk_out, queue=TX_DESC_QSEL_MGMT)
    print(f"  bulk-OUT for MGMT: 0x{ep:02x} (out of {[hex(e) for e in eps.bulk_out]})")

    # Defensive: clear any halt on the bulk-OUT pipe — sometimes WinUSB
    # leaves it in a stalled state from a previous session.
    try:
        dev.clear_halt(ep)
        print(f"  cleared halt on 0x{ep:02x}")
    except (usb.core.USBError, NotImplementedError) as e:
        print(f"  clear_halt(0x{ep:02x}) skipped: {e}")

    deauth = build_deauth_frame(ap, client, reason=7)
    desc = build_tx_desc_mgmt(deauth, band_is_2g=True)
    payload = desc + deauth
    print(f"  deauth MPDU: {len(deauth)}B  tx_desc: {len(desc)}B  total: {len(payload)}B")
    print(f"  MPDU hex:    {deauth.hex()}")
    print(f"  desc hex:    {desc.hex()}")

    import time as _t
    successes = 0
    for i in range(count):
        try:
            sent = write_bulk(dev, ep, payload, timeout_ms=200)
            if sent == len(payload):
                successes += 1
                print(f"  [{i+1:3d}/{count}] bulk-OUT OK ({sent}B)")
            else:
                print(f"  [{i+1:3d}/{count}] SHORT WRITE {sent}/{len(payload)}B")
        except usb.core.USBError as e:
            print(f"  [{i+1:3d}/{count}] FAIL: {e}")
        _t.sleep(0.05)   # 50ms between frames

    # If MGMT queue failed entirely, try HIGH queue as a fallback (different
    # bulk-OUT EP, different reserved-page pool). This proves whether the
    # issue is queue-specific or chip-wide TX failure.
    if successes == 0:
        from wifit3.chips.rtl8812au.tx import TX_DESC_QSEL_HIGH
        ep_high = pick_bulk_out_ep(eps.bulk_out, queue=TX_DESC_QSEL_HIGH)
        print(f"\n  Fallback: trying HIGH queue on 0x{ep_high:02x}")
        # Rebuild desc with QSEL=HIGH
        import struct as _struct
        desc2 = bytearray(desc)
        w1 = _struct.unpack_from("<I", desc2, 4)[0]
        w1 = (w1 & ~(0x1F << 8)) | ((TX_DESC_QSEL_HIGH & 0x1F) << 8)
        _struct.pack_into("<I", desc2, 4, w1)
        # Re-checksum
        from wifit3.chips.rtw88_base.tx_common import fill_txdesc_checksum
        # Zero out W7 first so checksum is consistent
        _struct.pack_into("<I", desc2, 7*4, 0)
        fill_txdesc_checksum(desc2, num_u16_words=16, w7_byte_offset=7*4)
        payload2 = bytes(desc2) + deauth
        try:
            sent = write_bulk(dev, ep_high, payload2, timeout_ms=200)
            print(f"    HIGH queue write: sent={sent}B  ({'OK' if sent == len(payload2) else 'SHORT'})")
        except usb.core.USBError as e:
            print(f"    HIGH queue FAIL: {e}")

    print(f"\n  Sent {successes}/{count} deauth frames to 0x{ep:02x}.")
    if successes == 0:
        fail("0 deauths sent — chip refused bulk-OUT entirely. Likely MAC TX disabled.")
    if successes < count:
        print("  (Partial — some bulk-OUTs failed; chip may be backing off.)")
    ok(f"{successes}/{count} deauth bursts went out on the wire. "
       "Watch the target client (phone connected to NETGEAR2G) — it should "
       "show a brief disconnect/reconnect.")


def _extract_ssid(mpdu: bytes) -> str:
    """SSID IE in a beacon: 24B 802.11 hdr + 12B fixed body, then IEs.

    Returns hex if non-UTF-8.
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
        return mpdu[ies_off + 2: ies_off + 2 + ssid_len].decode("utf-8")
    except UnicodeDecodeError:
        return mpdu[ies_off + 2: ies_off + 2 + ssid_len].hex()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--phase",
        choices=("open", "efuse", "fw", "validate", "mac_init", "phy",
                 "channel", "beacon", "tx", "all"),
        default="all",
    )
    p.add_argument("--debug", action="store_true", help="verbose USB + FW logging")
    p.add_argument("--channel", type=int, default=1, help="channel to tune (1..13)")
    p.add_argument("--beacon-secs", type=float, default=5.0,
                   help="how long to listen for beacons (phase=beacon)")
    p.add_argument("--rfe", type=int, default=0, choices=range(0, 7),
                   help="rfe_option for phy init (0..6). Try 3 for AWUS036ACH "
                        "high-power ext-LNA+ext-PA cards. Default 0 matches "
                        "the kernel's USB-with-unset-EFUSE default.")
    p.add_argument("--ext-lna", action="store_true",
                   help="set ext_lna_2g + ext_lna_5g in EfuseDefaults (changes "
                        "phy_cond bitfield-rfe branches in BB/AGC tables).")
    p.add_argument("--ext-pa", action="store_true",
                   help="set ext_pa_2g + ext_pa_5g in EfuseDefaults.")
    p.add_argument("--target-bssid", default="aa:bb:cc:dd:ee:01",
                   help="BSSID to spoof as source of deauth (default NETGEAR2G).")
    p.add_argument("--target-client", default="ff:ff:ff:ff:ff:ff",
                   help="Destination MAC for deauth. Default broadcast = "
                        "all clients on the BSS.")
    p.add_argument("--tx-count", type=int, default=10,
                   help="How many deauth frames to send (phase=tx).")
    args = p.parse_args()
    setup_logging(args.debug)

    step("USB discovery + claim")
    dev = open_device()
    ok("Interface 0 claimed")
    transport = RTL8812AUTransport(dev)

    needs_efuse = ("efuse", "all")
    needs_fw = ("fw", "validate", "mac_init", "phy", "channel", "beacon", "tx", "all")
    needs_validate = ("validate", "mac_init", "phy", "channel", "beacon", "tx", "all")
    needs_mac_init = ("mac_init", "phy", "channel", "beacon", "tx", "all")
    needs_phy = ("phy", "channel", "beacon", "tx", "all")
    needs_channel = ("channel", "beacon", "tx", "all")
    needs_beacon = ("beacon", "all")    # not strictly needed for tx
    needs_tx = ("tx", "all")
    fifo = None
    efuse_from_chip: EfuseDefaults | None = None

    try:
        if args.phase in ("open", "all"):
            phase_open(transport)
        if args.phase in needs_efuse:
            efuse_from_chip = phase_efuse(transport)
        if args.phase in needs_fw:
            fifo = phase_fw(transport, args.debug)
        if args.phase in needs_validate:
            phase_validate(transport)
        if args.phase in needs_mac_init:
            phase_mac_init(transport, fifo)
        if args.phase in needs_phy:
            # Priority: --rfe/--ext-lna/--ext-pa CLI args override (for
            # empirical testing). Otherwise use the EFUSE-derived defaults
            # if --phase all (which runs phase_efuse first). Otherwise
            # fall back to bare EfuseDefaults().
            cli_overrides = (args.rfe != 0 or args.ext_lna or args.ext_pa)
            if cli_overrides:
                efuse = EfuseDefaults(
                    rfe_option=args.rfe,
                    ext_lna_2g=int(args.ext_lna),
                    ext_pa_2g=int(args.ext_pa),
                    ext_lna_5g=int(args.ext_lna),
                    ext_pa_5g=int(args.ext_pa),
                )
            elif efuse_from_chip is not None:
                efuse = efuse_from_chip
            else:
                efuse = EfuseDefaults()
            phase_phy(transport, efuse)
        if args.phase in needs_channel:
            # Pass the same efuse we used for phy so RFE pinmux is consistent
            # across band-switch when tuning to 5G.
            efuse_for_chan = (
                efuse_from_chip
                if (efuse_from_chip is not None and not (args.rfe or args.ext_lna or args.ext_pa))
                else EfuseDefaults(
                    rfe_option=args.rfe,
                    ext_lna_2g=int(args.ext_lna),
                    ext_pa_2g=int(args.ext_pa),
                    ext_lna_5g=int(args.ext_lna),
                    ext_pa_5g=int(args.ext_pa),
                )
            )
            phase_channel(transport, args.channel, efuse_for_chan)
        if args.phase in needs_beacon:
            phase_beacon(dev, transport, args.beacon_secs)
        if args.phase in needs_tx:
            phase_tx(dev, transport, args.target_bssid, args.target_client,
                     args.tx_count)
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
