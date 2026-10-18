"""Hardware smoke test for RTL8192EU (TP-Link TL-WN823N v2/v3, USB 2357:0109).

Runs the ACTUAL driver bring-up path, on real silicon, on this machine. The card must be
bound to WinUSB (Zadig/wdi-simple) and released from any VM holding it.

Phases (each builds on the previous):
  --phase open    : USB discovery + claim + REG_SYS_CFG read (chip is responsive).
  --phase bringup : open + driver.connect() cold bring-up (power-on -> FW -> EFUSE ->
                    MAC -> BB/AGC/RF -> LC/IQK -> monitor RX), timed; dumps the chip
                    state the port derived (MAC, crystal cap, TX-power tables).
  --phase scan    : bringup + hop the 2.4 GHz band collecting beacons; reports
                    BSSID / SSID / RSSI / channel. Default.
  --phase warm    : read is_chip_warm() without a full connect (run right after a
                    bringup, with the chip still powered, to see the warm verdict).
  --phase inject  : bringup + fire --count unicast MGMT frames at a real AP and count the
                    AP's link-layer ACKs with the driver's ACK tap. The only single-card
                    proof that injection reaches the air (the AP is the prober). Control:
                    the same frames sent to a MAC nobody owns.

--phase inject fires real TX at --bssid (default: the designated injection target). The
frame is a unicast deauth whose source is a locally-administered MAC that is associated
nowhere, so the AP ACKs it at the MAC layer and no client is affected.

Usage:
    uv run python scripts/chips/rtl8192eu/test_hw_rtl8192eu.py
    uv run python scripts/chips/rtl8192eu/test_hw_rtl8192eu.py --phase bringup --debug
    uv run python scripts/chips/rtl8192eu/test_hw_rtl8192eu.py --phase scan --dwell 0.6 --channels 1-13
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8192eu import SUPPORTED_IDS
from wifit3.chips.rtl8192eu.constants import REG_SYS_CFG
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver
from wifit3.chips.rtl8192eu.mac import is_chip_warm
from wifit3.chips.rtl8192eu.transport import RTL8192EUTransport
from wifit3.dot11 import build_deauth
from wifit3.dot11.parser import WlanFrameParser

USB_VID_TPLINK = 0x2357
USB_PID_TL_WN823N_V2_V3 = 0x0109


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
        idVendor=USB_VID_TPLINK, idProduct=USB_PID_TL_WN823N_V2_V3, backend=backend
    )
    if dev is None:
        fail(
            "TL-WN823N v2/v3 not found (VID=0x2357 PID=0x0109). Confirm it is bound to "
            "WinUSB, is not captured by a VM, then replug and retry."
        )
    print(f"  Found TL-WN823N v2/v3 at bus {dev.bus}, address {dev.address}")
    print(f"  bcdUSB = 0x{dev.bcdUSB:04x}, bcdDevice = 0x{dev.bcdDevice:04x}")
    return dev


def _claim(dev) -> None:
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    usb.util.claim_interface(dev, 0)


def _release(dev) -> None:
    try:
        usb.util.release_interface(dev, 0)
        usb.util.dispose_resources(dev)
    except usb.core.USBError as e:
        print(f"  (release warning: {e})")


def phase_open(dev) -> None:
    step("Read REG_SYS_CFG (0x00F0)")
    t = RTL8192EUTransport(dev)
    val = t.read32(REG_SYS_CFG)
    print(f"  REG_SYS_CFG = 0x{val:08x}")
    if val in (0, 0xFFFFFFFF):
        fail(
            f"Implausible value 0x{val:08x} — device may be wedged. "
            "Unplug, wait 5 s, replug, and rerun."
        )
    ok("vendor-control reads work (chip is responsive)")
    print(f"  is_chip_warm() = {is_chip_warm(t)}")


async def phase_bringup(driver: RTL8192EUDriver) -> None:
    step("driver.connect() — cold bring-up")

    def progress(pct: float, msg: str) -> None:
        print(f"    [{int(pct * 100):3d}%] {msg}")

    t0 = time.perf_counter()
    try:
        connected = await driver.connect(progress_cb=progress)
    except Exception as e:  # BringUpError and friends
        fail(f"connect() raised: {type(e).__name__}: {e}")
    dt_ms = (time.perf_counter() - t0) * 1000
    if not connected:
        fail("connect() returned False")
    ok(f"connect() returned True in {dt_ms:.0f} ms")

    e = driver._efuse  # noqa: SLF001 — diagnostic view of the parsed EFUSE defaults
    print(f"  is_warm            = {driver.is_warm}")
    print(f"  mac_address        = {driver.mac_address}")
    print(f"  current_channel    = {driver.current_channel}")
    print(f"  crystal cap        = 0x{e.default_crystal_cap:02x}")
    print(f"  cck_tx_power   (A) = {[f'0x{x:02x}' for x in e.cck_tx_power_index_A]}")
    print(f"  ht40_1s_tx_pwr (A) = {[f'0x{x:02x}' for x in e.ht40_1s_tx_power_index_A]}")
    if driver.mac_address is None:
        print("  NOTE: MAC is None — EFUSE read failed or is unprogrammed (fallback defaults).")
    ok("cold bring-up completed end-to-end")


def _parse_channels(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


async def phase_scan(driver: RTL8192EUDriver, channels: list[int], dwell_s: float) -> None:
    step(f"Channel scan: {channels} @ {dwell_s:.2f}s dwell")
    seen: dict[str, dict] = {}
    tally = {"frames": 0, "beacons": 0}

    def on_frame(frame) -> None:
        tally["frames"] += 1
        if frame.subtype_id != WlanFrameParser.SUBTYPE_BEACON:
            return
        tally["beacons"] += 1
        bssid = frame.bssid or "?"
        rssi = frame.rssi
        rec = seen.get(bssid)
        if rec is None:
            seen[bssid] = {
                "ssid": frame.ssid or "",
                "rssi": rssi,
                "count": 1,
                "channel": getattr(frame, "channel", None) or driver.current_channel,
                "enc": getattr(frame, "encryption", None),
            }
        else:
            rec["count"] += 1
            if rssi is not None and (rec["rssi"] is None or rssi > rec["rssi"]):
                rec["rssi"] = rssi

    driver.register_rx_callback(on_frame)

    for ch in channels:
        await driver.set_channel(ch)
        await asyncio.sleep(dwell_s)
        print(f"    ch {ch:2d}: {len(seen):2d} BSSIDs, {tally['beacons']:4d} beacons so far")

    print()
    print(f"  Frames parsed: {tally['frames']}, beacons: {tally['beacons']}")
    print(f"  Distinct BSSIDs: {len(seen)}")
    for bssid, rec in sorted(
        seen.items(), key=lambda kv: (kv[1]["rssi"] is None, -(kv[1]["rssi"] or -127))
    )[:30]:
        rssi_str = f"{rec['rssi']:+4d} dBm" if rec["rssi"] is not None else "  --  "
        print(
            f"    {bssid}  ch {rec['channel']:>2}  {rssi_str}  "
            f"({rec['count']:3d} beacons)  enc={rec['enc']}  ssid={rec['ssid']!r}"
        )

    if not seen:
        fail(
            "no beacons captured — RX path is not delivering 802.11 frames. "
            "Check REG_RCR / bulk-IN endpoint / channel set."
        )
    ok(f"{len(seen)} distinct BSSIDs visible across {len(channels)} channels")


def _mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


async def phase_inject(
    driver: RTL8192EUDriver, bssid: str, channel: int, count: int, interval: float
) -> None:
    """Fire `count` unicast MGMT frames at a real AP and count that AP's ACKs."""
    target = _mac_bytes(bssid)
    src = _mac_bytes("02:b0:b0:00:00:01")
    bogus = _mac_bytes("02:00:00:00:00:99")

    step("Arm the RX ACK tap (REG_RXFLTMAP1 bit 13)")
    await driver.enable_rx_acks()
    await driver.set_channel(channel)
    print(f"  tuned to ch {channel}; target AP {bssid}")

    async def fire(dst: bytes) -> int:
        frame = build_deauth(dst, src, dst, reason=7)   # a1=dst, a2=src, a3=dst
        before = driver.acks_seen(src)
        sent = 0
        for _ in range(count):
            if await driver.inject_frame(frame):
                sent += 1
            await asyncio.sleep(interval)
        return driver.acks_seen(src) - before if sent else 0

    step(f"Fire {count} frames at the AP (expect the AP's ACKs back to our source)")
    acked = await fire(target)
    print(f"    a1={bssid}  ->  {acked}/{count} ACK(s) seen for 02:b0:b0:00:00:01")

    step("Control: same frames to a MAC nobody owns (expect ~0)")
    nowhere = await fire(bogus)
    print(f"    a1=02:00:00:00:00:99  ->  {nowhere}/{count} ACK(s)")

    margin = max(count // 4, 5)
    if acked > max(nowhere, margin):
        ok(f"injection reaches the air: the AP ACKed {acked}/{count} of our frames")
    else:
        fail(
            f"no AP ACKs ({acked}/{count}, control {nowhere}) — either the frames never "
            "left the radio or the AP is out of range/moved off this channel."
        )


def phase_warm(dev) -> None:
    step("is_chip_warm() probe")
    t = RTL8192EUTransport(dev)
    warm = is_chip_warm(t)
    print(f"  is_chip_warm() = {warm}")
    if warm:
        ok("Chip is WARM — a previous session left FW running + MAC enabled.")
        print("  -> driver.connect() would take the warm reattach path.")
    else:
        ok("Chip is COLD — full bring-up required.")
        print("  -> run --phase bringup first, then --phase warm to see the warm path.")


async def run(args) -> int:
    entry = next(d for d in SUPPORTED_IDS if d.vid == USB_VID_TPLINK
                 and d.pid == USB_PID_TL_WN823N_V2_V3)
    step("USB discovery")
    dev = open_device()
    print(f"  chipset = {entry.chipset}, model = {entry.product_name}")

    if args.phase == "open":
        _claim(dev)
        try:
            phase_open(dev)
        finally:
            _release(dev)
        print("\nALL PASS")
        return 0

    if args.phase == "warm":
        _claim(dev)
        try:
            phase_warm(dev)
        finally:
            _release(dev)
        print("\nALL PASS")
        return 0

    driver = RTL8192EUDriver.from_usb_device(dev, entry)
    try:
        await phase_bringup(driver)
        if args.phase == "scan":
            channels = _parse_channels(args.channels)
            await phase_scan(driver, channels, args.dwell)
        elif args.phase == "inject":
            await phase_inject(driver, args.bssid, args.target_channel, args.count, args.interval)
    finally:
        step("driver.close()")
        await driver.close()
        print("  closed")

    print("\nALL PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--phase", choices=("open", "bringup", "scan", "warm", "inject"), default="scan")
    p.add_argument("--debug", action="store_true", help="verbose USB + FW logging")
    p.add_argument("--channels", default="1-13", help="channel spec for scan, e.g. 1-13 or 1,6,11")
    p.add_argument("--dwell", type=float, default=0.5, help="seconds to listen per channel")
    p.add_argument("--bssid", default="c0:06:c3:c9:ec:0b",
                   help="inject target AP (default: the designated bench target)")
    p.add_argument("--target-channel", type=int, default=4, help="channel for --phase inject")
    p.add_argument("--count", type=int, default=50, help="frames per inject run")
    p.add_argument("--interval", type=float, default=0.02, help="seconds between injects")
    args = p.parse_args()
    setup_logging(args.debug)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
