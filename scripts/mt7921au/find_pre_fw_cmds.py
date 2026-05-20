"""Hunt for MCU commands on EP 0x08 in a window — decode cid byte.

For MT7921AU each EP 0x08 bulk-OUT payload starts with:
  [4B SDIO hdr] [64B mt76_connac2_mcu_txd] [payload]
The cid byte sits at offset 4+36 = 40 in the URB payload (after the 4-byte
SDIO header + 36-byte txd header proper).
"""
from __future__ import annotations
import struct, sys
from pathlib import Path

PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
PCAPNG_EPB = 0x00000006


def iter_pcapng(path):
    with path.open("rb") as f:
        magic = f.read(4)
        assert magic == b"\x0a\x0d\x0d\x0a"
        block_total_len_raw = f.read(4)
        bom = f.read(4)
        endian = "<" if struct.unpack("<I", bom)[0] == PCAPNG_BYTE_ORDER_MAGIC else ">"
        block_total_len = struct.unpack(f"{endian}I", block_total_len_raw)[0]
        f.read(block_total_len - 12)
        frame_no = 0
        while True:
            t = f.read(4)
            if not t or len(t) < 4: return
            l = f.read(4)
            block_type = struct.unpack(f"{endian}I", t)[0]
            block_len = struct.unpack(f"{endian}I", l)[0]
            body = f.read(block_len - 12)
            f.read(4)
            if block_type != PCAPNG_EPB or len(body) < 20: continue
            cap_len = struct.unpack(f"{endian}I", body[12:16])[0]
            data = body[20:20 + cap_len]
            frame_no += 1
            yield frame_no, data


def main():
    pcap = Path(sys.argv[1])
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    end = int(sys.argv[3]) if len(sys.argv) > 3 else 15200

    for frame_no, data in iter_pcapng(pcap):
        if frame_no < start: continue
        if frame_no > end: break
        if len(data) < 16: continue
        evt = chr(data[8]); xfer = data[9]; ep = data[10]
        if evt != "S" or xfer != 3 or ep != 0x08: continue
        urb = data[64:]
        if len(urb) < 4 + 40 + 4: continue
        cid = urb[4 + 36]    # offset 4 (SDIO hdr) + 36 (txd meta start) = 40
        seq = urb[4 + 39]    # seq at +3 inside meta
        sdio = struct.unpack("<I", urb[:4])[0]
        print(f"frame {frame_no:>5}: SDIO len=0x{sdio & 0xffff:04x} "
              f"cid=0x{cid:02x} seq=0x{seq:02x}  total_urb={len(urb)} B")


if __name__ == "__main__":
    main()
