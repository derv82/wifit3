"""Broadcom wps_monitor vs hostapd differential from qkaiser/broadcom-wps broadcom_fingerprint.py, on wifit3's drivers.

    uv run python scripts/bcom/fragment_probe.py --ssid MyRouter
    uv run python scripts/bcom/fragment_probe.py --bssid 11:22:33:44:55:66
    uv run python scripts/bcom/fragment_probe.py --bssid 11:22:33:44:55:66 --channel 6 --card 7925

Associates as a WPS Enrollee, answers the Identity Request as Enrollee, waits for WSC_START, then sends one
first fragment that declares a total length of 0 and carries no payload. EAP-Failure or a reason-0 deauth
means wps_monitor; WSC_FRAG_ACK means hostapd; anything else is inconclusive. With no WSC_START the fragment
still goes out, on the Identity Request's EAP id, to test whether reassembly is reachable outside a WPS session.

This TRANSMITS. Run it only against an AP you own. BSSIDs are runtime args only. Never hardcode one.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py

from dev import select_device

from wifit3.campaigns.auth_assoc import Association, WlanTransport, build_client_leaving
from wifit3.campaigns.probe.broadcom_wps import RESPONSE_TIMEOUT, WpsImplementation, enter_wps, send_fragment
from wifit3.device.manager import wlan_close, wlan_ifaces
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_ENROLLEE, wps_assoc_ie
from wifit3.models import AccessPoint
from wifit3.persist.pcap import write_pcap
from wifit3.wlan.array import WlanArray

SCAN_SECONDS = 6.0
HOP_INTERVAL = 0.5
MAX_SWEEPS = 3
IMPLEMENTATION_DESCRIPTIONS = {
    WpsImplementation.BROADCOM_WPS_MONITOR: "Broadcom wps_monitor (upstream: LIKELY_VULNERABLE)",
    WpsImplementation.HOSTAPD: "hostapd",
    WpsImplementation.INCONCLUSIVE:
        "inconclusive (upstream classifies only EAP-Failure / deauth reason 0, and WSC_FRAG_ACK)",
}


def print_result(header: str, rows: list[tuple[str, str]]) -> None:
    print()
    print(header)
    for label, value in rows:
        print(f"    {label:<16} {value}")


def matching_aps(array: WlanArray, bssid: Optional[str], ssid: Optional[str]) -> list[AccessPoint]:
    return [ap for ap in array.get_access_points()
            if (bssid is None or ap.bssid.lower() == bssid) and (ssid is None or ap.ssid == ssid)]


async def find_ap(iface, array: WlanArray, bssid: Optional[str], ssid: Optional[str],
                  channel: Optional[int]) -> Optional[AccessPoint]:
    """The strongest AP matching ``bssid``/``ssid``: a dwell on ``channel``, else hop sweeps of every channel."""
    wanted = " ".join(part for part in (bssid, repr(ssid) if ssid is not None else None) if part)
    if channel is not None:
        print(f"[*] Looking for {wanted} on CH{channel}...")
        await iface.set_channel(channel)
        await asyncio.sleep(SCAN_SECONDS)
    else:
        sweep_seconds = len(iface.supported_channels) * HOP_INTERVAL
        await iface.start_hopping(interval=HOP_INTERVAL)
        try:
            for sweep in range(1, MAX_SWEEPS + 1):
                print(f"[*] Looking for {wanted} across {len(iface.supported_channels)} channels "
                      f"(sweep {sweep} of {MAX_SWEEPS}, {sweep_seconds:.0f}s)...")
                await asyncio.sleep(sweep_seconds)
                if matching_aps(array, bssid, ssid):
                    break
        finally:
            await iface.stop_hopping()
    matches = sorted(matching_aps(array, bssid, ssid), key=lambda ap: ap.signal, reverse=True)
    if len(matches) > 1:
        print(f"[!] {len(matches)} APs match; using the strongest. Pass --bssid to pick another:")
        for ap in matches:
            print(f"      {ap.bssid}  CH{ap.channel:<3} {ap.signal} dBm  ssid={ap.ssid!r}")
    return matches[0] if matches else None


async def run(args) -> int:
    ifaces = wlan_ifaces()
    if not ifaces:
        print("[-] No supported devices found.", file=sys.stderr)
        return 1
    iface = select_device(ifaces, args.card)
    if iface is None:
        await wlan_close(ifaces)
        return 1

    print(f"[*] Bringing up {iface.name} ({iface.description})...", file=sys.stderr)
    try:
        if not await iface.connect():
            print("[-] Bring-up returned False.", file=sys.stderr)
            await wlan_close(ifaces)
            return 1
    except Exception as e:  # noqa: BLE001, bring-up can raise on USB error
        print(f"[-] Bring-up failed: {e}", file=sys.stderr)
        await wlan_close(ifaces)
        return 1

    array = WlanArray()
    array.attach(iface)
    capture: list[tuple[bytes, float]] = []
    fake_mac: Optional[str] = None
    rows: list[tuple[str, str]] = []
    bssid = args.bssid.lower() if args.bssid else ""
    ssid = args.ssid or ""
    try:
        ap = await find_ap(iface, array, args.bssid.lower() if args.bssid else None, args.ssid, args.channel)
        if ap is None and not (args.bssid and args.channel):
            print("[-] AP not found. Check the SSID/BSSID, or pass --channel to dwell longer on one channel.",
                  file=sys.stderr)
            return 1
        if ap is None:
            channel = args.channel
            print(f"[!] {bssid} not seen on CH{channel} in {SCAN_SECONDS:.0f}s; trying anyway with ssid={ssid!r}")
        else:
            bssid, channel = ap.bssid.lower(), ap.channel
            ssid = args.ssid if args.ssid is not None else (ap.ssid or "")
            print(f"[*] Target {bssid}  CH{channel}  {ap.signal} dBm  ssid={ssid!r}  enc={ap.encryption}")
        rows.append(("WPS advertised", "not seen" if ap is None else ("yes" if ap.wps else "no")))
        bssid_b = str_to_mac(bssid)
        if not await iface.set_channel(channel):
            print(f"[-] set_channel({channel}) failed.", file=sys.stderr)
            return 1

        fake_mac = await iface.set_fake_mac(None, bssid_b)
        if fake_mac is None:
            print(f"[-] {iface.name} can't active-monitor; the AP's frames would go unACKed.", file=sys.stderr)
            return 1
        our_mac = str_to_mac(fake_mac)
        print(f"[*] Active monitor: card HW-ACKs {fake_mac}")
        await iface.enable_rx_acks()

        def on_rx(pkt) -> None:
            if our_mac in (pkt.raw[4:10], pkt.raw[10:16]):
                capture.append((pkt.raw, time.time()))

        iface.register_rx_callback(on_rx)
        transport = WlanTransport(iface, bssid_b, our_mac,
                                  tx_observer=lambda fr: capture.append((bytes(fr), time.time())))

        sends = 0

        async def send(payload_1x: bytes) -> None:
            nonlocal sends
            sends += 1
            await transport.send_no_wait(M.build_data_frame(bssid_b, our_mac, bssid_b, payload_1x))

        assoc = Association(iface, bssid, ssid, channel, our_mac=our_mac,
                            assoc_trailer_ies=wps_assoc_ie(WPS_REQ_ENROLLEE, False))
        assoc.start()
        transport.start()
        try:
            if not await assoc.associate():
                rows.append(("Associated", f"no ({assoc.fail_reason})"))
                return 1
            print(f"[+] Associated as {fake_mac}")
            rows.append(("Associated", f"yes, as {fake_mac}"))
            acks_before, sends_before = iface.acks_seen(our_mac), sends
            identity_id, wsc_start_id, early = await enter_wps(transport, send)
            rows.append(("WSC_START", f"yes (id {wsc_start_id})" if wsc_start_id is not None
                         else f"no reply in {RESPONSE_TIMEOUT:.0f}s"))
            rows.append(("Entry frames",
                         f"{sends - sends_before} sent, {iface.acks_seen(our_mac) - acks_before} ACKed"))
            if early is not None:
                rows.append(("Fragment", f"not sent, the AP ended WPS entry with {early.describe()}"))
                rows.append(("Implementation", IMPLEMENTATION_DESCRIPTIONS[WpsImplementation.INCONCLUSIVE]))
                return 0
            fragment_id = wsc_start_id if wsc_start_id is not None else identity_id
            if fragment_id is None:
                rows.append(("Fragment", "not sent, no Identity Request to take an EAP id from"))
                rows.append(("Implementation", IMPLEMENTATION_DESCRIPTIONS[WpsImplementation.INCONCLUSIVE]))
                return 0
            acks_before, sends_before = iface.acks_seen(our_mac), sends
            implementation, response = await send_fragment(transport, send, fragment_id)
            rows.append(("Fragment", f"id {fragment_id}, {sends - sends_before} sent, "
                                     f"{iface.acks_seen(our_mac) - acks_before} ACKed"))
            rows.append(("AP response", response))
            rows.append(("Implementation", IMPLEMENTATION_DESCRIPTIONS[implementation]))
        finally:
            await iface.send_no_wait(build_client_leaving(bssid_b, our_mac))
            transport.stop()
            assoc.stop()
        return 0
    finally:
        if fake_mac is not None:
            await iface.disable_rx_acks()
            await iface.clear_fake_mac()
        if capture:
            rows.append(("pcap", f"{args.out} ({write_pcap(Path(args.out), capture)} frames)"))
        if rows:
            print_result(f"Result: {bssid} {ssid!r}", rows)
        await wlan_close(ifaces)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Broadcom wps_monitor vs hostapd first-fragment differential.")
    p.add_argument("--bssid", help="AP to probe.")
    p.add_argument("--ssid", help="AP to probe, by SSID; the strongest match wins.")
    p.add_argument("--channel", type=int, help="AP channel; default: sweep every supported channel to find it.")
    p.add_argument("--card", default="", help="Substring of the adapter to bring up; default: first found.")
    p.add_argument("--out", default="fragment-probe.pcap", help="pcap of every frame to/from our MAC.")
    args = p.parse_args()
    if not args.bssid and args.ssid is None:
        p.error("give --bssid, --ssid, or both")
    return args


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        sys.exit(130)
