"""Extract rtw8812a firmware body from cold-boot pcap.

The 8812a kernel uses the legacy MCUFWDL upload path (rtw88-source-v6.18/
usb.c:168 rtw_usb_write_firmware_page). Each 4096-byte page is sliced into
196 / 8 / 1 byte chunks and pushed via vendor control transfers:

    bRequest    = RTW_USB_CMD_REQ  (0x05)
    bmReqType   = 0x40 (vendor OUT)
    wValue      = FW_START_ADDR_LEGACY (0x1000) + offset-within-page
    wIndex      = 0
    payload     = 196 / 8 / 1 bytes of FW body

The kernel strips the 32-byte rtw_fw_hdr_legacy from the firmware file
before sending — so the wire bytes are body-only.

Page boundaries are signalled by a write to REG_MCUFW_CTRL (0x0080) that
sets BIT_ROM_PGE (bits 18:16) with the new page number. We detect them by
watching wValue restart at 0x1000.

Output: writes the concatenated body to
src/wifit3/chips/rtl8812au/assets/rtw8812a_fw.bin (creating dirs as needed).
Also prints SHA-256 so the user can byte-verify against linux-firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


def extract(pcap_path: Path, start_frame: int, end_frame: int) -> bytes:
    """Run tshark, filter for FW-upload control transfers, concat the body."""
    filter_str = (
        f"frame.number >= {start_frame} and frame.number <= {end_frame} "
        "and usb.bmRequestType == 0x40 "
        "and usb.setup.bRequest == 0x05 "
        "and usb.setup.wValue >= 0x1000 "
        "and usb.setup.wValue < 0x2000"
    )
    cmd = [
        "tshark", "-r", str(pcap_path),
        "-Y", filter_str,
        "-T", "fields",
        "-e", "frame.number",
        "-e", "usb.setup.wValue",
        "-e", "usb.data_fragment",
    ]
    print(f"Running tshark with filter: {filter_str}")
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)

    body = bytearray()
    last_wvalue = -1
    page_starts = []
    rows = 0
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2]:
            continue
        rows += 1
        wvalue = int(parts[1], 16) if parts[1].startswith("0x") else int(parts[1])
        data_hex = parts[2].replace(":", "")
        chunk = bytes.fromhex(data_hex)

        # Detect page restart: wValue drops back to 0x1000.
        if wvalue == 0x1000 and last_wvalue >= 0x1000:
            page_starts.append((int(parts[0]), len(body)))
        elif wvalue == 0x1000 and last_wvalue == -1:
            page_starts.append((int(parts[0]), 0))

        body.extend(chunk)
        last_wvalue = wvalue

    print(f"  tshark rows matched: {rows}")
    print(f"  Detected {len(page_starts)} page boundaries")
    for i, (frame, offset) in enumerate(page_starts):
        next_off = page_starts[i + 1][1] if i + 1 < len(page_starts) else len(body)
        page_size = next_off - offset
        print(f"    page {i:2d}: frame {frame:5d}, body offset 0x{offset:06x}, size {page_size}")

    return bytes(body)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--pcap",
        default="driver_captures/captures_rtw88_8812au/capture-1.pcap",
        type=Path,
    )
    p.add_argument("--start-frame", type=int, default=1)
    p.add_argument(
        "--end-frame", type=int, default=3144,
        help="upper bound (defaults to end of cold-boot phase per pcap_slicer)",
    )
    p.add_argument(
        "--output",
        default="src/wifit3/chips/rtl8812au/assets/rtw8812a_fw.bin",
        type=Path,
    )
    p.add_argument("--no-write", action="store_true", help="just print stats")
    args = p.parse_args()

    if not args.pcap.exists():
        print(f"pcap not found: {args.pcap}", file=sys.stderr)
        return 1

    body = extract(args.pcap, args.start_frame, args.end_frame)

    print(f"\nExtracted body: {len(body)} bytes")
    print(f"SHA-256:        {hashlib.sha256(body).hexdigest()}")
    print(f"First 32B hex:  {body[:32].hex()}")
    print(f"Last  32B hex:  {body[-32:].hex()}")

    if args.no_write:
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(body)
    print(f"\nWrote {len(body)} bytes -> {args.output}")
    print(
        f"\nByte-verify hint: locate linux-firmware/rtw88/rtw8812a_fw.bin and "
        f"compare bytes[32:] (skipping its 32-byte rtw_fw_hdr_legacy) to "
        f"this body. The SHA-256 of bytes[32:] of the official file should "
        f"match {hashlib.sha256(body).hexdigest()}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
