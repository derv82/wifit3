"""
Parse the pcapng/usbmon capture directly and extract the kernel's CONTROL-transfer
register WRITES (addr + value) — the data tshark's usb.capdata field doesn't surface.
This is the ground truth our dma_init magic numbers were never diffed against.

Usage: uv run python scripts/mt7921au/extract_reg_writes.py <pcap> [--until-ep08]
"""
import struct
import sys

path = sys.argv[1]
until_first_mcu = "--until-ep08" in sys.argv

with open(path, "rb") as f:
    data = f.read()

# pcapng: little-endian assumed (SHB byte-order magic 0x1A2B3C4D).
packets = []
off = 0
while off + 12 <= len(data):
    btype, blen = struct.unpack_from("<II", data, off)
    if blen < 12 or off + blen > len(data):
        break
    if btype == 0x00000006:  # Enhanced Packet Block
        # body: iface_id, ts_hi, ts_lo, cap_len, orig_len, then packet data
        iface_id, ts_hi, ts_lo, cap_len, orig_len = struct.unpack_from("<IIIII", data, off + 8)
        pkt = data[off + 8 + 20: off + 8 + 20 + cap_len]
        ts = (ts_hi << 32) | ts_lo   # microseconds (default if_tsresol)
        packets.append((ts, pkt))
    off += blen

if not packets:
    print("no packets parsed (format?)")
    sys.exit(1)

t0 = packets[0][0]
print(f"{len(packets)} packets. Device-5 control WRITES (S, control, OUT):")
print("   time      bmReq bReq  addr        value")
first_mcu_t = None
for ts, pkt in packets:
    if len(pkt) < 64:
        continue
    urb_type = pkt[8]      # 0x53 'S' submit
    xfer = pkt[9]          # 2 = control, 3 = bulk
    epnum = pkt[10]
    devnum = pkt[11]
    if devnum != 5:
        continue
    # note when the first MCU bulk command (EP 0x08 OUT) appears = end of cold init
    if first_mcu_t is None and urb_type == 0x53 and xfer == 0x03 and epnum == 0x08:
        first_mcu_t = ts
    if urb_type != 0x53 or xfer != 0x02:
        continue
    bmReq, bReq = pkt[40], pkt[41]
    if bmReq & 0x80:       # IN (read) — skip, we want writes
        continue
    if bReq not in (0x66, 0x02):   # unified write / UHW write
        continue
    wValue = struct.unpack_from("<H", pkt, 42)[0]
    wIndex = struct.unpack_from("<H", pkt, 44)[0]
    wLength = struct.unpack_from("<H", pkt, 46)[0]
    addr = (wValue << 16) | wIndex
    val = struct.unpack_from("<I", pkt, 64)[0] if len(pkt) >= 68 and wLength >= 4 else None
    rel = (ts - t0) / 1e6
    if until_first_mcu and first_mcu_t and ts > first_mcu_t:
        break
    vstr = f"0x{val:08x}" if val is not None else "(none)"
    print(f"  {rel:8.4f}  0x{bmReq:02x}  0x{bReq:02x}  0x{addr:08x}  {vstr}")
