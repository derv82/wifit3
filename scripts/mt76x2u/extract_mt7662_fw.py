"""Extract MT7612U firmware blobs from a USB capture (cleanroom).

The mt76x2u kernel driver uploads two firmware blobs in sequence:
  1. ROM patch (mt7662_rom_patch.bin) -> SRAM offset 0x00090000
  2. Main FW    (mt7662.bin)            -> ILM @ 0x00080000, DLM @ 0x00110000(+0x800 if rev>=E3)

Per data_dumps/mt76-source-v6.18/mt76x02_usb_mcu.c::__mt76x02u_mcu_fw_send_data,
each chunk is uploaded as:

  vendor write (bReq=0x42, MT_VEND_WRITE_FCE):
      [wValue=val&0xFFFF, wIndex=0x0230, no payload]  (DMA_ADDR low)
      [wValue=val>>16,    wIndex=0x0232, no payload]  (DMA_ADDR high)
      [wValue=val&0xFFFF, wIndex=0x0234, no payload]  (DMA_LEN  low)
      [wValue=val>>16,    wIndex=0x0236, no payload]  (DMA_LEN  high)

  bulk OUT on out_ep[0] (MT_EP_OUT_INBAND_CMD):
      [4B  mt76 info: PORT|LEN|TYPE_CMD]
      [N   chunk bytes]
      [4B  trailing zero pad]

The blobs in linux-firmware include a small structured header BEFORE the
payload bytes that get pushed on the wire:
  - ROM patch:  struct mt76x02_patch_header (~14 bytes incl. build_time + crc)
  - Main FW:    struct mt76x02_fw_header   (32 bytes incl. ilm_len/dlm_len/fw_ver)
These headers never appear on the wire. We save the header-stripped bodies
under chips/mt76x2u/assets/; firmware.py skips the header-read step at load
time. If a linux-firmware checkout is available, byte-verify both bodies
against `linux-firmware/mediatek/mt7662{_rom_patch,}.bin[header_size:]`.

Section split heuristic: each section starts with a `bReq=0x01 wValue=0x0001`
vendor request (mt76x02u_mcu_fw_reset). The second of these marks the
boundary between ROM patch and main FW. The trigger to RUN the FW is
`bReq=0x01 wValue=0x0012` (mt76x2u_mcu_load_ivb).
"""
from __future__ import annotations
import argparse
import struct
import sys
from pathlib import Path

PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
PCAPNG_EPB = 0x00000006

MT_VEND_WRITE_FCE = 0x42
MT_VEND_DEV_MODE = 0x01
MT_FCE_DMA_ADDR = 0x0230
MT_FCE_DMA_LEN = 0x0234
EP_OUT_INBAND_CMD = 0x08
MT_INFO_HDR_LEN = 4
MT_INFO_TAIL_LEN = 4


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


def extract(pcap_path: Path):
    # chunk records: list of (dst_addr, payload_bytes)
    rom_records: list[tuple[int, bytes]] = []
    fw_records: list[tuple[int, bytes]] = []
    section = 0  # 0=pre-reset, 1=rom-patch, 2=main-fw, 3=post-IVB
    last_dst_lo = 0
    last_dst_hi = 0
    pending_dst = None
    frame_ranges = [(None, None), (None, None)]

    for frame_no, data in iter_pcapng(pcap_path):
        if len(data) < 16:
            continue
        evt = chr(data[8])
        xfer = data[9]
        ep = data[10]

        # Submit only (Completion duplicates everything)
        if evt != "S":
            continue

        # Control transfer
        if xfer == 2:
            if len(data) < 48:
                continue
            setup = data[40:48]
            bmReq, bReq = setup[0], setup[1]
            wVal = struct.unpack("<H", setup[2:4])[0]
            wIdx = struct.unpack("<H", setup[4:6])[0]

            if bmReq == 0x40 and bReq == MT_VEND_DEV_MODE and wVal == 0x0001:
                # mt76x02u_mcu_fw_reset — section boundary marker
                section += 1
                if section > 2:
                    section = 2
                continue
            if bmReq == 0x40 and bReq == MT_VEND_DEV_MODE and wVal == 0x0012:
                # mt76x2u_mcu_load_ivb — done uploading
                break
            if bmReq == 0x40 and bReq == MT_VEND_WRITE_FCE:
                if wIdx == MT_FCE_DMA_ADDR:
                    last_dst_lo = wVal
                elif wIdx == MT_FCE_DMA_ADDR + 2:
                    last_dst_hi = wVal
                    pending_dst = (last_dst_hi << 16) | last_dst_lo

        # Bulk-OUT chunk on out_ep[0]
        elif xfer == 3 and ep == EP_OUT_INBAND_CMD:
            urb_payload = data[64:]
            if len(urb_payload) <= MT_INFO_HDR_LEN + MT_INFO_TAIL_LEN:
                continue
            chunk = urb_payload[MT_INFO_HDR_LEN:-MT_INFO_TAIL_LEN]
            dst = pending_dst if pending_dst is not None else 0
            pending_dst = None

            if section == 1:
                rom_records.append((dst, chunk))
                f0, _ = frame_ranges[0]
                frame_ranges[0] = (f0 or frame_no, frame_no)
            elif section == 2:
                fw_records.append((dst, chunk))
                f0, _ = frame_ranges[1]
                frame_ranges[1] = (f0 or frame_no, frame_no)

    rom_body = b"".join(c for _, c in rom_records)

    # Split main FW into ILM + DLM based on dst-addr region.
    ilm_records = [r for r in fw_records if 0x080000 <= r[0] < 0x100000]
    dlm_records = [r for r in fw_records if r[0] >= 0x100000]
    ilm_body = b"".join(c for _, c in ilm_records)
    dlm_body = b"".join(c for _, c in dlm_records)
    dlm_base = dlm_records[0][0] if dlm_records else 0

    print("=== ROM patch ===")
    print(f"  chunks: {len(rom_records)}, body: {len(rom_body)} bytes")
    print(f"  frame range: {frame_ranges[0][0]}..{frame_ranges[0][1]}")
    if rom_records:
        print(f"  dst addrs: 0x{rom_records[0][0]:08x} .. 0x{rom_records[-1][0]:08x}")
    print()
    print("=== Main FW ===")
    print(f"  total chunks: {len(fw_records)}, body: {len(ilm_body) + len(dlm_body)} bytes")
    print(f"  frame range: {frame_ranges[1][0]}..{frame_ranges[1][1]}")
    print(f"  ILM: {len(ilm_records)} chunks, {len(ilm_body)} bytes "
          f"-> 0x{0x80000:08x}")
    print(f"  DLM: {len(dlm_records)} chunks, {len(dlm_body)} bytes "
          f"-> 0x{dlm_base:08x}  "
          f"(rev>=E3: offset = 0x110000 + 0x800 = 0x110800)")

    return rom_body, ilm_body, dlm_body, dlm_base


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pcap", type=Path, help="Path to USB capture .pcap")
    p.add_argument("--out-dir", type=Path,
                   default=Path("src/wifit3/chips/mt76x2u/assets"),
                   help="Output dir (default: src/wifit3/chips/mt76x2u/assets/)")
    p.add_argument("--verify-fw", type=Path, default=None,
                   help="linux-firmware mt7662.bin to byte-verify against (body only)")
    p.add_argument("--verify-rom", type=Path, default=None,
                   help="linux-firmware mt7662_rom_patch.bin to byte-verify against (body only)")
    args = p.parse_args()

    print(f"[*] Reading {args.pcap}")
    rom_body, ilm_body, dlm_body, dlm_base = extract(args.pcap)
    if not rom_body or not ilm_body or not dlm_body:
        print("[-] One or more bodies are empty. Check pcap content.")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rom_path = args.out_dir / "mt7662_rom_patch_body.bin"
    ilm_path = args.out_dir / "mt7662_ilm.bin"
    dlm_path = args.out_dir / "mt7662_dlm.bin"
    rom_path.write_bytes(rom_body)
    ilm_path.write_bytes(ilm_body)
    dlm_path.write_bytes(dlm_body)
    print(f"[+] Wrote {rom_path} ({len(rom_body)} bytes)")
    print(f"[+] Wrote {ilm_path} ({len(ilm_body)} bytes)")
    print(f"[+] Wrote {dlm_path} ({len(dlm_body)} bytes, target=0x{dlm_base:08x})")

    rc = 0
    if args.verify_rom:
        rc |= _verify(rom_body, args.verify_rom, "ROM patch")
    if args.verify_fw:
        # main FW body = ILM + DLM concatenated (matches linux-firmware
        # mt7662.bin layout after the 32-byte mt76x02_fw_header).
        rc |= _verify(ilm_body + dlm_body, args.verify_fw, "main FW")
    return rc


def _verify(blob: bytes, reference: Path, name: str) -> int:
    ref = reference.read_bytes()
    # Try a range of plausible header sizes (mt76x02 headers are small + structured).
    for hdr in (0, 14, 32, 64):
        if ref[hdr:] == blob:
            print(f"[+] {name}: BYTE-FOR-BYTE MATCH against "
                  f"{reference} skipping {hdr}-byte header")
            return 0
    print(f"[-] {name}: no match against {reference} for any header size in (0,14,32,64)")
    print(f"    extracted len = {len(blob)}, reference len = {len(ref)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
