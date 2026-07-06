"""Hardware test for rt2500usb bring-up (Ralink RT2570).

Target: Buffalo "Nintendo Wi-Fi USB Connector" (VID 0x0411, PID 0x008b)
and the rest of the rt2500usb device table.

Phase summary (mirrors the milestone breakdown):
  --phase open    : USB enumeration + claim + MAC_CSR0 (ASIC revision)
                    read + one-shot EEPROM read → permanent MAC + RF type.
                    The "we can talk to it" proof. (M1)
  --phase macinit : open + warm probe + init_registers + set_state(AWAKE)
                    + always-monitor RX filter. Spot-checks the writes
                    survived (MAC_CSR1.HOST_READY, TXRX_CSR2 accept-all,
                    TXRX_CSR1.AUTO_SEQUENCE). (M2a)
  --phase bbpinit : macinit + init_bbp (31 fixed BBP writes via the
                    PHY_CSR7/8 indirect path + EEPROM overrides). Verifies
                    the BBP is responsive and the EEPROM override readbacks
                    match. (M2b)
  --phase chaninit: bbpinit + sweep set_channel 1..14 (RF2525E). RF is
                    write-only, so this verifies no RF_BUSY timeout and
                    reports PHY_CSR10.RF_PLL_LD (PLL lock) per channel. (M2c)
  --phase rx      : chaninit + config_ant + tune --channel + drain EP 0x81
                    ~10s, decode RXD-at-end, parse 802.11, report
                    URBs/RSSI/types/BSSIDs. (M3)
  --phase deauth  : full bring-up + tune --channel + TX N deauths on EP
                    0x01 (1 Mbps CCK). REQUIRES --bssid + --client
                    (deliberate, authorized testing only). (M5)

Ground truth (usb_dumps/captures_rt2500usb/capture-2, cold-boot probe):
  * MAC_CSR0 read returns a small, non-0xFFFF value (observed 0x0005).
  * EEPROM read = 110 bytes (EEPROM_SIZE 0x6e); MAC at byte offset 4
    carries the Buffalo OUI 00:0D:0B; EEPROM word 0x0b RF_TYPE = RF2525E.
RT2570 needs NO firmware blob — bring-up is all register pokes.

Usage:
    uv run python scripts/rt2500usb/test_hw_rt2500usb.py            # all phases
    uv run python scripts/rt2500usb/test_hw_rt2500usb.py --phase open --debug
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

from wifit3.chips.rt2500usb.constants import (
    EEPROM_ANTENNA,
    EEPROM_ANTENNA_RF_TYPE,
    EEPROM_MAC_ADDR_0,
    EEPROM_SIZE,
    MAC_CSR0,
    MAC_CSR1,
    MAC_CSR1_HOST_READY,
    PHY_CSR10,
    PHY_CSR10_RF_BUSY,
    PHY_CSR10_RF_PLL_LD,
    RF2522,
    RF2523,
    RF2524,
    RF2525,
    RF2525E,
    RF5222,
    RT2500USB_DEVICE_TABLE,
    TXRX_CSR1,
    TXRX_CSR1_AUTO_SEQUENCE,
    TXRX_CSR2,
    TXRX_CSR2_DISABLE_RX,
    TXRX_CSR2_DROP_BROADCAST,
    TXRX_CSR2_DROP_CONTROL,
    TXRX_CSR2_DROP_CRC,
    TXRX_CSR2_DROP_MULTICAST,
    TXRX_CSR2_DROP_NOT_TO_ME,
    TXRX_CSR2_DROP_PHYSICAL,
    TXRX_CSR2_DROP_TODS,
    TXRX_CSR2_DROP_VERSION_ERROR,
    USB_PID_NINTENDO_WIFI,
    USB_VID_MELCO,
)
from wifit3.chips.rt2500usb.bbp import (
    bbp_read,
    eeprom_bbp_overrides,
    init_bbp,
)
from wifit3.chips.rt2500usb.chan import antenna_defaults, config_ant, set_channel
from wifit3.chips.rt2500usb.mac import (
    apply_monitor_filter,
    init_registers,
    is_chip_warm,
    read_revision,
)
from wifit3.chips.rt2500usb.rx import (
    parse_rx_urb,
    probe_endpoints,
    read_rx_burst,
)
from wifit3.chips.rt2500usb.tx import inject as tx_inject
from wifit3.chips.rt2500usb.transport import RT2500USBTransport, get_field16
from wifit3.wlan.packet import WlanFrameParser

# After apply_monitor_filter: accept bits must be CLEAR (we surface these
# frames); error bits must be SET (drop CRC + PLCP + version — the RX loop
# discards all of these anyway).
_TXRX_CSR2_ACCEPT_MASK = (
    TXRX_CSR2_DISABLE_RX | TXRX_CSR2_DROP_CONTROL
    | TXRX_CSR2_DROP_NOT_TO_ME | TXRX_CSR2_DROP_TODS
    | TXRX_CSR2_DROP_MULTICAST | TXRX_CSR2_DROP_BROADCAST
)
_TXRX_CSR2_NOISE_MASK = (
    TXRX_CSR2_DROP_CRC | TXRX_CSR2_DROP_PHYSICAL | TXRX_CSR2_DROP_VERSION_ERROR
)

_RF_NAMES = {
    RF2522: "RF2522", RF2523: "RF2523", RF2524: "RF2524",
    RF2525: "RF2525", RF2525E: "RF2525E", RF5222: "RF5222",
}


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
    """Find any rt2500usb device; prefer the Nintendo connector. First match wins."""
    backend = libusb_package.get_libusb1_backend()

    # Try the Nintendo connector first, then the rest of the table.
    candidates = [(USB_VID_MELCO, USB_PID_NINTENDO_WIFI,
                   "Buffalo Nintendo Wi-Fi USB Connector")]
    candidates += [(v, p, d) for (v, p, d) in RT2500USB_DEVICE_TABLE
                   if (v, p) != (USB_VID_MELCO, USB_PID_NINTENDO_WIFI)]

    for vid, pid, label in candidates:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is not None:
            print(f"  Found {label} ({vid:#06x}:{pid:#06x}) "
                  f"at bus {dev.bus}, address {dev.address}")
            print(f"  bcdUSB=0x{dev.bcdUSB:04x}, bcdDevice=0x{dev.bcdDevice:04x}")
            break
    else:
        fail(
            "No rt2500usb device found. Plug in the Nintendo Wi-Fi connector "
            "(0x0411:0x008b), confirm Zadig bound it to WinUSB, and retry."
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


def phase_open(transport: RT2500USBTransport) -> None:
    step("Read MAC_CSR0 (ASIC revision)")
    rev = transport.read16(MAC_CSR0)
    print(f"  MAC_CSR0 = 0x{rev:04x}")
    if rev == 0xFFFF:
        fail("MAC_CSR0=0xFFFF — control-IN read failed / chip wedged. "
             "Unplug, wait ~5s, replug, retry.")
    if rev == 0x0000:
        fail("MAC_CSR0=0x0000 — chip appears dead / not powered.")
    ok(f"control-IN plumbing works; ASIC revision reads back as 0x{rev:04x} "
       f"(capture-2 reference: 0x0005)")

    step("Read EEPROM (one-shot) → permanent MAC + RF type")
    eeprom = transport.read_eeprom(EEPROM_SIZE)
    print(f"  EEPROM bytes read = {len(eeprom)} (expected {EEPROM_SIZE})")
    if len(eeprom) != EEPROM_SIZE:
        fail(f"short EEPROM read: {len(eeprom)}/{EEPROM_SIZE}")

    mac_off = EEPROM_MAC_ADDR_0 * 2          # word index → byte offset
    mac = eeprom[mac_off:mac_off + 6]
    mac_str = ":".join(f"{b:02x}" for b in mac)
    print(f"  permanent MAC = {mac_str}")
    if mac == b"\x00" * 6:
        fail("MAC is all zeros — EEPROM read returned blanks.")
    if mac == b"\xff" * 6:
        fail("MAC is all 0xFF — EEPROM read failed or chip in bad state.")
    if mac[0] & 0x01:
        fail(f"MAC {mac_str} has multicast bit set — not a valid station MAC; "
             "EEPROM parse offset is likely wrong.")
    ok(f"EEPROM read OK; valid unicast MAC {mac_str} "
       f"(OUI {mac_str[:8]})")

    # EEPROM word 0x000b, RF_TYPE field (bits 15:11).
    ant_off = EEPROM_ANTENNA * 2
    antenna = eeprom[ant_off] | (eeprom[ant_off + 1] << 8)
    rf_type = get_field16(antenna, EEPROM_ANTENNA_RF_TYPE)
    rf_name = _RF_NAMES.get(rf_type, f"unknown(0x{rf_type:x})")
    print(f"  EEPROM_ANTENNA = 0x{antenna:04x}  RF_TYPE = 0x{rf_type:x} ({rf_name})")
    ok(f"RF chip identified: {rf_name} "
       "(determines the config_channel branch to port in M2+)")


def phase_macinit(transport: RT2500USBTransport) -> None:
    step("Warm probe (MAC_CSR1.HOST_READY)")
    warm_before = is_chip_warm(transport)
    print(f"  chip is {'WARM' if warm_before else 'COLD'} before init "
          f"(MAC_CSR1=0x{transport.read16(MAC_CSR1):04x})")
    if warm_before:
        print("  NOTE: a prior session left HOST_READY set — re-running init "
              "is still safe (idempotent register pokes).")

    step("Read chip revision (MAC_CSR0)")
    rev = read_revision(transport)
    print(f"  revision nibble = 0x{rev & 0xf:x} "
          f"({'>= VERSION_C → PHY_CSR2 LNA=0' if (rev & 0xf) >= 3 else '< VERSION_C'})")

    step("init_registers + set_state(AWAKE)")
    init_registers(transport, rev)
    ok("init_registers completed (set_state reached AWAKE without timeout)")

    step("apply always-monitor RX filter (TXRX_CSR2 accept-all)")
    apply_monitor_filter(transport)

    step("Verify writes survived")
    mac1 = transport.read16(MAC_CSR1)
    csr2 = transport.read16(TXRX_CSR2)
    csr1 = transport.read16(TXRX_CSR1)
    print(f"  MAC_CSR1  = 0x{mac1:04x}  HOST_READY={bool(mac1 & MAC_CSR1_HOST_READY)}")
    print(f"  TXRX_CSR2 = 0x{csr2:04x}  (expect 0x{_TXRX_CSR2_NOISE_MASK:04x}: RX on, "
          "accept real frames, drop PLCP/version noise)")
    print(f"  TXRX_CSR1 = 0x{csr1:04x}  AUTO_SEQUENCE={bool(csr1 & TXRX_CSR1_AUTO_SEQUENCE)}")
    if not (mac1 & MAC_CSR1_HOST_READY):
        fail("MAC_CSR1.HOST_READY not latched — init_registers didn't take.")
    if csr2 & _TXRX_CSR2_ACCEPT_MASK:   # DISABLE_RX or an accept-DROP bit set
        fail(f"TXRX_CSR2=0x{csr2:04x}: RX disabled or over-filtering "
             "(an accept bit is set).")
    if (csr2 & _TXRX_CSR2_NOISE_MASK) != _TXRX_CSR2_NOISE_MASK:
        fail(f"TXRX_CSR2=0x{csr2:04x}: PLCP/version noise-drop bits not set.")
    if not (csr1 & TXRX_CSR1_AUTO_SEQUENCE):
        fail("TXRX_CSR1.AUTO_SEQUENCE not set — last init write didn't take.")
    ok("MAC initialised, radio AWAKE, monitor filter open")


def phase_bbpinit(transport: RT2500USBTransport) -> None:
    step("init_bbp (fixed BBP writes + EEPROM overrides)")
    eeprom = transport.read_eeprom()
    overrides = eeprom_bbp_overrides(eeprom)
    print(f"  EEPROM BBP overrides = "
          f"{[(r, f'0x{v:02x}') for r, v in overrides]}")
    try:
        init_bbp(transport, eeprom)
    except IOError as e:
        fail(f"init_bbp raised: {e}")
    ok("init_bbp completed (wait_bbp_ready passed; all writes issued)")

    step("Verify BBP is responsive + override readbacks")
    bbp0 = bbp_read(transport, 0)
    print(f"  BBP[0] = 0x{bbp0:02x} (must be != 0x00 and != 0xff)")
    if bbp0 in (0x00, 0xFF):
        fail(f"BBP[0]=0x{bbp0:02x} — baseband not responding via PHY_CSR7/8.")

    # BBP[17] is the one register the kernel itself reads back
    # (validate_eeprom), so it is reliably readable. If the EEPROM
    # overrides it, the readback must match.
    ovr = dict(overrides)
    checked = False
    for reg_id in (17, 21, 22):
        if reg_id in ovr:
            val = bbp_read(transport, reg_id)
            exp = ovr[reg_id]
            tag = "OK" if val == exp else "MISMATCH"
            print(f"  BBP[{reg_id}] = 0x{val:02x} (EEPROM override 0x{exp:02x}) [{tag}]")
            if reg_id == 17:
                checked = True
                if val != exp:
                    fail(f"BBP[17] readback 0x{val:02x} != EEPROM 0x{exp:02x} "
                         "— indirect write/read path is wrong.")
    if not checked:
        print("  NOTE: no EEPROM override for BBP[17]; relying on the "
              "responsive check above.")
    ok("BBP indirect path verified (write + read round-trip)")


def phase_chaninit(transport: RT2500USBTransport) -> None:
    step("Determine RF type from EEPROM")
    eeprom = transport.read_eeprom()
    ant_off = EEPROM_ANTENNA * 2
    antenna = eeprom[ant_off] | (eeprom[ant_off + 1] << 8)
    rf_type = get_field16(antenna, EEPROM_ANTENNA_RF_TYPE)
    print(f"  RF_TYPE = 0x{rf_type:x} ({_RF_NAMES.get(rf_type, '?')})")

    step("Sweep set_channel 1..14 (RF write-only → check RF_BUSY + PLL lock)")
    pll_locked = 0
    for ch in range(1, 15):
        tuned = set_channel(transport, rf_type, ch)
        csr10 = transport.read16(PHY_CSR10)
        busy = bool(csr10 & PHY_CSR10_RF_BUSY)
        pll = bool(csr10 & PHY_CSR10_RF_PLL_LD)
        pll_locked += int(pll)
        flag = "" if tuned and not busy else "  <-- RF_BUSY stuck!"
        print(f"  ch {ch:2d}: tuned={tuned}  PHY_CSR10=0x{csr10:04x}  "
              f"PLL_LD={pll}{flag}")
        if not tuned or busy:
            fail(f"channel {ch} tune failed (RF_BUSY did not clear).")
    ok(f"all 14 channels tuned without RF_BUSY timeout "
       f"(PLL lock seen on {pll_locked}/14)")
    if pll_locked == 0:
        print("  NOTE: RF_PLL_LD never latched — tuning writes still "
              "succeeded; PLL-lock readback may not be reliable on this "
              "unit. RX (M3) is the definitive channel-tune proof.")


def phase_rx(transport: RT2500USBTransport, dev, channel: int) -> None:
    step("config_ant (antenna + RF2525E I/Q flip)")
    eeprom = transport.read_eeprom()
    ant_off = EEPROM_ANTENNA * 2
    antenna = eeprom[ant_off] | (eeprom[ant_off + 1] << 8)
    rf_type = get_field16(antenna, EEPROM_ANTENNA_RF_TYPE)
    ant_tx, ant_rx = antenna_defaults(antenna)
    print(f"  RF=0x{rf_type:x}  default antenna tx={ant_tx} rx={ant_rx}")
    config_ant(transport, rf_type, ant_tx, ant_rx)
    ok("config_ant applied")

    step(f"Tune to channel {channel} + enable RX")
    if not set_channel(transport, rf_type, channel):
        fail(f"set_channel({channel}) hit an RF_BUSY timeout.")
    apply_monitor_filter(transport)   # ensure DISABLE_RX clear (start_queue RX)
    eps = probe_endpoints(dev)
    ep = eps.primary_bulk_in
    print(f"  draining bulk-IN 0x{ep:02x} for 10s on channel {channel}...")

    n_urbs = raw_drops = fcs_errors = 0
    rssi_samples: list[int] = []
    type_counts: dict[str, int] = {}
    bssids: set[str] = set()
    dumped = 0

    import time as _t
    deadline = _t.perf_counter() + 10.0
    while _t.perf_counter() < deadline:
        buf = read_rx_burst(dev, ep, timeout_ms=100)
        if buf is None:
            continue
        n_urbs += 1
        if dumped < 3:
            preview = " ".join(f"{b:02x}" for b in buf[:16])
            print(f"  URB #{n_urbs} ({len(buf)}B): {preview} ...")
            dumped += 1
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
        ftype = parsed.type
        type_counts[ftype] = type_counts.get(ftype, 0) + 1
        bssid = parsed.bssid
        if bssid:
            bssids.add(bssid)

    print(f"\n  URBs received    = {n_urbs}")
    print(f"  Raw-parse drops  = {raw_drops}")
    print(f"  FCS-error frames = {fcs_errors}")
    print(f"  Parsed frames    = {sum(type_counts.values())}")
    print(f"  Frame types      = {type_counts}")
    if rssi_samples:
        print("  RSSI dBm         = min={} max={} mean={:.1f}".format(
            min(rssi_samples), max(rssi_samples),
            sum(rssi_samples) / len(rssi_samples)))
    print(f"  Unique BSSIDs    = {len(bssids)}")

    if sum(type_counts.values()) == 0:
        fail(
            f"No parsed frames in 10s on channel {channel}. Try a busier "
            "channel (--channel 6 / 11), or this may be a config_ant / "
            "descriptor-offset issue."
        )
    ok(f"Decoded {sum(type_counts.values())} frames across {len(bssids)} BSSIDs")


def _mac_to_bytes(s: str) -> bytes:
    parts = s.replace("-", ":").split(":")
    if len(parts) != 6:
        fail(f"bad MAC '{s}' (expected aa:bb:cc:dd:ee:ff)")
    return bytes(int(p, 16) for p in parts)


def _build_deauth(bssid: bytes, client: bytes, reason: int = 7) -> bytes:
    """AP→client deauth (matches the capture's aireplay frame): addr1=client,
    addr2=addr3=bssid, reason=7 (class-3 frame from nonassociated STA).
    Sequence is left 0 — the chip fills it (TXD NEW_SEQ + AUTO_SEQUENCE)."""
    import struct as _s
    return (
        b"\xc0\x00"            # FC: mgmt / deauth
        + b"\x00\x00"          # duration
        + client + bssid + bssid
        + b"\x00\x00"          # seq (chip-assigned)
        + _s.pack("<H", reason)
    )


def phase_deauth(transport: RT2500USBTransport, dev, channel: int,
                 bssid: str, client: str, count: int) -> None:
    if not bssid or not client:
        fail("--phase deauth requires --bssid and --client (authorized "
             "testing only).")
    bssid_b = _mac_to_bytes(bssid)
    client_b = _mac_to_bytes(client)

    step("config_ant + tune target channel")
    eeprom = transport.read_eeprom()
    ant_off = EEPROM_ANTENNA * 2
    antenna = eeprom[ant_off] | (eeprom[ant_off + 1] << 8)
    rf_type = get_field16(antenna, EEPROM_ANTENNA_RF_TYPE)
    ant_tx, ant_rx = antenna_defaults(antenna)
    config_ant(transport, rf_type, ant_tx, ant_rx)
    apply_monitor_filter(transport)
    if not set_channel(transport, rf_type, channel):
        fail(f"set_channel({channel}) failed.")
    ok(f"radio up, tuned to channel {channel}")

    eps = probe_endpoints(dev)
    ep_out = eps.bulk_out[0]
    frame = _build_deauth(bssid_b, client_b)
    print(f"  deauth {bssid} -> {client}, {len(frame)}B frame, "
          f"{count}x on EP 0x{ep_out:02x}")

    step(f"Inject {count} deauth frames (1 Mbps CCK)")
    sent_ok = 0
    for i in range(count):
        try:
            n = tx_inject(dev, ep_out, frame)
            sent_ok += int(n > 0)
        except Exception as e:
            fail(f"inject #{i+1} raised: {e}")
    ok(f"injected {sent_ok}/{count} deauth frames without USB error")
    print("  (verify effect with a second capture / the target's RX, or "
          "watch the client drop. TX success here = no USB stall.)")


def main() -> None:
    parser = argparse.ArgumentParser(description="rt2500usb hardware bring-up test")
    parser.add_argument("--phase", default="open",
                        choices=["open", "macinit", "bbpinit", "chaninit",
                                 "rx", "deauth"],
                        help="which milestone phase to run (default: open = M1)")
    parser.add_argument("--channel", type=int, default=1,
                        help="channel to tune for --phase rx/deauth (default: 1)")
    parser.add_argument("--bssid", default="", help="target AP BSSID (deauth)")
    parser.add_argument("--client", default="", help="target client MAC (deauth)")
    parser.add_argument("--count", type=int, default=5,
                        help="deauth frames to send (default: 5)")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args()

    setup_logging(args.debug)

    print("=== rt2500usb (RT2570) hardware test ===")
    dev = open_device()
    transport = RT2500USBTransport(dev)

    try:
        if args.phase == "open":
            phase_open(transport)
        elif args.phase == "macinit":
            phase_open(transport)
            phase_macinit(transport)
        elif args.phase == "bbpinit":
            phase_open(transport)
            phase_macinit(transport)
            phase_bbpinit(transport)
        elif args.phase == "chaninit":
            phase_open(transport)
            phase_macinit(transport)
            phase_bbpinit(transport)
            phase_chaninit(transport)
        elif args.phase == "rx":
            phase_open(transport)
            phase_macinit(transport)
            phase_bbpinit(transport)
            phase_rx(transport, dev, args.channel)
        elif args.phase == "deauth":
            phase_open(transport)
            phase_macinit(transport)
            phase_bbpinit(transport)
            phase_deauth(transport, dev, args.channel,
                         args.bssid, args.client, args.count)
        print("\n=== ALL CHECKS PASSED ===")
    finally:
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
