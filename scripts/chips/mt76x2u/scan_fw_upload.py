"""Walk capture-1.pcap and identify the MT7612U FW upload region.

Looks for:
  - Vendor writes with bRequest=0x42 (MT_VEND_WRITE_FCE) to MT_FCE_DMA_ADDR
    (0x0230) — these mark the start of each FW chunk transfer.
  - Bulk-OUT on the inband-cmd endpoint (descriptor order: first bulk-OUT).
  - The boot-time mass-storage→wireless switch (bRequest=0x01 to MT_VEND_DEV_MODE).
"""
from __future__ import annotations
import struct, sys
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
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    end = int(sys.argv[3]) if len(sys.argv) > 3 else 3000

    first_fce = None
    last_fce = None
    n_fce_writes = 0
    n_dma_addr = 0
    n_dma_len = 0
    bulk_out_counts = {}
    bulk_out_first = {}
    bulk_out_last = {}
    bulk_in_counts = {}
    vend_dev_mode_frames = []
    fw_dma_addrs = []

    for frame_no, data in iter_pcapng(pcap):
        if frame_no < start: continue
        if frame_no > end: break
        if len(data) < 16: continue
        evt = chr(data[8]); xfer = data[9]; ep = data[10]
        # Submit only — Completions duplicate everything
        if evt != "S": continue

        # Control transfer
        if xfer == 2:
            if len(data) < 48: continue
            setup = data[40:48]
            bmReq, bReq = setup[0], setup[1]
            wVal = struct.unpack("<H", setup[2:4])[0]
            wIdx = struct.unpack("<H", setup[4:6])[0]
            wLen = struct.unpack("<H", setup[6:8])[0]
            # MT_VEND_WRITE_FCE = 0x42
            if bmReq == 0x40 and bReq == 0x42:
                n_fce_writes += 1
                if first_fce is None: first_fce = frame_no
                last_fce = frame_no
                addr = (wVal << 16) | wIdx
                if addr == 0x0230:  # MT_FCE_DMA_ADDR
                    n_dma_addr += 1
                    # The data isn't in this URB (it's in the Complete), but in usbmon
                    # the Submit for a Host-to-Device transfer does include the payload
                    # in 'data' tail.
                    if len(data) > 64 and wLen == 4:
                        payload = data[64:64 + wLen]
                        dst = struct.unpack("<I", payload)[0]
                        fw_dma_addrs.append((frame_no, dst))
                elif addr == 0x0234:  # MT_FCE_DMA_LEN
                    n_dma_len += 1
            elif bmReq == 0x40 and bReq == 0x01:
                vend_dev_mode_frames.append((frame_no, wVal, wIdx))

        # Bulk transfer
        elif xfer == 3:
            cnt = bulk_out_counts if not (ep & 0x80) else bulk_in_counts
            cnt[ep] = cnt.get(ep, 0) + 1
            if not (ep & 0x80):
                bulk_out_first.setdefault(ep, frame_no)
                bulk_out_last[ep] = frame_no

    print(f"=== Frame range scanned: {start}..{end} ===")
    print()
    print(f"MT_VEND_WRITE_FCE (0x42): {n_fce_writes} writes "
          f"(frames {first_fce}..{last_fce})")
    print(f"    to MT_FCE_DMA_ADDR (0x0230): {n_dma_addr}")
    print(f"    to MT_FCE_DMA_LEN  (0x0234): {n_dma_len}")
    print()
    print(f"MT_VEND_DEV_MODE (bReq=0x01) writes: {len(vend_dev_mode_frames)}")
    for f, v, i in vend_dev_mode_frames[:15]:
        print(f"  frame {f}: wValue=0x{v:04x} wIndex=0x{i:04x}")
    print()
    print("Bulk-OUT EP traffic:")
    for ep in sorted(bulk_out_counts):
        print(f"  EP 0x{ep:02x}: {bulk_out_counts[ep]} URBs "
              f"(first={bulk_out_first[ep]}, last={bulk_out_last[ep]})")
    print()
    print("Bulk-IN EP traffic:")
    for ep in sorted(bulk_in_counts):
        print(f"  EP 0x{ep:02x}: {bulk_in_counts[ep]} URBs")
    print()
    print(f"First 10 DMA_ADDR targets:")
    for f, addr in fw_dma_addrs[:10]:
        print(f"  frame {f}: dst=0x{addr:08x}")
    print(f"Last 5 DMA_ADDR targets:")
    for f, addr in fw_dma_addrs[-5:]:
        print(f"  frame {f}: dst=0x{addr:08x}")


if __name__ == "__main__":
    main()
