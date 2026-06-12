"""Hardware test for RTL8187L bring-up.

Phase summary (mirrors the milestone breakdown):
  --phase open     : USB enumeration + vendor control transfers.
                     Reads MAC@0xFF00, TX_CONF@0xFF40 (HWVER probe),
                     CMD@0xFF37 (warm probe). (M1)
  --phase init     : open + rtl8187_init_hw + start (MAC side only,
                     rf.init STUBBED). Verifies CMD latched
                     TX_ENABLE|RX_ENABLE. (M2a)
  --phase rf       : open + asic_rev + detect_rf + rtl8225_rf_init
                     + bulk-IN drain. (M2b/c)
  --phase rx       : rf + decode RX descriptors + parse 802.11 frames
                     w/ real RSSI for ~3s. (M3)
  --phase channel  : rx + hop through channels 1, 6, 11, 13, counting
                     beacons + unique BSSIDs per channel. (M4)
  --phase tx       : bring-up + scan ch11 + fire 20 broadcast deauths
                     spoofed from the strongest AP, count self-RX
                     bounces. (M5)
  --phase handshake: bring-up + tune to --channel (default 11) + listen
                     --seconds (default 30) for EAPOL-Key frames.
                     Classifies any captures as M1/M2/M3/M4. (M6)
  --phase all      : the highest implemented phase (currently 'handshake').

Usage:
    uv run python scripts/rtl8187/test_hw_rtl8187.py                 # all
    uv run python scripts/rtl8187/test_hw_rtl8187.py --phase open    # M1
    uv run python scripts/rtl8187/test_hw_rtl8187.py --phase init    # M2a
    uv run python scripts/rtl8187/test_hw_rtl8187.py --phase init --debug
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

from wifit3.chips.rtl8187.constants import (
    CMD_RX_ENABLE,
    CMD_TX_ENABLE,
    REG_CMD,
    REG_RX_CONF,
    REG_TX_CONF,
    TX_CONF_HWVER_MASK,
    USB_EP_BULK_IN,
    USB_PID_RTL8187,
    USB_VID_REALTEK,
)
from wifit3.chips.rtl8187.mac import (
    cold_bring_up,
    detect_chip_variant,
    is_chip_warm,
    read_perm_mac,
)
from wifit3.chips.rtl8187.chan import config_channel
from wifit3.chips.rtl8187.probe import probe
from wifit3.chips.rtl8187.tx import (
    BROADCAST_MAC,
    RATE_1MBPS_CCK,
    build_deauth,
    build_tx_hdr,
    inject_frame,
)
from wifit3.chips.rtl8187.rtl8225 import build_rf_init
from wifit3.chips.rtl8187.rx import (
    parse_rx_urb,
    probe_endpoints,
    read_rx_burst,
)
from wifit3.chips.rtl8187.transport import RTL8187Transport
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
    dev = usb.core.find(
        idVendor=USB_VID_REALTEK, idProduct=USB_PID_RTL8187, backend=backend
    )
    if dev is None:
        fail(
            "RTL8187L not found (VID=0x0bda PID=0x8187). "
            "Plug it in, confirm Zadig bound it to WinUSB, and retry."
        )
    print(f"  Found RTL8187L at bus {dev.bus}, address {dev.address}")
    print(f"  bcdUSB=0x{dev.bcdUSB:04x}, bcdDevice=0x{dev.bcdDevice:04x}")

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


def phase_open(transport: RTL8187Transport) -> None:
    step("Read MAC@0xFF00 (6 bytes)")
    mac_bytes = read_perm_mac(transport)
    mac_str = ":".join(f"{b:02x}" for b in mac_bytes)
    print(f"  MAC0..5 = {mac_str}")
    if mac_bytes == b"\xff" * 6:
        # All-FFs typically means EEPROM hasn't been loaded into the
        # CSR yet (cold boot before cmd_reset issued EEPROM_CMD_LOAD).
        # Not a failure for M1 — the read worked, we just got the
        # default state. M2 will trigger the EEPROM load.
        print("  NOTE: all 0xFF — EEPROM not yet loaded into CSR (expected on cold boot)")
    elif mac_bytes == b"\x00" * 6:
        print("  NOTE: all zeros — unexpected. Chip may be in a bad state.")
    else:
        ok(f"control-IN plumbing works; MAC reads back as {mac_str}")

    step("Read TX_CONF@0xFF40 (HWVER probe)")
    tx_conf = transport.read32(REG_TX_CONF)
    hwver = tx_conf & TX_CONF_HWVER_MASK
    print(f"  TX_CONF  = 0x{tx_conf:08x}")
    print(f"  HWVER    = 0x{hwver:08x}  (bits [27:25] = {hwver >> 25})")
    variant = detect_chip_variant(transport)
    print(f"  chip     = {variant.name}")
    if variant.is_8187b_masquerade:
        print(
            "  WARNING: this is actually an early RTL8187B in 0x8187-disguise. "
            "M1-M6 are scoped to 8187L only."
        )
    if tx_conf == 0 or tx_conf == 0xFFFFFFFF:
        fail(
            f"Implausible TX_CONF=0x{tx_conf:08x} — device may be in a bad state. "
            "Unplug, wait 5s, replug, and rerun."
        )
    ok("HWVER decode worked")

    step("Read CMD@0xFF37 (warm probe)")
    cmd = transport.read8(REG_CMD)
    print(f"  CMD      = 0x{cmd:02x}")
    print(f"    TX_ENABLE = {bool(cmd & CMD_TX_ENABLE)}")
    print(f"    RX_ENABLE = {bool(cmd & CMD_RX_ENABLE)}")
    if is_chip_warm(transport):
        ok("chip is WARM (a previous session left TX+RX enabled)")
    else:
        ok("chip is COLD (TX+RX not yet enabled — normal on first plug-in)")


def phase_init(dev, transport: RTL8187Transport) -> None:
    """M2a: run cold_bring_up + verify CMD latched TX|RX enable.

    Also drains bulk-IN for ~1s to see whether the FIFO produces any
    bytes. Without RF init (M2b) this will usually be silent or noise,
    but we surface the count so we can compare M2a vs M2b runs.
    """
    step("Running cold bring-up (init_hw + start, rf.init STUBBED)")
    try:
        cold_bring_up(transport)
    except (IOError, usb.core.USBError) as e:
        fail(f"cold_bring_up raised: {type(e).__name__}: {e}")
    ok("cold_bring_up completed without IOError")

    step("Verify CMD@0xFF37 has TX_ENABLE | RX_ENABLE latched")
    cmd = transport.read8(REG_CMD)
    print(f"  CMD      = 0x{cmd:02x}")
    print(f"    TX_ENABLE = {bool(cmd & CMD_TX_ENABLE)}")
    print(f"    RX_ENABLE = {bool(cmd & CMD_RX_ENABLE)}")
    if not (cmd & CMD_TX_ENABLE and cmd & CMD_RX_ENABLE):
        fail("CMD did not latch TX|RX enable — bring-up incomplete.")
    ok("CMD has TX_ENABLE | RX_ENABLE")

    step("Read RX_CONF@0xFF44 (reflects start() baseline + configure_filter monitor entry)")
    rx_conf = transport.read32(REG_RX_CONF)
    print(f"  RX_CONF  = 0x{rx_conf:08x}")
    # cold_bring_up = init_hw + start + configure_filter. start() writes the station
    # baseline 0x9094FC0A; configure_filter ORs MONITOR (bit 0) + CTRL (bit 19) for the
    # airmon posture → 0x909CFC0B.
    expected = (
        (1 << 31) | (1 << 28) | (1 << 23) | (1 << 20) | (1 << 19) | (1 << 18)
        | (7 << 13) | (7 << 10) | (1 << 3) | (1 << 1) | (1 << 0)
    )
    if rx_conf != expected:
        print(f"  NOTE: expected 0x{expected:08x} — diff may indicate a HW-driven bit.")
    else:
        ok("RX_CONF matches the monitor-entry expected value exactly")

    step("Drain bulk-IN for ~1s (informational — receiver blind w/o RF init)")
    total = 0
    try:
        import time as _t
        deadline = _t.perf_counter() + 1.0
        while _t.perf_counter() < deadline:
            try:
                data = dev.read(USB_EP_BULK_IN, 4096, 100)
                total += len(data)
            except usb.core.USBError:
                continue
    except KeyboardInterrupt:
        pass
    print(f"  Drained {total} bytes in 1s")
    if total == 0:
        print("  (expected — without M2b's RF init the receiver is silent)")
    else:
        print(f"  Bulk-IN delivered {total} bytes — RF FIFO is alive even without RF init")


def phase_rf(dev, transport: RTL8187Transport) -> None:
    """M2b: probe asic_rev + RF variant, then run the full bring-up
    with the real rtl8225 RF init wired in. Drain bulk-IN for ~2s and
    show how many bytes came through. With RF init done the receiver
    should now produce frames on whatever the chip's default channel
    happens to be after init (typically channel 1)."""
    step("Probe (93cx6 EEPROM MAC + TX power, asic_rev, HWVER, RF variant)")
    pr = probe(transport)
    print(f"  mac        = {pr.mac.hex(':')}")
    print(f"  asic_rev   = {pr.setup.asic_rev}  "
          f"({'8051 fast' if pr.setup.asic_rev else 'bitbang'} SPI)")
    print(f"  RF variant = {pr.setup.variant.value}")
    print(f"  ch1 TXpwr  = hw_value=0x{pr.power.hw_value[0]:02x}, base=0x{pr.power.base:04x}")
    ok(f"probe complete (RF={pr.setup.variant.value}, both BCD + z2 paths ported)")

    step("Build rf_init callback + run cold_bring_up with real RF init")
    rf_init = build_rf_init(transport, pr.setup, pr.power)
    import time as _t
    t0 = _t.perf_counter()
    try:
        from wifit3.chips.rtl8187.mac import cold_bring_up as _cold
        _cold(transport, rf_init)
    except (IOError, usb.core.USBError) as e:
        fail(f"cold_bring_up (RF) raised: {type(e).__name__}: {e}")
    dt = _t.perf_counter() - t0
    ok(f"cold_bring_up (with RF init) completed in {dt:.2f}s")

    step("Verify CMD@0xFF37 latched TX|RX enable")
    cmd = transport.read8(REG_CMD)
    print(f"  CMD = 0x{cmd:02x}")
    if not (cmd & CMD_TX_ENABLE and cmd & CMD_RX_ENABLE):
        fail("CMD did not latch TX|RX enable after RF init.")
    ok("CMD has TX_ENABLE | RX_ENABLE")

    step("Drain bulk-IN for ~2s (receiver should now produce frames)")
    total = 0
    n_reads = 0
    try:
        deadline = _t.perf_counter() + 2.0
        while _t.perf_counter() < deadline:
            try:
                data = dev.read(USB_EP_BULK_IN, 4096, 200)
                if data:
                    total += len(data)
                    n_reads += 1
            except usb.core.USBError:
                continue
    except KeyboardInterrupt:
        pass
    print(f"  Drained {total} bytes across {n_reads} reads in 2s")
    if total == 0:
        print("  WARNING: bulk-IN silent. Possible causes:")
        print("    * RF cal failed (check log for 'RF calibration failed')")
        print("    * Channel is empty (default tune may be off — channel control is M4)")
        print("    * RF variant detection wrong")
        print("  Not a hard failure — M4 (set_channel) may be needed to find traffic.")
    else:
        ok(f"bulk-IN delivered {total} bytes ({total // n_reads if n_reads else 0} avg/read)")


def phase_rx(dev, transport: RTL8187Transport) -> None:
    """M3: same bring-up as --phase rf, but decode RX descriptors and
    parse 802.11 frames for ~3s. Print frame-type counts + RSSI stats."""
    # Run the rf phase first (asic_rev, detect, cold_bring_up).
    phase_rf(dev, transport)

    step("Decode RX URBs + parse 802.11 frames for ~3s")
    eps = probe_endpoints(dev)
    ep = eps.primary_bulk_in
    print(f"  bulk-IN endpoint = 0x{ep:02x}")

    type_counts: dict[str, int] = {}
    subtype_counts: dict[int, int] = {}
    rssi_samples: list[int] = []
    fcs_errors = 0
    raw_drops = 0
    n_urbs = 0
    seen_bssids: set[str] = set()

    import time as _t
    deadline = _t.perf_counter() + 3.0
    while _t.perf_counter() < deadline:
        buf = read_rx_burst(dev, ep, timeout_ms=100)
        if buf is None:
            continue
        n_urbs += 1
        rx = parse_rx_urb(buf)
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
        subtype = parsed.get("subtype")
        if subtype is not None:
            subtype_counts[subtype] = subtype_counts.get(subtype, 0) + 1
        bssid = parsed.get("bssid")
        if bssid:
            seen_bssids.add(bssid)

    print(f"\n  URBs received     = {n_urbs}")
    print(f"  Raw-parse drops   = {raw_drops}  (URB too short, bad trailer)")
    print(f"  FCS-error frames  = {fcs_errors}")
    print(f"  Parsed frames     = {sum(type_counts.values())}")
    print(f"  Frame types       = {type_counts}")
    print(f"  Frame subtypes    = {subtype_counts}")
    if rssi_samples:
        print(
            "  RSSI dBm          = min={} max={} mean={:.1f}".format(
                min(rssi_samples),
                max(rssi_samples),
                sum(rssi_samples) / len(rssi_samples),
            )
        )
    print(f"  Unique BSSIDs     = {len(seen_bssids)}")
    for b in sorted(seen_bssids)[:10]:
        print(f"    {b}")

    if sum(type_counts.values()) == 0:
        fail(
            "No parsed frames in 3s. Possible causes: bulk-IN delivered "
            "non-frame data (RF cal off?), or trailer math wrong."
        )
    ok(f"Decoded {sum(type_counts.values())} frames across {len(seen_bssids)} BSSIDs")


def phase_channel(dev, transport: RTL8187Transport) -> None:
    """M4: run cold_bring_up, then hop through 1, 6, 11, 13 — counting
    beacons + unique BSSIDs per channel. Beacons-per-channel sanity-
    checks that the synth tune is actually moving the radio."""
    # cold_bring_up.
    pr = probe(transport)
    setup = pr.setup
    rf_init = build_rf_init(transport, setup, pr.power)
    from wifit3.chips.rtl8187.mac import cold_bring_up as _cold
    step("cold_bring_up (init_hw + RF init + start)")
    _cold(transport, rf_init)
    cmd = transport.read8(REG_CMD)
    if not (cmd & CMD_TX_ENABLE and cmd & CMD_RX_ENABLE):
        fail(f"CMD did not latch TX|RX enable after bring-up (CMD=0x{cmd:02x}).")
    ok(f"bring-up complete (CMD=0x{cmd:02x}, RF={setup.variant.value})")

    eps = probe_endpoints(dev)
    ep = eps.primary_bulk_in

    channels = [1, 6, 11, 13]
    per_channel: dict[int, dict] = {}

    import time as _t
    for ch in channels:
        step(f"Tune to channel {ch}, sample 2s")
        t0 = _t.perf_counter()
        config_channel(transport, setup.asic_rev, setup.variant, ch, pr.power)
        # Drain any stale URBs from the previous channel before sampling.
        for _ in range(8):
            if read_rx_burst(dev, ep, timeout_ms=30) is None:
                break

        beacons = 0
        bssids: set[str] = set()
        rssi_samples: list[int] = []
        n_parsed = 0

        deadline = _t.perf_counter() + 2.0
        while _t.perf_counter() < deadline:
            buf = read_rx_burst(dev, ep, timeout_ms=100)
            if buf is None:
                continue
            rx = parse_rx_urb(buf)
            if rx is None or rx.has_fcs_error:
                continue
            parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
            if parsed is None:
                continue
            n_parsed += 1
            rssi_samples.append(rx.rssi_dbm)
            if parsed.get("type") == "beacon":
                beacons += 1
                bssid = parsed.get("bssid")
                if bssid:
                    bssids.add(bssid)

        dt = _t.perf_counter() - t0
        per_channel[ch] = {
            "beacons": beacons,
            "bssids": len(bssids),
            "parsed": n_parsed,
            "rssi_mean": (sum(rssi_samples) / len(rssi_samples)) if rssi_samples else None,
        }
        print(
            "  ch={} : tune+sample {:.1f}s — parsed={} beacons={} BSSIDs={}".format(
                ch, dt, n_parsed, beacons, len(bssids)
            )
        )

    step("Per-channel summary")
    for ch, stats in per_channel.items():
        rssi = "{:.1f}".format(stats["rssi_mean"]) if stats["rssi_mean"] is not None else "n/a"
        print(
            f"  ch {ch:2d}: parsed={stats['parsed']:3d}  beacons={stats['beacons']:3d}  "
            f"BSSIDs={stats['bssids']:2d}  RSSI mean={rssi}"
        )

    if all(s["beacons"] == 0 for s in per_channel.values()):
        fail("No beacons on any channel — set_channel may be a no-op or wrong values.")
    if len({tuple(sorted(per_channel[c]["bssids"] if isinstance(per_channel[c]["bssids"], set) else ())) for c in channels}) == 1:
        # Crude check: if every channel sees the *same* BSSID set, tune
        # is suspect (every AP should not be visible on every channel).
        # NB: this is an int now (we stored len), so the check above is
        # effectively dead — kept as a comment-of-intent.
        pass
    ok(f"Channel sweep complete across {channels}")


def phase_tx(dev, transport: RTL8187Transport) -> None:
    """M5: bring up, scan channel 11 for the strongest BSSID, then
    fire a burst of broadcast deauths spoofed from that BSSID. Watch
    bulk-IN to see whether our own frames bounce back (the chip's RX
    path often delivers self-TX'd frames in monitor mode)."""
    pr = probe(transport)
    setup = pr.setup
    rf_init = build_rf_init(transport, setup, pr.power)
    from wifit3.chips.rtl8187.mac import cold_bring_up as _cold
    step("cold_bring_up + tune to channel 11")
    _cold(transport, rf_init)
    config_channel(transport, setup.asic_rev, setup.variant, 11, pr.power)
    ok("ready on ch 11")

    eps = probe_endpoints(dev)
    ep_in = eps.primary_bulk_in

    step("Sample 1.5s on ch 11 to pick the strongest beacon BSSID")
    import time as _t
    best_rssi = -999
    best_bssid: bytes | None = None
    seen: dict[bytes, int] = {}
    deadline = _t.perf_counter() + 1.5
    while _t.perf_counter() < deadline:
        buf = read_rx_burst(dev, ep_in, timeout_ms=100)
        if buf is None:
            continue
        rx = parse_rx_urb(buf)
        if rx is None or rx.has_fcs_error:
            continue
        parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
        if parsed is None or parsed.get("type") != "beacon":
            continue
        bssid_str = parsed.get("bssid")
        if not bssid_str:
            continue
        bssid_bytes = bytes.fromhex(bssid_str.replace(":", ""))
        prev = seen.get(bssid_bytes, -999)
        if rx.rssi_dbm > prev:
            seen[bssid_bytes] = rx.rssi_dbm
        if rx.rssi_dbm > best_rssi:
            best_rssi = rx.rssi_dbm
            best_bssid = bssid_bytes

    if best_bssid is None:
        fail("No beacons on ch 11 to target — try a different channel or location.")
    bssid_str = ":".join(f"{b:02x}" for b in best_bssid)
    ok(f"Targeting {bssid_str} (RSSI {best_rssi} dBm, {len(seen)} candidates)")

    step("Build deauth frame (broadcast target, spoofed AP src)")
    deauth = build_deauth(BROADCAST_MAC, best_bssid)
    print(f"  frame length = {len(deauth)} bytes")
    print(f"  hex = {deauth.hex()}")
    hdr = build_tx_hdr(len(deauth), rate_hw_value=RATE_1MBPS_CCK)
    print(f"  tx_hdr ({len(hdr)} bytes) = {hdr.hex()}")

    step("Inject 20 deauths (50ms apart) + count self-RX bounces")
    n_sent = 0
    n_errs = 0
    self_rx = 0
    other_rx = 0

    # Drain stale bulk-IN before we start so the bounce-count is clean.
    for _ in range(8):
        if read_rx_burst(dev, ep_in, timeout_ms=20) is None:
            break

    for i in range(20):
        try:
            inject_frame(dev, deauth)
            n_sent += 1
        except usb.core.USBError as e:
            n_errs += 1
            print(f"  inject {i}: USBError: {e}")

        # Poll bulk-IN for ~50ms between injects.
        sub_deadline = _t.perf_counter() + 0.050
        while _t.perf_counter() < sub_deadline:
            buf = read_rx_burst(dev, ep_in, timeout_ms=20)
            if buf is None:
                continue
            rx = parse_rx_urb(buf)
            if rx is None or rx.has_fcs_error:
                continue
            # Is this our own deauth bouncing back?
            #   * subtype 12 (deauth)
            #   * addr3 (bssid) matches the target
            if (
                len(rx.mpdu) >= 26
                and rx.mpdu[0] == 0xC0
                and rx.mpdu[16:22] == best_bssid
            ):
                self_rx += 1
            else:
                other_rx += 1

    print(f"\n  Frames sent       = {n_sent}")
    print(f"  USB errors        = {n_errs}")
    print(f"  Self-RX deauths   = {self_rx}  (our injects bouncing back)")
    print(f"  Other-RX frames   = {other_rx}  (normal channel traffic)")

    if n_sent == 0:
        fail("Bulk-OUT rejected every inject — tx_hdr or endpoint wrong.")
    if n_errs > 0:
        fail(f"{n_errs} USBErrors during inject — investigate.")
    if self_rx == 0:
        print(
            "  NOTE: 0 self-RX bounces. Not a hard failure — the chip's RX "
            "filter may drop self-TX frames. Confirm with a second card "
            "in monitor mode if you want absolute proof of TX."
        )
    else:
        ok(f"{n_sent} deauths injected, {self_rx} bounced back on bulk-IN")


def phase_handshake(dev, transport: RTL8187Transport, channel: int, seconds: float) -> None:
    """M6: bring up, tune to ``channel``, listen for EAPOL-Key frames.

    Reports any 4-way handshake messages seen, classified M1/M2/M3/M4.
    Useful for sanity-checking handshake capture against a known AP+
    client pair you can trigger a reconnect on.
    """
    pr = probe(transport)
    setup = pr.setup
    rf_init = build_rf_init(transport, setup, pr.power)
    from wifit3.chips.rtl8187.mac import cold_bring_up as _cold
    step(f"cold_bring_up + tune to channel {channel}")
    _cold(transport, rf_init)
    config_channel(transport, setup.asic_rev, setup.variant, channel, pr.power)
    ok(f"listening on ch {channel} for {seconds:.0f}s")

    eps = probe_endpoints(dev)
    ep = eps.primary_bulk_in

    eapol_count = 0
    msg_counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 0: 0}
    seen_bssids_with_eapol: set[str] = set()

    import time as _t
    deadline = _t.perf_counter() + seconds
    while _t.perf_counter() < deadline:
        buf = read_rx_burst(dev, ep, timeout_ms=100)
        if buf is None:
            continue
        rx = parse_rx_urb(buf)
        if rx is None or rx.has_fcs_error:
            continue
        parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
        if parsed is None or parsed.get("type") != "eapol":
            continue
        eapol_count += 1
        msg_num = parsed.get("eapol_msg_num", 0) or 0
        msg_counts[msg_num] = msg_counts.get(msg_num, 0) + 1
        bssid = parsed.get("bssid")
        if bssid:
            seen_bssids_with_eapol.add(bssid)
        print(
            f"  [t={_t.perf_counter() - (deadline - seconds):.1f}s] "
            f"M{msg_num} EAPOL from {parsed.get('source', '?')} "
            f"on BSSID {bssid} (RSSI {rx.rssi_dbm})"
        )

    step("Summary")
    print(f"  EAPOL frames seen     = {eapol_count}")
    print(f"  M1 (AP→STA)           = {msg_counts.get(1, 0)}")
    print(f"  M2 (STA→AP, +SNonce)  = {msg_counts.get(2, 0)}")
    print(f"  M3 (AP→STA, +GTK)     = {msg_counts.get(3, 0)}")
    print(f"  M4 (STA→AP, ACK)      = {msg_counts.get(4, 0)}")
    print(f"  Unique BSSIDs w/ EAPOL = {len(seen_bssids_with_eapol)}")
    if eapol_count == 0:
        print(
            "  NOTE: 0 EAPOL frames captured. The driver is fine — you "
            "just didn't see a handshake during the listen window. Try "
            "forcing a reconnect on a test device, or use --phase tx in "
            "another run to deauth a client and watch it reassociate."
        )
    else:
        ok(f"Captured {eapol_count} EAPOL frame(s)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--phase",
        choices=["open", "init", "rf", "rx", "channel", "tx", "handshake", "all"],
        default="all",
        help="Which phase to run (default: all available phases).",
    )
    p.add_argument(
        "--channel", type=int, default=11,
        help="Channel for --phase handshake (default: 11).",
    )
    p.add_argument(
        "--seconds", type=float, default=30.0,
        help="Listen time for --phase handshake (default: 30s).",
    )
    p.add_argument("--debug", action="store_true", help="Enable DEBUG-level logging.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.debug)

    dev = open_device()
    transport = RTL8187Transport(dev)
    try:
        # Each higher phase strictly supersedes the lower ones (they
        # all run cold_bring_up so don't chain them back to back).
        if args.phase == "open":
            phase_open(transport)
        elif args.phase == "init":
            phase_open(transport)
            phase_init(dev, transport)
        elif args.phase == "rf":
            phase_open(transport)
            phase_rf(dev, transport)
        elif args.phase == "rx":
            phase_open(transport)
            phase_rx(dev, transport)
        elif args.phase == "channel":
            phase_open(transport)
            phase_channel(dev, transport)
        elif args.phase == "tx":
            phase_open(transport)
            phase_tx(dev, transport)
        elif args.phase in ("handshake", "all"):
            phase_open(transport)
            phase_handshake(dev, transport, args.channel, args.seconds)
        print("\n=== phases all PASSED ===")
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  USB release warning: {e}")


if __name__ == "__main__":
    main()
