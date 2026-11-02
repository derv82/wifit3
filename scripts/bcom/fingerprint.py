"""Passive Broadcom fingerprint of every AP heard: the IE checks from qkaiser/broadcom-wps broadcom_fingerprint.py.

    uv run python scripts/bcom/fingerprint.py
    uv run python scripts/bcom/fingerprint.py --card 8822

Hops all supported channels until Ctrl-C, printing one line per BSSID and frame type (beacon, probe
response) that carries both the Broadcom IE and a WPS IE. Sends nothing.
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
from wifit3.dot11.wsc.messages import (
    ATTR_DEV_NAME, ATTR_MANUFACTURER, ATTR_MODEL_NAME, ATTR_MODEL_NUMBER, ATTR_SERIAL_NUMBER,
    ATTR_UUID_E, ATTR_UUID_R, iter_wsc_tlvs,
)

BEACON_IE_OFFSET = 36  # 24-byte header + 12-byte fixed params
BROADCOM_IE_PREFIX = bytes.fromhex("00101802")
WPS_IE_PREFIX = bytes.fromhex("0050f204")
FIXED_WPS_UUID = bytes.fromhex("000102030405060708090a0b0c0d0ebb")
WPS_TEXT_ATTRIBUTES = {
    ATTR_DEV_NAME: "device_name",
    ATTR_MANUFACTURER: "vendor",
    ATTR_MODEL_NAME: "model",
    ATTR_MODEL_NUMBER: "model_number",
    ATTR_SERIAL_NUMBER: "serial_number",
}


def fingerprint(pkt: Packet) -> None:
    broadcom_ie = None
    wps_ie = None
    for tag_id, body, _ in iter_information_elements(pkt.raw, BEACON_IE_OFFSET):
        if tag_id != 221:
            continue
        if len(body) == 9 and body.startswith(BROADCOM_IE_PREFIX):
            broadcom_ie = body
        if body.startswith(WPS_IE_PREFIX):
            wps_ie = body[len(WPS_IE_PREFIX):]
    if broadcom_ie is None or wps_ie is None:
        return

    identity: dict[str, str] = {}
    uuids: dict[int, bytes] = {}
    for attr, value in iter_wsc_tlvs(wps_ie):
        if attr in WPS_TEXT_ATTRIBUTES:
            text = value.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
            if text:
                identity[WPS_TEXT_ATTRIBUTES[attr]] = text
        if attr in (ATTR_UUID_E, ATTR_UUID_R):
            uuids[attr] = value
    fixed_uuid = FIXED_WPS_UUID in uuids.values()
    defaults = [
        identity.get("vendor", "").casefold() == "broadcom",
        identity.get("model", "").casefold() in {"broadcom", "broadcomap"},
        identity.get("model_number") in {"1234", "123456"},
        identity.get("device_name", "").casefold() == "broadcomap",
    ]

    names = " ".join(f"{name}={identity[name]!r}" if name in identity else f"{name}=-" for name in WPS_TEXT_ATTRIBUTES.values())
    print(f"{pkt.bssid}  {pkt.type:<10} {pkt.rssi:>4}  ssid={pkt.ssid!r}  bcm_ie={broadcom_ie.hex()}  "
          f"fixed_uuid={'yes' if fixed_uuid else 'no'}  defaults={sum(defaults)}/4  {names}")


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

    seen: set[tuple[str, str]] = set()

    def on_rx(pkt: Packet) -> None:
        key = ((pkt.bssid or "").lower(), pkt.type)
        if pkt.type in ("beacon", "probe_resp") and key[0] and key not in seen:
            seen.add(key)
            fingerprint(pkt)

    iface.register_rx_callback(on_rx)
    await iface.start_hopping(interval=0.5)
    print(f"[*] Hopping {iface.supported_channels}; Ctrl-C to stop.", file=sys.stderr)
    try:
        await asyncio.Event().wait()
    finally:
        await wlan_close(ifaces)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Passive Broadcom fingerprint of every AP heard.")
    p.add_argument("--card", default="", help="Substring of the adapter to bring up; default: first found.")
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        sys.exit(130)
