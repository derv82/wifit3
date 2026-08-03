"""Find the post-FW-upload MCU CMD frames on EP 0x08 in capture-1.pcap.

After frame 965 (IVB trigger), the next bulk-OUT URBs on EP 0x08 are
function_select(Q_SELECT,1) + set_radio_state(true). Print their TXINFO
+ payload for cross-checking against our wifit3 port.
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
PCAPNG_EPB = 0x00000006


def iter_pcapng(path: Path):
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
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 970
    end = int(sys.argv[3]) if len(sys.argv) > 3 else 1500
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 8

    count = 0
    for frame_no, data in iter_pcapng(pcap):
        if frame_no < start: continue
        if frame_no > end: break
        if len(data) < 16: continue
        evt = chr(data[8]); xfer = data[9]; ep = data[10]
        if evt != "S": continue
        if xfer != 3: continue
        if ep != 0x08: continue
        urb_payload = data[64:]
        if len(urb_payload) < 4: continue
        txinfo = struct.unpack("<I", urb_payload[:4])[0]
        # Decode
        T = (txinfo >> 30) & 0x3
        DPORT = (txinfo >> 27) & 0x7
        CMD_TYPE = (txinfo >> 20) & 0x7F
        CMD_SEQ = (txinfo >> 16) & 0xF
        LEN = txinfo & 0xFFFF
        print(f"--- frame {frame_no}: EP 0x08 bulk-OUT len={len(urb_payload)} ---")
        print(f"  TXINFO = 0x{txinfo:08x}  TYPE={T} DPORT={DPORT} "
              f"CMD_TYPE={CMD_TYPE} CMD_SEQ={CMD_SEQ} LEN={LEN}")
        payload = urb_payload[4:4 + LEN]
        print(f"  payload({len(payload)}): {payload.hex()}")
        count += 1
        if count >= limit:
            break


if __name__ == "__main__":
    main()
