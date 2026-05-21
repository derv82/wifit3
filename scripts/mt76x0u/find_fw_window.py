"""Locate the FW-upload window across all mt76x0u captures.

For each capture, walks the entire pcap and reports:
  - frame range of mass-storage-stub enumeration (bInterfaceClass=0x08 absent
    in usbmon, so we use bDeviceClass=0x08 in any GET_DESCRIPTOR(DEVICE) response,
    or just look for SCSI-class bulk-OUT lengths 31 / 0x55534243 CBW magic)
  - first FW-reset frame (bmReq=0x40, bReq=0x01 MT_VEND_DEV_MODE, wValue=0x0001)
  - first IVB-trigger frame (bmReq=0x40, bReq=0x01, wValue=0x0012, wLen=0x40)
  - count of bulk-OUT chunks to EP 0x08 between reset and IVB
  - first/last vendor read or write (the windows of vendor-protocol activity)

That tells us deterministically which capture (if any) contains the FW upload,
and where in the frame timeline it sits.
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

PCAPNG_EPB = 0x00000006

MT_VEND_DEV_MODE  = 0x01
MT_VEND_MULTI_WR  = 0x06
MT_VEND_MULTI_RD  = 0x07
MT_VEND_WRITE_FCE = 0x42
EP_OUT_INBAND_CMD = 0x08


def iter_pcapng(path: Path):
    with path.open("rb") as f:
        magic = f.read(4)
        assert magic == b"\x0a\x0d\x0d\x0a"
        block_total_len_raw = f.read(4)
        bom = f.read(4)
        endian = "<" if struct.unpack("<I", bom)[0] == 0x1A2B3C4D else ">"
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
            yield frame_no, data


def scan(pcap_path: Path) -> None:
    print(f"\n=== {pcap_path.name} ===")
    fw_reset_frames: list[int] = []
    ivb_frames: list[int] = []
    bulk_out_08_chunks: list[tuple[int, int]] = []  # (frame, payload_len)
    first_vendor_frame = None
    last_vendor_frame = None
    fce_writes = 0
    multi_reads = 0
    multi_writes = 0
    mass_storage_frames: list[int] = []
    last_frame = 0

    for frame_no, data in iter_pcapng(pcap_path):
        last_frame = frame_no
        if len(data) < 16:
            continue
        evt = chr(data[8])
        xfer = data[9]
        ep = data[10]

        # Only look at Submit events (Completion duplicates).
        if evt != "S":
            continue

        # Control transfer
        if xfer == 2 and len(data) >= 48:
            setup = data[40:48]
            bmReq, bReq = setup[0], setup[1]
            wVal = struct.unpack("<H", setup[2:4])[0]
            wLen = struct.unpack("<H", setup[6:8])[0]

            # Vendor-out: bmReq=0x40, vendor-in: bmReq=0xc0
            if bmReq in (0x40, 0xc0):
                if first_vendor_frame is None:
                    first_vendor_frame = frame_no
                last_vendor_frame = frame_no
                if bReq == MT_VEND_DEV_MODE and wVal == 0x0001:
                    fw_reset_frames.append(frame_no)
                elif bReq == MT_VEND_DEV_MODE and wVal == 0x0012:
                    ivb_frames.append(frame_no)
                elif bReq == MT_VEND_WRITE_FCE:
                    fce_writes += 1
                elif bReq == MT_VEND_MULTI_RD:
                    multi_reads += 1
                elif bReq == MT_VEND_MULTI_WR:
                    multi_writes += 1

        # Bulk-OUT
        elif xfer == 3 and ep == EP_OUT_INBAND_CMD:
            payload_len = len(data) - 64
            if payload_len > 0:
                bulk_out_08_chunks.append((frame_no, payload_len))

        # SCSI mass-storage: look for CBW magic 0x43425355
        elif xfer == 3 and len(data) >= 68:
            payload = data[64:68]
            if payload == b"USBC":
                mass_storage_frames.append(frame_no)

    print(f"  total frames           : {last_frame}")
    print(f"  mass-storage CBWs      : {len(mass_storage_frames)}"
          + (f"  (frames {mass_storage_frames[:3]}..{mass_storage_frames[-3:]})"
             if mass_storage_frames else ""))
    print(f"  first vendor xfer frame: {first_vendor_frame}")
    print(f"  last  vendor xfer frame: {last_vendor_frame}")
    print(f"  FW-reset frames        : {fw_reset_frames}")
    print(f"  IVB-trigger frames     : {ivb_frames}")
    print(f"  EP-0x08 bulk-OUT chunks: {len(bulk_out_08_chunks)}")
    if bulk_out_08_chunks:
        print(f"    first/last frames    : {bulk_out_08_chunks[0][0]}..{bulk_out_08_chunks[-1][0]}")
        sizes = [s for _, s in bulk_out_08_chunks]
        print(f"    size  min/max        : {min(sizes)}..{max(sizes)} bytes")
    print(f"  MT_VEND_WRITE_FCE      : {fce_writes}")
    print(f"  MT_VEND_MULTI_READ     : {multi_reads}")
    print(f"  MT_VEND_MULTI_WRITE    : {multi_writes}")

    verdict = []
    if fw_reset_frames and ivb_frames and bulk_out_08_chunks:
        verdict.append("HAS FW UPLOAD")
    elif fw_reset_frames or ivb_frames:
        verdict.append("PARTIAL FW upload artifacts")
    else:
        verdict.append("NO FW upload — warm boot from chip's perspective")
    print(f"  verdict                : {' | '.join(verdict)}")


def main() -> int:
    pcap_dir = Path("usb_dumps/captures_mt76x0u")
    for p in sorted(pcap_dir.glob("capture-*.pcap")):
        scan(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
