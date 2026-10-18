"""Single-card hardware active-monitor probe for RTL8192EU (TP-Link TL-WN823N v2/v3).

Arms the port's hardware active monitor for a forged MAC, injects unicast frames addressed
to that MAC, and counts the link-layer ACKs that come back to the injected source MAC via
the driver's own ACK tally. Controls: active monitor off (same target), and a bogus MAC.

Single-card caveat: this machine has one RTL8192EU, so the SAME radio transmits the probe
and is expected to ACK it. The ACK must be heard after our TX completes; a half-duplex
radio often cannot do that (and RX may be off during TX). So a ~0 count here is
INCONCLUSIVE, not proof the port lacks hardware active monitor. The definitive test needs a
second (prober) card:

    uv run python scripts/ack/rx_autoack.py --test-card 8192 --channel <ch>

Usage:
    uv run python scripts/chips/rtl8192eu/test_hw_active_monitor.py
    uv run python scripts/chips/rtl8192eu/test_hw_active_monitor.py --count 200 --channel 6
    uv run python scripts/chips/rtl8192eu/test_hw_active_monitor.py --interval 0.05 --debug
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
from wifit3.chips.rtl8192eu.constants import REG_MACID
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver
from wifit3.dot11 import build_deauth

USB_VID_TPLINK = 0x2357
USB_PID_TL_WN823N_V2_V3 = 0x0109

SPOOF = "02:ac:ac:00:00:01"       # forged MAC armed in hardware active monitor
SPOOF2 = "02:ac:ac:00:00:02"      # second forged MAC for the double-arm case
SRC = "02:b0:b0:00:00:01"         # sources the injected frames; its ACKs are counted
BOGUS = "02:00:00:00:00:99"       # unicast, nobody owns it: control, expect ~0 ACKs
SETTLE_S = 0.5                    # let the RX tap drain after the last injection


def mac_bytes(mac: str) -> bytes:
    return bytes(int(octet, 16) for octet in mac.split(":"))


def read_macid(driver: RTL8192EUDriver) -> bytes:
    """The 6 bytes currently in REG_MACID..+5, read straight off the wire (6 × read8)."""
    t = driver.transport
    return bytes(t.read8(REG_MACID + i) for i in range(6))



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


async def probe(driver: RTL8192EUDriver, dst: bytes, count: int, interval: float) -> int:
    """Inject ``count`` unicast frames addressed to ``dst``; return ACKs seen to SRC."""
    src = mac_bytes(SRC)
    frame = build_deauth(a1=dst, a2=src, a3=dst, reason=7)
    before = driver.acks_seen(src)
    sent = 0
    for _ in range(count):
        try:
            if await driver.inject_frame(frame):
                sent += 1
        except Exception as e:                                # noqa: BLE001
            print(f"    inject failed: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)
    await asyncio.sleep(SETTLE_S)
    acked = driver.acks_seen(src) - before
    print(f"    injected {sent}/{count} -> {acked} ACK(s) back to {SRC}")
    return acked


async def arm(driver: RTL8192EUDriver, mac: bytes) -> None:
    try:
        armed = await driver.enter_active_monitor(mac)
    except NotImplementedError as e:
        fail(
            f"hardware active monitor is not available on this port ({e}). "
            "Nothing to probe on one card; use scripts/ack/rx_autoack.py with a prober card."
        )
    except Exception as e:                                    # noqa: BLE001
        fail(f"enter_active_monitor({mac.hex(':')}) raised: {type(e).__name__}: {e}")
    ok(f"armed hardware ACKs for {bytes(armed).hex(':')}")


async def run(args) -> int:
    entry = next(
        (d for d in SUPPORTED_IDS
         if d.vid == USB_VID_TPLINK and d.pid == USB_PID_TL_WN823N_V2_V3),
        None,
    )
    if entry is None:
        fail("TL-WN823N v2/v3 is missing from wifit3.chips.rtl8192eu.SUPPORTED_IDS")

    step("USB discovery")
    dev = open_device()
    print(f"  chipset = {entry.chipset}, model = {entry.product_name}")

    driver = RTL8192EUDriver.from_usb_device(dev, entry)
    try:
        step("driver.connect() — cold bring-up")

        def progress(pct: float, msg: str) -> None:
            print(f"    [{int(pct * 100):3d}%] {msg}")

        t0 = time.perf_counter()
        try:
            connected = await driver.connect(progress_cb=progress)
        except Exception as e:                                # noqa: BLE001
            fail(f"connect() raised: {type(e).__name__}: {e}")
        dt_ms = (time.perf_counter() - t0) * 1000
        if not connected:
            fail("connect() returned False")
        ok(f"connect() returned True in {dt_ms:.0f} ms")
        print(f"  mac_address     = {driver.mac_address}")
        print(f"  current_channel = {driver.current_channel}")

        if not await driver.set_channel(args.channel):
            fail(f"set_channel({args.channel}) failed")
        print(f"  tuned to channel {args.channel}")

        step("enable_rx_acks() — arm the ACK tap")
        try:
            await driver.enable_rx_acks()
        except Exception as e:                                # noqa: BLE001
            fail(f"enable_rx_acks() raised: {type(e).__name__}: {e}")
        ok("ACK control frames are admitted to the RX tap")

        spoof = mac_bytes(SPOOF)
        step("Capture REG_MACID before the first arm")
        real_mac = read_macid(driver)
        print(f"  REG_MACID = {real_mac.hex(':')}")

        step(f"enter_active_monitor({SPOOF})")
        await arm(driver, spoof)

        step(f"Probe spoofed MAC {SPOOF} — active monitor ON")
        on = await probe(driver, spoof, args.count, args.interval)

        step(f"Control: active monitor OFF, same target {SPOOF}")
        try:
            await driver.exit_active_monitor()
        except Exception as e:                                # noqa: BLE001
            fail(f"exit_active_monitor() raised: {type(e).__name__}: {e}")
        restored = read_macid(driver)
        if restored != real_mac:
            fail(
                "exit_active_monitor did not restore REG_MACID: "
                f"{restored.hex(':')} != {real_mac.hex(':')}"
            )
        ok(f"REG_MACID restored to {restored.hex(':')}")
        off = await probe(driver, spoof, args.count, args.interval)

        step(f"Control: bogus MAC {BOGUS} — active monitor ON for {SPOOF}")
        await arm(driver, spoof)
        bogus = await probe(driver, mac_bytes(BOGUS), args.count, args.interval)

        step(f"Double-arm: enter {SPOOF} then {SPOOF2} without an exit")
        try:
            await driver.exit_active_monitor()
        except Exception as e:                                # noqa: BLE001
            fail(f"exit_active_monitor() raised: {type(e).__name__}: {e}")
        await arm(driver, spoof)
        await arm(driver, mac_bytes(SPOOF2))
        if read_macid(driver) != mac_bytes(SPOOF2):
            fail(f"double-arm left REG_MACID at {read_macid(driver).hex(':')}")
        ok("second arm re-pointed REG_MACID at the second MAC")

        step("Exit after the double-arm restores the ORIGINAL REG_MACID")
        try:
            await driver.exit_active_monitor()
        except Exception as e:                                # noqa: BLE001
            fail(f"exit_active_monitor() raised: {type(e).__name__}: {e}")
        restored = read_macid(driver)
        if restored != real_mac:
            fail(
                "exit after the double-arm did not restore the original REG_MACID: "
                f"{restored.hex(':')} != {real_mac.hex(':')} (captured a forged value?)"
            )
        ok(f"REG_MACID restored to {restored.hex(':')} after the double-arm")

        margin = max(args.count // 4, 5)
        print()
        print(f"  {SPOOF}  active monitor ON : {on:4d}/{args.count}")
        print(f"  {SPOOF}  active monitor OFF: {off:4d}/{args.count}")
        print(f"  {BOGUS}  bogus (ON)        : {bogus:4d}/{args.count}")
        print(f"  margin = {margin}")

        if on > args.count * 2:
            print(
                f"  NOTE: counts exceed {args.count}: the chip's HW ACK-retry resends each "
                "inject, and every on-air copy is ACKed."
            )

        if on > max(off, bogus) + margin:
            ok(
                f"HW ACKs the spoofed MAC: {on} ACKs vs off={off}, bogus={bogus} "
                f"(channel {args.channel})"
            )
            return 0

        if max(on, off, bogus) == 0:
            print(
                "\n  All counts are 0, which is INCONCLUSIVE on a single radio: the ACK must "
                "be heard after our own TX completes. Re-run with a second prober card:\n"
                f"    uv run python scripts/ack/rx_autoack.py --test-card 8192 "
                f"--channel {args.channel}"
            )
            fail("no ACKs observed (single-card probe cannot prove the negative)")
        fail(
            f"spoofed MAC did not clearly beat the controls: {on} <= "
            f"max({off}, {bogus}) + {margin}"
        )
    finally:
        step("restore")
        try:
            await driver.exit_active_monitor()
            print("  active monitor off")
        except Exception as e:                                # noqa: BLE001
            print(f"  exit_active_monitor skipped: {type(e).__name__}: {e}")
        step("driver.close()")
        await driver.close()
        print("  closed")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--count", type=int, default=100, help="unicast frames injected per probe")
    p.add_argument("--channel", type=int, default=1, help="channel to tune to (default: 1)")
    p.add_argument(
        "--interval", type=float, default=0.02, help="seconds between injections (default: 0.02)"
    )
    p.add_argument("--debug", action="store_true", help="verbose USB + FW logging")
    args = p.parse_args()
    setup_logging(args.debug)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
