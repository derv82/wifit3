"""Dump the payload of a specific frame number from a pcapng usbmon capture."""
import struct
import sys
from pathlib import Path

PCAPNG_EPB = 0x00000006


def main():
    pcap_path = Path(sys.argv[1])
    targets = set(int(x) for x in sys.argv[2:])

    with pcap_path.open("rb") as f:
        magic = f.read(4)
        assert magic == b"\x0a\x0d\x0d\x0a"
        block_total_len_raw = f.read(4)
        byte_order_magic_raw = f.read(4)
        if struct.unpack("<I", byte_order_magic_raw)[0] == 0x1A2B3C4D:
            endian = "<"
        else:
            endian = ">"
        block_total_len = struct.unpack(f"{endian}I", block_total_len_raw)[0]
        f.read(block_total_len - 12)

        frame_no = 0
        while True:
            t = f.read(4)
            if not t or len(t) < 4:
                return
            l = f.read(4)
            block_type = struct.unpack(f"{endian}I", t)[0]
            block_len = struct.unpack(f"{endian}I", l)[0]
            body = f.read(block_len - 12)
            f.read(4)
            if block_type != PCAPNG_EPB or len(body) < 20:
                continue
            cap_len = struct.unpack(f"{endian}I", body[12:16])[0]
            data = body[20:20 + cap_len]
            frame_no += 1
            if frame_no in targets:
                print(f"--- frame {frame_no} (len {cap_len}) ---")
                print(f"  evt={chr(data[8])!r} xfer={data[9]} ep=0x{data[10]:02x} "
                      f"dev={data[11]} bus={struct.unpack('<H', data[12:14])[0]}")
                if cap_len >= 48:
                    setup = data[40:48]
                    print(f"  setup: bmReq=0x{setup[0]:02x} bReq=0x{setup[1]:02x} "
                          f"wVal=0x{struct.unpack('<H', setup[2:4])[0]:04x} "
                          f"wIdx=0x{struct.unpack('<H', setup[4:6])[0]:04x} "
                          f"wLen=0x{struct.unpack('<H', setup[6:8])[0]:04x}")
                if cap_len > 64:
                    payload = data[64:]
                    print(f"  payload({len(payload)}): {payload.hex()}")


if __name__ == "__main__":
    main()
