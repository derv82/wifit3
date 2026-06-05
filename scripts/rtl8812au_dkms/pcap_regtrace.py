"""Decode an RTL8812AU vendor-driver USB pcap into an ORDERED register-write trace.

The output is the diff oracle for our Python port: a clean, in-order list of every
vendor control-transfer register write the morrownr driver issued during cold-boot
bring-up, with RF-path BB writes resolved into their underlying RF[addr]=val.

Capture-format gotchas (verified against capture-2.pcap; do NOT re-derive):
  * A register WRITE is ONE vendor control transfer:
        usb.bmRequestType==0x40 && usb.setup.bRequest==5
    GOTCHA: the field is `usb.bmRequestType`, NOT `usb.setup.bmRequestType`
    (the latter is invalid and silently matches nothing).
  * The register address is in usb.setup.wValue.
  * The value is in usb.data_fragment, as little-endian bytes (1/2/4 wide).
  * An RF-register write is a BB write to wValue 0x0C90 (RF path A) / 0x0E90
    (RF path B). The 32-bit LE value decodes as:
        rf_addr = (dword >> 20) & 0xFF
        rf_val  = dword & 0xFFFFF
    e.g. data_fragment=00800103 -> LE dword 0x03018000 -> RF[A] 0x30 = 0x18000.
  * RF reads latch the addr into wValue 0x08B0 then read back; 0x08B0 writes are
    therefore "rf-read-latch" markers, not real config writes.

Output: one line per write, in capture order:
    frame=NNNNN  BB 0xADDR=0xVALUE
RF-path writes append the decoded RF target:
    frame=NNNNN  BB 0x0C90=0x03018000  -> RF[A] 0x30=0x18000
RF-read-latch writes are tagged:
    frame=NNNNN  BB 0x08B0=0x000000XX  (rf-read-latch)

Dependency-free: subprocess + stdlib only. tshark must be on PATH.

Run (default scopes to the cold-boot bring-up window):
    uv run python scripts/rtl8812au_dkms/pcap_regtrace.py \
        usb_dumps_new/captures_8812au/capture-2.pcap \
        --max-frame 12500 \
        --out scripts/rtl8812au_dkms/ref/morrownr_capture2_bringup.txt
"""
from __future__ import annotations

import argparse
import subprocess
import sys

RF_PATH = {0x0C90: "A", 0x0E90: "B"}   # BB wValue -> RF path for RF writes
RF_READ_LATCH = 0x08B0                  # BB wValue used to latch an RF read addr

TSHARK_FILTER = "usb.bmRequestType==0x40 && usb.setup.bRequest==5"
TSHARK_FIELDS = ("frame.number", "usb.setup.wValue", "usb.data_fragment")


def _le_dword(data_hex: str) -> int:
    """Interpret a usb.data_fragment hex string as a little-endian integer.

    data_fragment is the raw payload bytes in transmit order; the value is LE, so
    reverse the byte order before assembling the int.
    """
    b = bytes.fromhex(data_hex)
    return int.from_bytes(b, "little")


def _run_tshark(pcap: str, max_frame: int) -> list[tuple[int, int, str]]:
    """Return (frame, wValue, data_hex) tuples for every register write up to max_frame."""
    disp = TSHARK_FILTER
    if max_frame:
        disp = f"({TSHARK_FILTER}) && frame.number<={max_frame}"
    cmd = ["tshark", "-r", pcap, "-Y", disp, "-T", "fields"]
    for f in TSHARK_FIELDS:
        cmd += ["-e", f]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"tshark failed (exit {proc.returncode})")
    rows: list[tuple[int, int, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[2]:
            # No data_fragment: not a value-carrying write (skip).
            continue
        frame = int(parts[0])
        wvalue = int(parts[1], 16)
        data_hex = parts[2].replace(":", "").replace("0x", "")
        rows.append((frame, wvalue, data_hex))
    return rows


def _format(frame: int, wvalue: int, data_hex: str) -> str:
    dword = _le_dword(data_hex)
    width = len(data_hex) // 2
    line = f"frame={frame:<6d} BB 0x{wvalue:04X}=0x{dword:0{width * 2}X}"
    if wvalue in RF_PATH:
        rf_addr = (dword >> 20) & 0xFF
        rf_val = dword & 0xFFFFF
        line += f"  -> RF[{RF_PATH[wvalue]}] 0x{rf_addr:02X}=0x{rf_val:05X}"
    elif wvalue == RF_READ_LATCH:
        line += "  (rf-read-latch)"
    return line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pcap", help="path to the usbmon pcap")
    ap.add_argument("--max-frame", type=int, default=12500,
                    help="scope to frames <= this (default 12500: cold-boot bring-up "
                         "before the scan-hop flood). 0 = whole capture.")
    ap.add_argument("--out", help="write trace to this file (default: stdout)")
    args = ap.parse_args()

    rows = _run_tshark(args.pcap, args.max_frame)
    lines = [_format(*r) for r in rows]
    body = "\n".join(lines) + ("\n" if lines else "")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print(f"{len(lines)} register writes -> {args.out}")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
