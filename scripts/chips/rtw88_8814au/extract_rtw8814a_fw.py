"""Extract rtw8814a firmware from a USB capture (cleanroom).

The rtw88 8814A is a WCPU_3081 chip, so it uploads firmware via USB bulk-OUT
through the normal TX data path (the iDDMA path), NOT via control transfers
like the legacy 8051/MCUFWDL chips (8812a/8821a). See
`mac.c:start_download_firmware` + `mac.c:download_firmware_to_mem`:

- For each section (DMEM, then IMEM — the 8814a blob has no EMEM, mem_usage
  bit 4 is clear) the driver iterates `send_firmware_pkt(pg_addr, data+off,
  pkt_size)` with `pkt_size <= 4096`.
- `send_firmware_pkt` prepends a `tx_pkt_desc` header (TX_DESC_SIZE = 40 for
  8814a, per `.tx_pkt_desc_sz`) and writes it to the FW-upload bulk-OUT
  endpoint. On the AWUS1900 that is EP 0x02 (out_ep[0]).
- iDDMA register pokes (REG_DDMA_CH0SA/DA/CTRL) ride control transfers and
  don't affect FW reassembly.

Reassembly: iterate every Submit bulk-OUT URB on EP 0x02 in the bring-up
window, strip the 40-byte tx_pkt_desc, concatenate in pcap order. The result
equals `linux-firmware/rtw88/rtw8814a_fw.bin[FW_HDR_SIZE:]` (FW_HDR_SIZE = 64):
the 64-byte modern `rtw_fw_hdr` is read by the driver before upload and never
appears on the wire.

usbmon URB header is 64 bytes per pcap record; payload starts at offset 64.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

# 8814a tx_pkt_desc is 40 bytes (rtw8814a_hw_spec.tx_pkt_desc_sz). This is the
# one value that differs from the 8822bu extractor (which uses 48).
TX_DESC_SIZE = 40
FW_HDR_SIZE = 64
FW_HDR_CHKSUM_SIZE = 8
MAX_CHUNK_FW_BYTES = 0x1000  # 4096

# FW-upload bulk-OUT endpoint for the AWUS1900 (out_ep[0]). Confirmed from the
# cold-boot pcap: the only bulk-OUT pipe carrying >1KB chunks during bring-up.
EP_FW_UPLOAD = 0x02

PCAPNG_EPB = 0x00000006
PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D


def _iter_pcap_legacy(f, endian: str):
    frame_no = 0
    while True:
        pkt_hdr = f.read(16)
        if not pkt_hdr or len(pkt_hdr) < 16:
            return
        _, _, incl_len, _ = struct.unpack(f"{endian}IIII", pkt_hdr)
        data = f.read(incl_len)
        frame_no += 1
        if len(data) < 64:
            continue
        yield frame_no, data


def _iter_pcapng(f):
    f.read(4)  # block total length (leading copy)
    byte_order_magic_raw = f.read(4)
    if len(byte_order_magic_raw) < 4:
        return
    if struct.unpack("<I", byte_order_magic_raw)[0] == PCAPNG_BYTE_ORDER_MAGIC:
        endian = "<"
    elif struct.unpack(">I", byte_order_magic_raw)[0] == PCAPNG_BYTE_ORDER_MAGIC:
        endian = ">"
    else:
        raise ValueError("pcapng byte-order magic not recognized")
    f.seek(0)
    # Re-read the SHB cleanly now that endianness is known.
    f.read(4)
    block_total_len = struct.unpack(f"{endian}I", f.read(4))[0]
    f.read(block_total_len - 8)

    frame_no = 0
    while True:
        type_raw = f.read(4)
        if not type_raw or len(type_raw) < 4:
            return
        len_raw = f.read(4)
        if len(len_raw) < 4:
            return
        block_type = struct.unpack(f"{endian}I", type_raw)[0]
        block_len = struct.unpack(f"{endian}I", len_raw)[0]
        body = f.read(block_len - 12)
        if len(f.read(4)) < 4:
            return
        if block_type != PCAPNG_EPB or len(body) < 20:
            continue
        cap_len = struct.unpack(f"{endian}I", body[12:16])[0]
        if cap_len < 64:
            continue
        data = body[20:20 + cap_len]
        frame_no += 1
        if len(data) >= 64:
            yield frame_no, data


def _iter_urbs(pcap_path: Path):
    with pcap_path.open("rb") as f:
        magic4 = f.read(4)
        if len(magic4) < 4:
            raise ValueError("File too short to be a pcap")
        if magic4 == b"\x0a\x0d\x0d\x0a":
            yield from _iter_pcapng(f)
            return
        if magic4 == b"\xd4\xc3\xb2\xa1":
            endian = "<"
        elif magic4 == b"\xa1\xb2\xc3\xd4":
            endian = ">"
        else:
            raise ValueError(f"Unknown pcap magic {magic4.hex()}")
        f.read(20)
        yield from _iter_pcap_legacy(f, endian)


def extract(pcap_path: Path, bus: int | None, device: int | None,
            start_frame: int, end_frame: int | None) -> bytes:
    print(f"[*] Reading: {pcap_path}")
    print(f"[*] Frame window: {start_frame}..{end_frame or 'end'}")

    out = bytearray()
    chunk_count = 0
    first_frame: int | None = None
    last_frame: int | None = None
    section_breaks: list[tuple[int, int]] = []  # (frame, bytes_so_far)
    prev_chunk_size = 0

    for frame_no, data in _iter_urbs(pcap_path):
        if end_frame is not None and frame_no > end_frame:
            break
        if frame_no < start_frame:
            continue
        if data[8] != ord("S"):      # Submit
            continue
        if data[9] != 3:             # Bulk
            continue
        epnum = data[10]
        if epnum & 0x80:             # OUT only
            continue
        if (epnum & 0x7F) != EP_FW_UPLOAD:
            continue
        urb_devnum = data[11]
        urb_busnum = struct.unpack("<H", data[12:14])[0]
        if bus is not None and urb_busnum != bus:
            continue
        if device is not None and urb_devnum != device:
            continue
        if len(data) < 64 + TX_DESC_SIZE:
            continue

        urb_payload = data[64:]
        if len(urb_payload) <= TX_DESC_SIZE:
            continue
        fw_chunk = urb_payload[TX_DESC_SIZE:]

        # ZLP-avoidance: when (pkt_size + TX_DESC_SIZE) is a 512 multiple, the
        # kernel appends one byte before building the descriptor, so the wire
        # URB length is `% 512 == 1`. Trim it for a byte-identical blob.
        if len(urb_payload) > 1 and len(urb_payload) % 512 == 1:
            fw_chunk = fw_chunk[:-1]

        # A chunk smaller than the 4096 max that is NOT the last one marks a
        # section boundary (DMEM→IMEM).
        if 0 < prev_chunk_size < MAX_CHUNK_FW_BYTES:
            section_breaks.append((frame_no, len(out)))

        out.extend(fw_chunk)
        prev_chunk_size = len(fw_chunk)
        chunk_count += 1
        last_frame = frame_no
        if first_frame is None:
            first_frame = frame_no

    print(f"[+] Chunks: {chunk_count}, total FW bytes: {len(out)}")
    if first_frame is not None:
        print(f"[+] Frame range: {first_frame}..{last_frame}")
    prev_offset = 0
    for idx, (brk_frame, brk_offset) in enumerate(section_breaks):
        sec = brk_offset - prev_offset
        print(f"[+] Section #{idx + 1} ends before frame {brk_frame}: "
              f"{sec} bytes (={sec - FW_HDR_CHKSUM_SIZE} + "
              f"{FW_HDR_CHKSUM_SIZE}-byte chksum)")
        prev_offset = brk_offset
    if section_breaks:
        last_sec = len(out) - prev_offset
        print(f"[+] Final section: {last_sec} bytes "
              f"(={last_sec - FW_HDR_CHKSUM_SIZE} + "
              f"{FW_HDR_CHKSUM_SIZE}-byte chksum)")
    return bytes(out)


def verify_against(blob: bytes, reference_path: Path) -> bool:
    ref = reference_path.read_bytes()
    body = ref[FW_HDR_SIZE:]
    print(f"[*] Reference: {reference_path} ({len(ref)} bytes, "
          f"header={FW_HDR_SIZE}B -> body={len(body)}B)")
    if blob == body:
        print("[+] BYTE-FOR-BYTE MATCH against linux-firmware (header-stripped).")
        return True
    if len(blob) != len(body):
        print(f"[-] Size mismatch: extracted {len(blob)} vs body {len(body)}")
        common = min(len(blob), len(body))
        diffs = [i for i in range(common) if blob[i] != body[i]]
        print(f"[-] First diff at 0x{diffs[0]:x}" if diffs
              else "[-] One side is a prefix of the other.")
    else:
        diffs = [i for i, (a, b) in enumerate(zip(blob, body)) if a != b]
        print(f"[-] Length matches but {len(diffs)} bytes differ "
              f"(first at 0x{diffs[0]:x}).")
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pcap", type=Path, help="Path to USB capture .pcap")
    p.add_argument("output", type=Path, help="Path for extracted FW blob (.bin)")
    p.add_argument("--bus", type=int, default=None, help="usbmon bus filter")
    p.add_argument("--device", type=int, default=None, help="usbmon device filter")
    p.add_argument("--start-frame", type=int, default=1)
    p.add_argument("--end-frame", type=int, default=None)
    p.add_argument("--verify", type=Path, default=None,
                   help="Compare to linux-firmware rtw8814a_fw.bin "
                        "(skipping the 64-byte FW header)")
    args = p.parse_args()

    blob = extract(args.pcap, args.bus, args.device,
                   args.start_frame, args.end_frame)
    if not blob:
        print("[-] No firmware bytes extracted. Check filters.")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(blob)
    print(f"[+] Wrote {args.output} ({len(blob)} bytes)")

    if args.verify is not None:
        return 0 if verify_against(blob, args.verify) else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
