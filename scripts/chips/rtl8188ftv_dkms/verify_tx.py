"""Byte-diff the driver's TX path against every bulk-OUT URB in a vendor capture.

    uv run python scripts/chips/rtl8188ftv_dkms/verify_tx.py <capture.pcap>

Each recorded URB's 802.11 frame (after the 40-byte descriptor and any PKT_OFFSET pad) goes
through ``Rtl8188ftvDkmsDriver._inject_frame``; the endpoint and bytes it sends must equal the
recorded URB. The driver has one TX path, the monitor injection template, so a frame the vendor
sent another way (managed-mode assoc / DATA) diverges and is reported, not skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8188ftv_dkms import tx
from wifit3.chips.rtl8188ftv_dkms.driver import Rtl8188ftvDkmsDriver

SHOWN_DIVERGENCES = 10


class RecordingDevice:
    """Stands in for ``usb.core.Device``: records bulk-OUT writes; no descriptors, so
    ``tx.bulk_out_size`` uses its ``USB_BULK_SIZE`` (high-speed) fallback."""

    def __init__(self):
        self.writes: list[tuple[int, bytes]] = []

    def __iter__(self):
        return iter(())

    def write(self, endpoint, data, timeout=None):
        self.writes.append((endpoint, bytes(data)))
        return len(data)


def _bulk_out_urbs(pcap: Path, dev: int) -> list[tuple[int, int, bytes]]:
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y",
         f"usb.device_address=={dev} && usb.transfer_type==0x03 "
         f"&& usb.endpoint_address.direction==0 && usb.capdata",
         "-T", "fields", "-e", "frame.number", "-e", "usb.endpoint_address",
         "-e", "usb.capdata"],
        capture_output=True, text=True, check=True).stdout
    urbs = []
    for line in out.splitlines():
        fno, ep, data = (line.split("\t") + ["", ""])[:3]
        if data.strip():
            urbs.append((int(fno), int(ep, 0), bytes.fromhex(data.replace(":", ""))))
    return urbs


def _label(frame: bytes) -> str:
    if len(frame) < 2:
        return "short"
    kind = {0: "mgmt", 1: "ctrl", 2: "data"}.get((frame[0] >> 2) & 0x3, "rsvd")
    return f"{kind}-{(frame[0] >> 4) & 0xF:#x}"


def _diff(fno: int, rec_ep: int, rec: bytes, sent: list[tuple[int, bytes]]) -> str | None:
    if len(sent) != 1:
        return f"frame {fno}: driver sent {len(sent)} URBs, capture has 1"
    ep, data = sent[0]
    if ep != rec_ep:
        return f"frame {fno}: driver EP 0x{ep:02x}, capture EP 0x{rec_ep:02x}"
    if data != rec:
        n = min(len(data), len(rec))
        at = next((j for j in range(n) if data[j] != rec[j]), n)
        return (f"frame {fno}: bytes differ at {at} (driver {len(data)}B, capture {len(rec)}B; "
                f"driver {data[at:at + 4].hex()} vs capture {rec[at:at + 4].hex()})")
    return None


async def _drive(urbs: list[tuple[int, int, bytes]]) -> tuple[Counter, Counter, list[str]]:
    device = RecordingDevice()
    driver = Rtl8188ftvDkmsDriver(device)
    matched: Counter = Counter()
    diverged: Counter = Counter()
    messages: list[str] = []
    for fno, ep, rec in urbs:
        if len(rec) <= tx.TXDESC_SIZE:
            diverged["short"] += 1
            messages.append(f"frame {fno}: {len(rec)}B URB has no frame after the descriptor")
            continue
        start = tx.TXDESC_SIZE + tx.PACKET_OFFSET_SZ * tx.desc_pkt_offset(rec[:tx.TXDESC_SIZE])
        frame = rec[start:]
        device.writes.clear()
        ok = await driver._inject_frame(frame)
        problem = (f"frame {fno}: _inject_frame refused it" if not ok
                   else _diff(fno, ep, rec, device.writes))
        if problem is None:
            matched[_label(frame)] += 1
        else:
            diverged[_label(frame)] += 1
            messages.append(problem)
    return matched, diverged, messages


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Byte-diff rtl8188ftv_dkms _inject_frame against recorded bulk-OUT URBs.")
    parser.add_argument("pcap", type=Path, help="vendor-driver USB capture")
    args = parser.parse_args(argv)
    if not args.pcap.exists():
        print(f"FAIL: no such capture {args.pcap}")
        return 1
    logging.disable(logging.CRITICAL)
    urbs = _bulk_out_urbs(args.pcap, rp.find_card_device(args.pcap))
    if not urbs:
        print(f"FAIL: no bulk-OUT URBs in {args.pcap.name}; TX unverified")
        return 1
    matched, diverged, messages = asyncio.run(_drive(urbs))
    print(f"tx: {len(urbs)} bulk-OUT URBs from {args.pcap.name}")
    print(f"  matched:  {sum(matched.values())} {dict(sorted(matched.items()))}")
    print(f"  diverged: {sum(diverged.values())} {dict(sorted(diverged.items()))}")
    for m in messages[:SHOWN_DIVERGENCES]:
        print(f"    {m}")
    if len(messages) > SHOWN_DIVERGENCES:
        print(f"    ... {len(messages) - SHOWN_DIVERGENCES} more")
    if diverged:
        print("FAIL")
        return 1
    print("PASS: every recorded URB rebuilt byte-for-byte by _inject_frame")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
