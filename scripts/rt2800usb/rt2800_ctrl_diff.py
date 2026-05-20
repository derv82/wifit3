"""Extract vendor control transfers from rt2800usb pcaps and diff our
bring-up against the kernel's known-good sequence.

Usage:

    # Just dump the kernel's bring-up sequence (frames during airmon-ng):
    python scripts/rt2800usb/rt2800_ctrl_diff.py kernel

    # Dump our wifit3 sequence from a capture you took:
    python scripts/rt2800usb/rt2800_ctrl_diff.py ours <our.pcap>

    # Diff the two:
    python scripts/rt2800usb/rt2800_ctrl_diff.py diff <our.pcap>

Per-record format:
    [frame.number]  bReq  bmReq  wValue  wIndex  wLen  data...   -> decoded

Per-spec for rt2x00usb (see chips/rt2800usb/transport.py module docstring):
  * bRequest 6 = USB_MULTI_WRITE     wIndex = register address, data = u32 LE
  * bRequest 7 = USB_MULTI_READ      wIndex = register address
  * bRequest 1 = USB_DEVICE_MODE     wValue = mode (USB_MODE_FIRMWARE etc.)
  * bRequest 9 = USB_EEPROM_READ     wValue=wIndex=0, data = EEPROM dump
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

KERNEL_PCAP = Path(__file__).parent.parent.parent / "usb_dumps" / "captures_rt2800usb_rt5372" / "capture-1.pcap"

# Decode hints — populated from rt2800.h address constants.
REGISTER_NAMES = {
    0x0080: "WLAN_FUN_CTRL",
    0x0114: "OPT_14_CSR",
    0x0208: "WPDMA_GLO_CFG",
    0x0298: "RX_CRX_IDX",
    0x029C: "RX_DRX_IDX",
    0x02A0: "USB_DMA_CFG",
    0x02A4: "US_CYC_CNT",
    0x0400: "PBF_SYS_CTRL",
    0x0404: "HOST_CMD_CSR",
    0x0408: "PBF_CFG",
    0x040C: "PBF_MAX_PCNT",
    0x0500: "RF_CSR_CFG",
    0x0580: "EFUSE_CTRL",
    0x0584: "EFUSE_DATA0",
    0x0588: "EFUSE_DATA1",
    0x058C: "EFUSE_DATA2",
    0x0590: "EFUSE_DATA3",
    0x1000: "MAC_CSR0",
    0x1004: "MAC_SYS_CTRL",
    0x1008: "MAC_ADDR_DW0",
    0x100C: "MAC_ADDR_DW1",
    0x1010: "AUTOWAKEUP_CFG",
    0x1018: "MAX_LEN_CFG",
    0x101C: "BBP_CSR_CFG",
    0x102C: "LED_CFG",
    0x1040: "AMPDU_BA_WINSIZE",
    0x110C: "CH_TIME_CFG",
    0x1100: "XIFS_TIME_CFG",
    0x1104: "BKOFF_SLOT_CFG",
    0x1114: "BCN_TIME_CFG",
    0x1128: "INT_TIMER_CFG",
    0x1200: "MAC_STATUS_CFG",
    0x1204: "PWR_PIN_CFG",
    0x1328: "TX_PIN_CFG",
    0x132C: "TX_BAND_CFG",
    0x1330: "TX_SW_CFG0",
    0x1334: "TX_SW_CFG1",
    0x1338: "TX_SW_CFG2",
    0x1340: "TXOP_CTRL_CFG",
    0x1344: "TX_RTS_CFG",
    0x1348: "TX_TIMEOUT_CFG",
    0x134C: "TX_RTY_CFG",
    0x1350: "TX_LINK_CFG",
    0x1364: "CCK_PROT_CFG",
    0x1368: "OFDM_PROT_CFG",
    0x136C: "MM20_PROT_CFG",
    0x1370: "MM40_PROT_CFG",
    0x1374: "GF20_PROT_CFG",
    0x1378: "GF40_PROT_CFG",
    0x1380: "EXP_ACK_TIME",
    0x1400: "RX_FILTER_CFG",
    0x1404: "AUTO_RSP_CFG",
    0x1408: "LEGACY_BASIC_RATE",
    0x140C: "HT_BASIC_RATE",
    0x1500: "HT_FBK_CFG0",
    0x1504: "HT_FBK_CFG1",
    0x1508: "LG_FBK_CFG0",
    0x150C: "LG_FBK_CFG1",
    0x1608: "TXOP_HLDR_ET",
    0x1700: "RX_STA_CNT0",
    0x1704: "RX_STA_CNT1",
    0x1708: "RX_STA_CNT2",
    0x170C: "TX_STA_CNT0",
    0x1710: "TX_STA_CNT1",
    0x1714: "TX_STA_CNT2",
    0x7010: "H2M_MAILBOX_CSR",
    0x7014: "H2M_MAILBOX_CID",
    0x701C: "H2M_MAILBOX_STATUS",
    0x7024: "H2M_INT_SRC",
    0x7028: "H2M_BBP_AGENT",
}

VREQ_NAMES = {
    1: "USB_DEVICE_MODE",
    2: "USB_SINGLE_WRITE",
    3: "USB_SINGLE_READ",
    6: "USB_MULTI_WRITE",
    7: "USB_MULTI_READ",
    8: "USB_EEPROM_WRITE",
    9: "USB_EEPROM_READ",
    10: "USB_LED_CONTROL",
    12: "USB_RX_CONTROL",
}


def _reg_label(addr: int) -> str:
    name = REGISTER_NAMES.get(addr)
    if name:
        return name
    if 0x3000 <= addr < 0x4000:
        return f"FW_IMAGE[+0x{addr - 0x3000:04x}]"
    if 0x4000 <= addr < 0x8000:
        return f"reg_0x{addr:04x}"
    return f"0x{addr:04x}"


def extract_vendor_ctrl(pcap_path: Path, *, vid: int = 0x148F):
    """Return a list of dicts, one per vendor control transfer.

    Filters to the requested VID and to USB_TYPE_VENDOR (bmRequestType
    & 0x60 == 0x40).
    """
    if not shutil.which("tshark"):
        sys.exit("tshark not found on PATH")
    if not pcap_path.exists():
        sys.exit(f"pcap not found: {pcap_path}")

    # Filter matches vendor requests in both directions. The narrower
    # `usb.bmRequestType.type == 2` doesn't work on Linux usbmon
    # captures (the bit-field decode is missing on SUBMIT URBs), so
    # we match the raw byte values for vendor IN/OUT.
    cmd = [
        "tshark",
        "-r", str(pcap_path),
        "-Y", "usb.bmRequestType == 0x40 || usb.bmRequestType == 0xc0",
        "-T", "fields",
        "-e", "frame.number",
        "-e", "frame.time_epoch",
        "-e", "usb.bmRequestType",
        "-e", "usb.setup.bRequest",
        "-e", "usb.setup.wValue",
        "-e", "usb.setup.wIndex",
        "-e", "usb.setup.wLength",
        "-e", "usb.capdata",
        "-e", "usb.data_fragment",
        "-E", "separator=|",
        "-E", "quote=n",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    records = []
    for line in out.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 7:
            continue
        try:
            fn, ts, brt, breq, wval, widx, wlen = (parts[i] for i in range(7))
            # capdata is field 7, data_fragment is field 8. For OUT control
            # transfers the data lives in data_fragment.
            data = parts[8] if len(parts) > 8 and parts[8] else (parts[7] if len(parts) > 7 else "")
            records.append({
                "frame": int(fn),
                "ts": float(ts),
                "bmRequestType": int(brt, 16) if brt.startswith("0x") else int(brt),
                "bRequest": int(breq, 16) if breq.startswith("0x") else int(breq),
                "wValue": int(wval, 16) if wval.startswith("0x") else int(wval),
                "wIndex": int(widx, 16) if widx.startswith("0x") else int(widx),
                "wLength": int(wlen, 16) if wlen.startswith("0x") else int(wlen),
                "data": data.replace(":", ""),
            })
        except (ValueError, IndexError):
            continue
    return records


def decode_record(rec: dict) -> str:
    breq = rec["bRequest"]
    vreq_name = VREQ_NAMES.get(breq, f"req={breq}")
    direction = "IN " if rec["bmRequestType"] & 0x80 else "OUT"

    if breq in (6, 7):
        # USB_MULTI_*; wIndex = register address; data = LE u32 (or larger)
        addr = rec["wIndex"]
        label = _reg_label(addr)
        if rec["data"] and rec["wLength"] == 4:
            # Decode u32 LE
            d = bytes.fromhex(rec["data"])
            if len(d) >= 4:
                val = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
                return f"{vreq_name:18s} {direction} {label:24s} = 0x{val:08x}"
        if rec["wLength"] > 4 and rec["data"]:
            # Multi-byte write (e.g. FW upload chunk)
            return f"{vreq_name:18s} {direction} {label:24s}  ({rec['wLength']} bytes) {rec['data'][:32]}..."
        return f"{vreq_name:18s} {direction} {label:24s}  (wLen={rec['wLength']})"

    if breq == 1:
        # USB_DEVICE_MODE; wValue = mode
        modes = {1: "USB_MODE_RESET", 2: "USB_MODE_UNPLUG", 4: "USB_MODE_TEST", 8: "USB_MODE_FIRMWARE"}
        mode = modes.get(rec["wValue"], f"mode=0x{rec['wValue']:x}")
        return f"{vreq_name:18s} {direction} {mode}  wIndex=0x{rec['wIndex']:x}"

    if breq == 9:
        return f"{vreq_name:18s} {direction}  (wLen={rec['wLength']})  EEPROM dump"

    return f"{vreq_name:18s} {direction} wVal=0x{rec['wValue']:x} wIdx=0x{rec['wIndex']:x} wLen={rec['wLength']}"


def collapse_fw_chunks(records):
    """The FW upload is 64 × 64-byte chunks at addresses 0x3000..0x3FC0.
    Collapse them into a single summary record."""
    out = []
    fw_chunks = []
    for r in records:
        if r["bRequest"] == 6 and 0x3000 <= r["wIndex"] < 0x4000 and r["wLength"] == 64:
            fw_chunks.append(r)
            continue
        if fw_chunks:
            first = fw_chunks[0]
            last = fw_chunks[-1]
            out.append({
                **first,
                "_summary": (
                    f"USB_MULTI_WRITE   OUT FW_IMAGE @ 0x{first['wIndex']:04x}.."
                    f"0x{last['wIndex']:04x}  ({len(fw_chunks)} × 64 = "
                    f"{len(fw_chunks) * 64} bytes)"
                ),
            })
            fw_chunks = []
        out.append(r)
    if fw_chunks:
        first, last = fw_chunks[0], fw_chunks[-1]
        out.append({
            **first,
            "_summary": (
                f"USB_MULTI_WRITE   OUT FW_IMAGE @ 0x{first['wIndex']:04x}.."
                f"0x{last['wIndex']:04x}  ({len(fw_chunks)} × 64 = "
                f"{len(fw_chunks) * 64} bytes)"
            ),
        })
    return out


def dump(pcap_path: Path, *, only_writes: bool = False, t_start: float | None = None,
         t_end: float | None = None):
    records = extract_vendor_ctrl(pcap_path)
    records = collapse_fw_chunks(records)
    t0 = records[0]["ts"] if records else 0
    for r in records:
        if t_start is not None and (r["ts"] - t0) < t_start:
            continue
        if t_end is not None and (r["ts"] - t0) > t_end:
            break
        if only_writes and r.get("bmRequestType", 0) & 0x80:
            continue
        line = r.get("_summary") or decode_record(r)
        elapsed = r["ts"] - t0
        print(f"  [f={r['frame']:>5} t={elapsed:7.3f}s]  {line}")


def diff_register_writes(kernel_pcap: Path, ours_pcap: Path):
    """Show register writes done by kernel that ours doesn't, and vice
    versa. Aggregates by (address, value) — useful for spotting writes
    we skipped."""
    def _writes(pcap_path: Path):
        recs = extract_vendor_ctrl(pcap_path)
        writes = {}   # addr -> last value written
        for r in recs:
            if r["bRequest"] not in (6, 2):
                continue
            if r["bmRequestType"] & 0x80:
                continue
            if r["wLength"] != 4 or not r["data"]:
                continue
            d = bytes.fromhex(r["data"])
            if len(d) < 4:
                continue
            val = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
            writes[r["wIndex"]] = val
        return writes

    kernel = _writes(kernel_pcap)
    ours = _writes(ours_pcap)

    print("=== Registers kernel wrote but we DIDN'T ===")
    for addr in sorted(kernel):
        if addr not in ours:
            print(f"  {_reg_label(addr):24s} (0x{addr:04x}) kernel wrote 0x{kernel[addr]:08x}")

    print()
    print("=== Registers both wrote but with DIFFERENT values ===")
    for addr in sorted(kernel):
        if addr in ours and kernel[addr] != ours[addr]:
            print(f"  {_reg_label(addr):24s} (0x{addr:04x}) kernel=0x{kernel[addr]:08x}  ours=0x{ours[addr]:08x}")

    print()
    print("=== Registers we wrote but kernel DIDN'T ===")
    for addr in sorted(ours):
        if addr not in kernel:
            print(f"  {_reg_label(addr):24s} (0x{addr:04x}) ours wrote 0x{ours[addr]:08x}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["kernel", "ours", "diff"])
    ap.add_argument("pcap", nargs="?", default=None, help="for 'ours' / 'diff'")
    ap.add_argument("--writes-only", action="store_true", help="only show OUT (writes)")
    ap.add_argument("--start", type=float, default=None,
                    help="seconds-from-start to begin dumping")
    ap.add_argument("--end", type=float, default=None,
                    help="seconds-from-start to stop dumping")
    args = ap.parse_args()

    if args.mode == "kernel":
        print(f"# Kernel pcap: {KERNEL_PCAP}")
        dump(KERNEL_PCAP, only_writes=args.writes_only, t_start=args.start, t_end=args.end)
    elif args.mode == "ours":
        if not args.pcap:
            sys.exit("'ours' mode requires <our.pcap>")
        print(f"# Our pcap: {args.pcap}")
        dump(Path(args.pcap), only_writes=args.writes_only, t_start=args.start, t_end=args.end)
    elif args.mode == "diff":
        if not args.pcap:
            sys.exit("'diff' mode requires <our.pcap>")
        diff_register_writes(KERNEL_PCAP, Path(args.pcap))


if __name__ == "__main__":
    main()
