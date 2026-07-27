"""
Minimal, fast-fail MT7925AU (MediaTek MT7925U, Wi-Fi 7 / connac3) hardware test.

Validates, driving MT7925AUDriver directly (no device manager):
  1. Device found on USB (either claimed VID:PID)
  2. driver.connect() completes (firmware upload + post-boot init + monitor entry)
  3. set_channel(1) sends without error
  4. Frames received on 2.4 GHz, then a 5 GHz RX sweep
Exits 0 on a clean bring-up, 1 on any hard failure. RX counts are informational (a
quiet RF environment is not a failure); the beacon-rate bar is scripts/beacon_watch.py.

Live TX is the user-gated step (port skill Step 5). The default run transmits NOTHING.
``--tx`` fires a real targeted deauth burst: run it yourself, in the loop, on a client
you own. Broadcast is refused on purpose.

Usage (card plugged in + Zadig/WinUSB-bound to 0846:9072 or 0e8d:7925):
    uv run python scripts/mt7925au/test_hw.py                 # RX only, transmits nothing
    uv run python scripts/mt7925au/test_hw.py --debug         # + verbose USB logs
    uv run python scripts/mt7925au/test_hw.py --tx --bssid AA:BB:CC:DD:EE:FF \\
        --client 00:11:22:33:44:55 --channel 6 --tx-count 32  # LIVE deauth (user action)

Target MACs stay on your terminal only; never commit them.
"""
import argparse
import asyncio
import logging
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.mt7925au.driver import MT7925AUDriver
from wifit3.dot11.packet import Packet

CONNECT_TIMEOUT = 60
CHANNEL_TIMEOUT = 5
RX_WINDOW = 3


def setup_logging(debug: bool):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def step(label: str):
    print(f"\n--- {label} ---")


def ok(msg: str):
    print(f"[PASS] {msg}")


def fail(msg: str):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def _find_device():
    backend = libusb_package.get_libusb1_backend()
    for did in MT7925AUDriver.SUPPORTED_IDS:
        dev = usb.core.find(idVendor=did.vid, idProduct=did.pid, backend=backend)
        if dev is not None:
            return dev, did
    return None, None


def _mac_bytes(s: str) -> bytes:
    b = bytes(int(x, 16) for x in s.replace("-", ":").split(":"))
    if len(b) != 6:
        raise ValueError(f"not a MAC: {s}")
    return b


def _build_deauth(ap: bytes, client: bytes) -> tuple[bytes, bytes]:
    """The two targeted deauth frames (mirrors WlanInterface.deauth): FC=0xC0 (deauth/
    mgmt), reason 7. Both unicast; the HW fills the sequence via _stamp_tx_seq."""
    fc_dur, seq, reason = b"\xc0\x00\x00\x00", b"\x00\x00", struct.pack("<H", 7)
    client_deauth = fc_dur + client + ap + ap + seq + reason   # spoof AP -> client
    ap_deauth = fc_dur + ap + client + ap + seq + reason       # spoof client -> AP
    return client_deauth, ap_deauth


async def _rx_listen(driver, seconds: int) -> list:
    frames: list[Packet] = []
    driver.register_rx_callback(frames.append)
    await asyncio.sleep(seconds)
    return frames


async def main(args):
    setup_logging(args.debug)

    step("USB Discovery")
    dev, did = _find_device()
    if dev is None:
        ids = ", ".join(f"{d.vid:04x}:{d.pid:04x}" for d in MT7925AUDriver.SUPPORTED_IDS)
        fail(f"MT7925AU not found (looked for {ids}). Plug in + WinUSB-bind the card.")
    ok(f"Found {did.vid:04x}:{did.pid:04x} at bus {dev.bus}, address {dev.address}")

    step(f"connect() [{CONNECT_TIMEOUT}s timeout]")
    driver = MT7925AUDriver.from_usb_device(dev, did)
    try:
        success = await asyncio.wait_for(
            driver.connect(progress_cb=lambda p, m: print(f"  [{p*100:5.1f}%] {m}")),
            timeout=CONNECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        fail(f"connect() timed out after {CONNECT_TIMEOUT}s (device hung during init)")
    except Exception as e:  # noqa: BLE001
        fail(f"connect() raised {type(e).__name__}: {e}")
    if not success:
        fail("connect() returned False (check logs above)")
    ok(f"connect() succeeded (MAC {driver.mac_address})")

    tx_channel = args.channel or 1
    step(f"set_channel({tx_channel}) [{CHANNEL_TIMEOUT}s timeout]")
    try:
        await asyncio.wait_for(driver.set_channel(tx_channel), timeout=CHANNEL_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        fail(f"set_channel({tx_channel}) raised {type(e).__name__}: {e}")
    ok(f"set_channel({tx_channel}) completed")

    step(f"RX ({RX_WINDOW}s on CH{tx_channel})")
    frames = await _rx_listen(driver, RX_WINDOW)
    if frames:
        counts: dict[str, int] = {}
        for f in frames:
            counts[f.type] = counts.get(f.type, 0) + 1
        ok(f"Received {len(frames)} frames: {counts}")
    else:
        print("[WARN] No frames (quiet RF or no AP on this channel; not a failure).")

    if not args.tx:
        g5_total = 0
        for ch in (36, 44, 149, 157):
            step(f"5 GHz RX ({RX_WINDOW}s on CH{ch})")
            try:
                await asyncio.wait_for(driver.set_channel(ch), timeout=CHANNEL_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] set_channel({ch}) failed: {type(e).__name__}: {e}")
                continue
            g5 = await _rx_listen(driver, RX_WINDOW)
            g5_total += len(g5)
            bcn = sum(1 for f in g5 if f.type == "beacon")
            print(f"{'[PASS]' if g5 else '[WARN]'} CH{ch}: {len(g5)} frames ({bcn} beacons)")
        print(f"\n5 GHz: {g5_total} frames total: "
              f"{'CONFIRMED' if g5_total else 'UNCONFIRMED (RF may be quiet; not a failure)'}")

    if args.tx:
        await _run_tx(driver, args)

    step("Cleanup")
    await driver.close()
    print("\n=== BRING-UP PASSED ===")


async def _run_tx(driver, args):
    """LIVE targeted-deauth burst (the user-gated TX step). Refuses broadcast."""
    if not args.bssid or not args.client:
        fail("--tx requires --bssid and --client (a unicast STA you own)")
    ap, client = _mac_bytes(args.bssid), _mac_bytes(args.client)
    if client[0] & 0x01:
        fail("--client must be a unicast STA (broadcast/multicast refused)")

    step(f"LIVE TX: {args.tx_count}x bidirectional deauth on CH{args.channel or 1}")
    print(f"       AP {args.bssid}  <->  client {args.client}")
    client_deauth, ap_deauth = _build_deauth(ap, client)
    sent = 0
    for _ in range(args.tx_count):
        if await driver.inject_frame(client_deauth):
            sent += 1
        if await driver.inject_frame(ap_deauth):
            sent += 1
        await asyncio.sleep(0.01)
    if sent == 0:
        fail("inject_frame sent 0 frames (the bulk-OUT path failed)")
    ok(f"injected {sent}/{args.tx_count * 2} deauth frames on EP 0x09")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MT7925AU hardware test")
    parser.add_argument("--debug", action="store_true", help="DEBUG-level logging")
    parser.add_argument("--tx", action="store_true",
                        help="fire a LIVE targeted-deauth burst (user-gated; transmits)")
    parser.add_argument("--bssid", help="target AP BSSID (with --tx)")
    parser.add_argument("--client", help="target client STA, unicast (with --tx)")
    parser.add_argument("--channel", type=int, help="channel to tune before TX/RX")
    parser.add_argument("--tx-count", type=int, default=32,
                        help="deauth pairs to send (default 32)")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print("\n[ABORTED] Keyboard interrupt")
        sys.exit(1)
