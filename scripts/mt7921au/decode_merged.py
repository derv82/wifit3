"""
Merged post-boot op stream: interleave control-transfer register ops (EP0) with
bulk MCU commands (EP 0x08) and FW/TX bulk (EP 0x04), in capture order, starting
at FW_START. This is what verify_pcap CHECK 3 must walk with a single cursor.

Answers: how many register writes vs MCU commands post-boot, and in what order.

Usage: uv run python scripts/mt7921au/decode_merged.py [<pcap>] [--head N]
"""
import struct
import sys
from pathlib import Path

DEFAULT_CAP = "usb_dumps_new/captures_mt7921u_pau0f-no-adapter-scatter/capture-3.pcap"
PREFETCH0 = 0x7C024600
REG_BREQ = {0x63, 0x66, 0x01, 0x02}
EP_MCU_OUT, EP_FW_OUT = 0x08, 0x04


def parse_pcapng(path):
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def detect_dev(pkts):
    for pkt in pkts:
        if len(pkt) < 48 or pkt[9] != 0x02 or pkt[8] != 0x53 or pkt[41] not in REG_BREQ:
            continue
        wValue, wIndex = struct.unpack_from("<HH", pkt, 42)
        if ((wValue << 16) | wIndex) == PREFETCH0:
            return pkt[11]
    return None


def main():
    head = int(sys.argv[sys.argv.index("--head") + 1]) if "--head" in sys.argv else 80
    skip = {"--head", str(head)}
    args = [a for a in sys.argv[1:] if a not in skip and not a.startswith("--")]
    cap = args[0] if args else DEFAULT_CAP
    pkts = parse_pcapng(cap)
    dev = detect_dev(pkts)

    ops = []     # (pkt_index, kind, detail)
    pending = {}
    for i, pkt in enumerate(pkts):
        if len(pkt) < 40 or pkt[11] != dev:
            continue
        urb_type, xfer, ep = pkt[8], pkt[9], pkt[10]
        urb_id = pkt[0:8]
        if xfer == 0x02:    # control
            if urb_type == 0x53:
                bmReq, bReq = pkt[40], pkt[41]
                if bReq not in REG_BREQ:
                    continue
                wValue, wIndex, wLength = struct.unpack_from("<HHH", pkt, 42)
                addr = (wValue << 16) | wIndex
                if bmReq & 0x80:
                    pending[urb_id] = addr
                elif wLength >= 4 and len(pkt) >= 68:
                    val = struct.unpack_from("<I", pkt, 64)[0]
                    ops.append((i, "REG_WR", f"0x{addr:08x} = 0x{val:08x}"))
            elif urb_type == 0x43:
                addr = pending.pop(urb_id, None)
                if addr is not None and len(pkt) >= 68:
                    val = struct.unpack_from("<I", pkt, 64)[0]
                    ops.append((i, "REG_RD", f"0x{addr:08x} -> 0x{val:08x}"))
        elif xfer == 0x03 and urb_type == 0x53 and not (ep & 0x80):  # bulk OUT submit
            lencap = struct.unpack_from("<I", pkt, 36)[0]
            data = pkt[64:64 + min(lencap, len(pkt) - 64)]
            if ep == EP_MCU_OUT and len(data) >= 44:
                sdio = struct.unpack_from("<I", data, 0)[0] & 0xFFFF
                is_std = (data[38] == 0x00 and data[39] == 0x80)
                if is_std:
                    cid, ext = data[40], data[45]
                    tag = f"MCU_STD cid=0x{cid:02x} ext=0x{ext:02x}"
                else:
                    cid = struct.unpack_from("<H", data, 38)[0]
                    tag = f"MCU_UNI cid=0x{cid:02x}"
                ops.append((i, "BULK08", f"{tag} sdio={sdio}"))
            elif ep == EP_FW_OUT:
                ops.append((i, "BULK04", f"{lencap}B"))

    # Find FW_START (BULK08 MCU_STD cid=0x02) and split.
    fw = next((k for k, o in enumerate(ops)
               if o[1] == "BULK08" and "cid=0x02 ext=0x00" in o[2]), None)
    post = ops[fw + 1:] if fw is not None else ops
    # Count by kind in post-boot region.
    from collections import Counter
    counts = Counter(o[1] for o in post)
    print(f"{cap}: dev {dev}, {len(ops)} merged ops, FW_START at op {fw}")
    print(f"post-boot op kinds: {dict(counts)}\n")
    print(f"first {head} post-boot ops (capture order):")
    for k, (pi, kind, detail) in enumerate(post[:head]):
        print(f"  {k:>4}  pkt{pi:<6} {kind:<7} {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
