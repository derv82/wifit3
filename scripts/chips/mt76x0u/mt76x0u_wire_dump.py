"""mt76x0u_wire_dump.py -- emit a deterministic, diff-friendly text dump
of every Driver<->Firmware USB transaction in a pcap.

Output format is owned by `src/wifit3/chips/mt76x0u/wire_format.py` so that
this script and the live `wire_log.py` produce byte-identical lines for
equivalent USB transactions.

What's INCLUDED (driver/firmware coordination — deterministic, diffable):
  * USB vendor control transfers (register r/w, EEPROM read, device-mode
    switches, FCE config writes, etc.) — every bRequest decoded per
    mt76.h:618-629.
  * Bulk OUT EP 0x08    — MCU commands.
  * Bulk IN  EP 0x85    — MCU responses.

What's EXCLUDED (802.11 air-side traffic — volatile, not what we're
diagnosing):
  * Bulk OUT EP 0x04/0x05/0x06 (TX 802.11)
  * Bulk IN  EP 0x84 (RX 802.11)

NOTE: FW upload chunks ride on a bulk TX endpoint (also excluded by the
above). If you need to diff the FW-upload phase byte-for-byte, slice by
`--frames` and use the existing pcap inspection tools directly. This
script is for the register + MCU coordination layer.

Usage:

    # Full dump (human-readable, with [f=N t=Xs] prefix for each line):
    uv run python scripts/chips/mt76x0u/mt76x0u_wire_dump.py \\
        --pcap driver_captures/captures_mt76x0u/capture-2.pcap \\
        --device 14

    # Diff-friendly (no per-line prefix — feed two outputs to `diff -u`):
    uv run python scripts/chips/mt76x0u/mt76x0u_wire_dump.py \\
        --pcap kernel.pcap --device 14  --no-prefix > kernel.wire.txt
    diff -u kernel.wire.txt ours.wire.txt   # ours.wire.txt comes from wire_log

    # Slice to a phase by frame range (use pcap_slicer.py to find the range):
    uv run python scripts/chips/mt76x0u/mt76x0u_wire_dump.py \\
        --pcap kernel.pcap --device 14 --frames 4913-5742
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Add project src/ to path so we can import the shared wire_format module.
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from wifit3.chips.mt76x0u.wire_format import (
    fmt_fw_chunk,
    fmt_mcu_in,
    fmt_mcu_out,
    fmt_vendor,
)


# ---- Extraction -----------------------------------------------------------

def extract_transactions(pcap: Path, device_addr: int | None,
                         frame_start: int | None, frame_end: int | None):
    """Run tshark, parse fields, return list of dicts in time order.

    Filter pulls control endpoints (vendor + standard) + bulk MCU OUT/IN.
    Standard requests (USB enumeration) are filtered out in Python by
    checking bmRequestType.type. SUBMIT/COMPLETE pairs are merged so each
    transaction appears once.
    """
    if not shutil.which("tshark"):
        sys.exit("tshark not found on PATH")
    if not pcap.exists():
        sys.exit(f"pcap not found: {pcap}")

    # tshark display filter:
    #   - control endpoints 0x00/0x80 (vendor and standard control transfers,
    #     including COMPLETE-half frames that have response data but no
    #     bmRequestType field set by Linux usbmon)
    #   - bulk MCU OUT EP 0x08
    #   - bulk MCU IN  EP 0x85
    clauses = [
        "usb.endpoint_address == 0x00",
        "usb.endpoint_address == 0x80",
        "usb.endpoint_address == 0x08",
        "usb.endpoint_address == 0x85",
    ]
    flt = "(" + " || ".join(clauses) + ")"
    if device_addr is not None:
        flt = f"usb.device_address == {device_addr} && {flt}"
    if frame_start is not None:
        flt = f"{flt} && frame.number >= {frame_start}"
    if frame_end is not None:
        flt = f"{flt} && frame.number <= {frame_end}"

    cmd = [
        "tshark", "-r", str(pcap),
        "-Y", flt,
        "-T", "fields",
        "-e", "frame.number",
        "-e", "frame.time_relative",
        "-e", "usb.endpoint_address",
        "-e", "usb.bmRequestType",
        "-e", "usb.setup.bRequest",
        "-e", "usb.setup.wValue",
        "-e", "usb.setup.wIndex",
        "-e", "usb.setup.wLength",
        "-e", "usb.capdata",
        "-e", "usb.data_fragment",
        "-e", "usb.control.Response",
        "-E", "separator=|",
        "-E", "quote=n",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    raw_records = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 11:
            parts = parts + [""] * (11 - len(parts))
        try:
            fn = int(parts[0])
            t = float(parts[1])
            ep_str = parts[2]
            brt_str = parts[3]
            data = (parts[8] or parts[9] or parts[10] or "").replace(":", "")
            ep = (int(ep_str, 16) if ep_str.startswith("0x")
                  else int(ep_str)) if ep_str else None

            is_control_ep = ep in (0x00, 0x80)

            if brt_str or is_control_ep:
                def _i(s, default=0):
                    if not s:
                        return default
                    return int(s, 16) if s.startswith("0x") else int(s)
                rec = {
                    "frame": fn, "t": t, "kind": "vendor", "ep": ep,
                    "bmRequestType": _i(brt_str) if brt_str else None,
                    "bRequest": _i(parts[4]) if parts[4] else None,
                    "wValue": _i(parts[5]),
                    "wIndex": _i(parts[6]),
                    "wLength": _i(parts[7]),
                    "data": data,
                }
            else:
                if not data:
                    continue
                rec = {"frame": fn, "t": t, "kind": "bulk", "ep": ep, "data": data}
            raw_records.append(rec)
        except (ValueError, IndexError):
            continue

    raw_records.sort(key=lambda x: (x["t"], x["frame"]))

    # Merge SUBMIT/COMPLETE pairs for vendor transfers, filter standard reqs.
    merged = []
    consumed = set()
    for i, rec in enumerate(raw_records):
        if i in consumed:
            continue
        if rec["kind"] != "vendor":
            merged.append(rec)
            continue
        if rec.get("bRequest") is None:
            continue  # orphan COMPLETE (SUBMIT outside window)
        # Only vendor-class (bmRequestType.type == 2 = bits 5-6 == 0b10)
        brt = rec.get("bmRequestType", 0) or 0
        if (brt & 0x60) != 0x40:
            continue
        # Find the COMPLETE partner on same EP within 10ms
        partner_idx = None
        for j in range(i + 1, len(raw_records)):
            other = raw_records[j]
            if other["t"] - rec["t"] > 0.010:
                break
            if other.get("kind") != "vendor":
                continue
            if other.get("ep") != rec.get("ep"):
                continue
            if other.get("bRequest") is not None:
                continue
            partner_idx = j
            break
        if partner_idx is not None:
            other = raw_records[partner_idx]
            consumed.add(partner_idx)
            if other["data"] and not rec["data"]:
                rec = {**rec, "data": other["data"]}
        merged.append(rec)
    return merged


def format_line(tx: dict, *, no_prefix: bool, t0: float) -> str:
    """One-line text representation of a transaction."""
    if tx["kind"] == "vendor":
        is_in = bool((tx["bmRequestType"] or 0) & 0x80)
        data_bytes = bytes.fromhex(tx["data"]) if tx["data"] else b""
        decoded = fmt_vendor(
            bRequest=tx["bRequest"],
            is_in=is_in,
            wValue=tx["wValue"],
            wIndex=tx["wIndex"],
            wLength=tx["wLength"],
            data=data_bytes,
        )
    elif tx["kind"] == "bulk":
        raw = bytes.fromhex(tx["data"])
        if tx["ep"] == 0x08:
            decoded = fmt_mcu_out(raw)
        elif tx["ep"] == 0x85:
            decoded = fmt_mcu_in(raw)
        else:
            decoded = f"BULK_? EP=0x{tx['ep']:02x}  {len(raw)}B"
    else:
        decoded = f"UNKNOWN {tx}"

    if no_prefix:
        return decoded
    rel = tx["t"] - t0
    return f"[f={tx['frame']:>5d} t={rel:7.3f}s] {decoded}"


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--pcap", required=True, help="Path to pcap file")
    p.add_argument("--device", type=int, default=None,
                   help="USB device address to filter (e.g. 14 kernel, 31 ours)")
    p.add_argument("--frames", default=None, help="Frame range like 4913-5742")
    p.add_argument("--no-prefix", action="store_true",
                   help="Omit per-line [f=N t=Xs] prefix for clean diff output")
    args = p.parse_args()

    fs = fe = None
    if args.frames:
        fs, fe = (int(x) for x in args.frames.split("-"))

    txs = extract_transactions(Path(args.pcap), args.device, fs, fe)
    if not txs:
        print(f"# No matching transactions in {args.pcap}")
        return

    t0 = txs[0]["t"]
    for tx in txs:
        print(format_line(tx, no_prefix=args.no_prefix, t0=t0))


if __name__ == "__main__":
    main()
