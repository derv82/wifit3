"""
Characterize a usbmon pcapng: per (device, endpoint) tally of SUBMIT/COMPLETE
events and, crucially, how many bytes were actually CAPTURED on bulk-IN
completions (device->host RX).

Why this exists: the pre-scatter MT7921AU captures recorded every host->device
byte but ~zero device->host data, because mt76 RX uses scatter-gather buffers and
usbmon only snapshots urb->transfer_buffer (NULL for SG). With
`options mt76_usb disable_usb_sg=1` the RX path is linear and usbmon records it.
This script answers, from the bytes alone: did RX actually become visible?

Usage: uv run python scripts/mt7921au/analyze_usb_traffic.py <pcap> [devnum]
  (omit devnum to auto-list every device seen, then re-run with the mt7921's)
"""
import struct
import sys
from collections import defaultdict
from pathlib import Path

# usbmon mmapped pseudo-header (DLT_USB_LINUX_MMAPPED), 64 bytes, little-endian.
#  0  : u64 id
#  8  : u8  event_type   'S'=0x53 submit  'C'=0x43 complete  'E'=0x45 error
#  9  : u8  xfer_type    0=ISO 1=INT 2=CTRL 3=BULK
# 10  : u8  ep           bit7 = IN
# 11  : u8  devnum
# 12  : u16 busnum
# 14  : u8  flag_setup
# 15  : u8  flag_data
# 16  : s64 ts_sec
# 24  : s32 ts_usec
# 28  : s32 status
# 32  : u32 length       (urb requested length)
# 36  : u32 len_cap      (bytes actually captured into the file)
# 40  : 8 bytes setup / iso
# 48..63 misc
# 64..: data (len_cap bytes)
XFER = {0: "ISO", 1: "INT", 2: "CTRL", 3: "BULK"}


def parse_pcapng(path):
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:  # Enhanced Packet Block
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cap = sys.argv[1]
    want_dev = int(sys.argv[2]) if len(sys.argv) > 2 else None
    pkts = parse_pcapng(cap)
    print(f"{cap}: {len(pkts)} packets")

    devs = defaultdict(int)                       # devnum -> packet count
    # (dev, ep, dir, xfer) -> [submits, completes, cap_bytes_on_complete, max_single]
    tally = defaultdict(lambda: [0, 0, 0, 0])
    for pkt in pkts:
        if len(pkt) < 64:
            continue
        ev, xfer, ep, dev = pkt[8], pkt[9], pkt[10], pkt[11]
        devs[dev] += 1
        if want_dev is not None and dev != want_dev:
            continue
        ep_in = bool(ep & 0x80)
        len_cap = struct.unpack_from("<I", pkt, 36)[0]
        key = (dev, ep & 0x7F, "IN" if ep_in else "OUT", XFER.get(xfer, str(xfer)))
        t = tally[key]
        if ev == 0x53:        # SUBMIT
            t[0] += 1
        elif ev == 0x43:      # COMPLETE
            t[1] += 1
            actual = min(len_cap, len(pkt) - 64)
            t[2] += actual
            t[3] = max(t[3], actual)

    if want_dev is None:
        print("\ndevices seen (devnum: packets) — re-run with the mt7921's devnum:")
        for d in sorted(devs):
            print(f"  dev {d:3d}: {devs[d]} pkts")
        return 0

    print(f"\ndevice {want_dev} — per endpoint:")
    print(f"  {'EP':>4} {'DIR':>3} {'TYPE':>4} {'submits':>8} {'completes':>9} "
          f"{'capBytes':>10} {'maxOne':>7}")
    for key in sorted(tally):
        _, ep, d, xf = key
        s, c, cb, mx = tally[key]
        print(f"  0x{ep:02x} {d:>3} {xf:>4} {s:>8} {c:>9} {cb:>10} {mx:>7}")

    in_bulk_bytes = sum(v[2] for k, v in tally.items() if k[2] == "IN" and k[3] == "BULK")
    print(f"\n  >>> total bulk-IN bytes captured (device->host RX): {in_bulk_bytes}")
    if in_bulk_bytes < 1024:
        print("      RX is still effectively INVISIBLE — disable_usb_sg likely did NOT take.")
    else:
        print("      RX is VISIBLE — scatter-disable worked; device->host data recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
