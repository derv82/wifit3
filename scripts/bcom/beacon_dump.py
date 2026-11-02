"""Bring up the first card, sit on one channel, and dump every IE of the first beacon from one BSSID.

    uv run python scripts/bcom/beacon_dump.py --bssid 11:22:33:44:55:66
    uv run python scripts/bcom/beacon_dump.py --bssid 11:22:33:44:55:66 --channel 36 --card 8822

Runs until that beacon arrives (Ctrl-C to give up). BSSIDs are runtime args only. Never hardcode one.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py

from dev import select_device

from wifit3.device.manager import wlan_close, wlan_ifaces
from wifit3.dot11.ie import iter_information_elements
from wifit3.dot11.packet import Packet
from wifit3.dot11.wsc.messages import iter_wsc_tlvs
from wifit3.id import VENDOR_BY_OUI

BEACON_IE_OFFSET = 36  # 24-byte header + 12-byte fixed params
WPS_OUI_TYPE = bytes.fromhex("0050f204")


def dump_beacon(pkt: Packet) -> None:
    raw = pkt.raw
    print(f"[*] beacon from {pkt.bssid}  ssid={pkt.ssid!r}  rssi={pkt.rssi}  len={len(raw)}")
    print(f"    timestamp={int.from_bytes(raw[24:32], 'little')}  "
          f"interval={int.from_bytes(raw[32:34], 'little')}  "
          f"capability=0x{int.from_bytes(raw[34:36], 'little'):04x}")
    end = BEACON_IE_OFFSET
    for tag_id, body, ie in iter_information_elements(raw, BEACON_IE_OFFSET):
        end += len(ie)
        print(f"  tag {tag_id:3d}  len {len(body):3d}  {body.hex(' ')}")
        if tag_id == 221 and len(body) >= 4:
            oui = body[:3].hex().upper()
            print(f"           OUI {oui} ({VENDOR_BY_OUI.get(oui, 'unknown')})  type 0x{body[3]:02x}")
            if body[:4] == WPS_OUI_TYPE:
                for attr, value in iter_wsc_tlvs(body[4:]):
                    print(f"           WSC 0x{attr:04x}  {value.hex(' ')}  {value!r}")
    if end < len(raw):
        print(f"  {len(raw) - end} trailing bytes not parsed as IEs: {raw[end:].hex(' ')}")


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

    target = args.bssid.lower()
    found: asyncio.Future[Packet] = asyncio.get_running_loop().create_future()

    def on_rx(pkt: Packet) -> None:
        if pkt.type == "beacon" and (pkt.bssid or "").lower() == target and not found.done():
            found.set_result(pkt)

    iface.register_rx_callback(on_rx)
    if not await iface.set_channel(args.channel):
        print(f"[-] set_channel({args.channel}) failed.", file=sys.stderr)
        await wlan_close(ifaces)
        return 1
    print(f"[*] Listening on CH{args.channel} for {target}...", file=sys.stderr)
    try:
        dump_beacon(await found)
    finally:
        await wlan_close(ifaces)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dump every IE of one AP's beacon.")
    p.add_argument("--bssid", required=True, help="AP to listen for.")
    p.add_argument("--channel", type=int, default=44, help="Channel to tune to (default: 44).")
    p.add_argument("--card", default="", help="Substring of the adapter to bring up; default: first found.")
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        sys.exit(130)
