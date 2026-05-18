"""Extract rtw8822b firmware from a USB capture (cleanroom).

The rtw88 kernel driver uploads 8822B firmware via USB bulk-OUT writes through
the device's normal TX data path (NOT via control transfers like 8821a's legacy
path). See `mac.c:start_download_firmware` + `mac.c:download_firmware_to_mem`:

- For each section (DMEM, IMEM, optionally EMEM) the driver iterates
  `send_firmware_pkt(pg_addr, data+offset, pkt_size)` where `pkt_size <= 4096`.
- `send_firmware_pkt` builds an skb with a 48-byte `tx_pkt_desc` header
  (`TX_DESC_SIZE = 48`, qsel=BEACON, offset=48, ls=1, tx_pkt_size=pkt_size)
  and writes it to the high-priority bulk-OUT endpoint (EP 0x05 on
  TP-Link T3U / 0bda:0811-style 3-OUT-pipe layout).
- After each `send_firmware_pkt` it issues `iddma_download_firmware`, which is
  three control writes to REG_DDMA_CH0SA / CH0DA / CH0CTRL plus a poll. Those
  are NOT bulk-OUT and don't affect FW reassembly.
- After each section completes, the driver checks the iDDMA checksum status
  bit (REG_MCUFW_CTRL bits IMEM/DMEM_CHKSUM_OK).

So to reconstruct the FW blob we just need to:
  1. Iterate every Submit bulk-OUT URB on EP 0x05 during the bring-up window.
  2. Strip the 48-byte tx_pkt_desc from each URB payload.
  3. Concatenate the remaining bytes in pcap order.

The output equals `linux-firmware/rtw88/rtw8822b_fw.bin[FW_HDR_SIZE:]` where
`FW_HDR_SIZE = 64` (modern struct rtw_fw_hdr, NOT the 32-byte legacy header).
The 64-byte FW header is read by the driver before upload and never appears
on the wire.

We parse the pcap (pcapng or legacy) directly. The usbmon URB header is at
offset 0..63 of every pcap record; payload starts at offset 64.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

TX_DESC_SIZE = 48
FW_HDR_SIZE = 64
FW_HDR_CHKSUM_SIZE = 8
MAX_CHUNK_FW_BYTES = 0x1000  # 4096

# Endpoint address for the 8822bu HIGH-priority bulk-OUT (BEACON/H2C qsel).
# On the T3U (3-OUT-pipe layout: 0x05, 0x06, 0x08) this is out_ep[0] = 0x05.
EP_FW_UPLOAD = 0x05

PCAPNG_SHB = 0x0A0D0D0A
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
    block_total_len_raw = f.read(4)
    if len(block_total_len_raw) < 4:
        return
    byte_order_magic_raw = f.read(4)
    if len(byte_order_magic_raw) < 4:
        return
    if struct.unpack("<I", byte_order_magic_raw)[0] == PCAPNG_BYTE_ORDER_MAGIC:
        endian = "<"
    elif struct.unpack(">I", byte_order_magic_raw)[0] == PCAPNG_BYTE_ORDER_MAGIC:
        endian = ">"
    else:
        raise ValueError("pcapng byte-order magic not recognized")
    block_total_len = struct.unpack(f"{endian}I", block_total_len_raw)[0]
    remaining = block_total_len - 12
    if remaining > 0:
        f.read(remaining)

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
        body_len = block_len - 12
        body = f.read(body_len)
        trailer = f.read(4)
        if len(trailer) < 4:
            return
        if block_type != PCAPNG_EPB:
            continue
        if len(body) < 20:
            continue
        cap_len = struct.unpack(f"{endian}I", body[12:16])[0]
        if cap_len < 64:
            continue
        data = body[20:20 + cap_len]
        frame_no += 1
        if len(data) < 64:
            continue
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
    if bus is not None or device is not None:
        print(f"[*] Filtering bus={bus} device={device}")
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
        # Submit ('S') Bulk (3) OUT (epnum high bit clear)
        if data[8] != ord("S"):
            continue
        if data[9] != 3:
            continue
        epnum = data[10]
        if epnum & 0x80:
            continue
        if (epnum & 0x7F) != EP_FW_UPLOAD:
            continue
        urb_devnum = data[11]
        urb_busnum = struct.unpack("<H", data[12:14])[0]
        if bus is not None and urb_busnum != bus:
            continue
        if device is not None and urb_devnum != device:
            continue

        # usbmon URB header is 64 bytes; payload begins at offset 64.
        # Each FW upload URB = 48-byte tx_pkt_desc + chunk bytes.
        if len(data) < 64 + TX_DESC_SIZE:
            continue
        urb_payload = data[64:]
        if len(urb_payload) <= TX_DESC_SIZE:
            continue

        fw_chunk = urb_payload[TX_DESC_SIZE:]

        # Undo `send_firmware_pkt` ZLP-avoidance: when (pkt_size + TX_DESC_SIZE)
        # would be a multiple of 512, the kernel appends one extra byte BEFORE
        # building the TX descriptor (mac.c:send_firmware_pkt). On the wire that
        # turns into `URB_LEN % 512 == 1`. Trim the trailing byte so the
        # reassembled blob is byte-identical to the on-disk FW file.
        if len(urb_payload) > 1 and len(urb_payload) % 512 == 1:
            fw_chunk = fw_chunk[:-1]

        # Detect section boundary: a chunk smaller than MAX_CHUNK_FW_BYTES that is
        # NOT the last chunk means a section just ended.
        if 0 < prev_chunk_size < MAX_CHUNK_FW_BYTES:
            # The PREVIOUS chunk was a section-final chunk.
            section_breaks.append((frame_no, len(out)))

        out.extend(fw_chunk)
        prev_chunk_size = len(fw_chunk)
        chunk_count += 1
        last_frame = frame_no
        if first_frame is None:
            first_frame = frame_no

    print(f"[+] Chunks: {chunk_count}, total bytes uploaded: {len(out)}")
    if first_frame is not None and last_frame is not None:
        print(f"[+] Frame range: {first_frame}..{last_frame}")
    if section_breaks:
        prev_offset = 0
        for idx, (brk_frame, brk_offset) in enumerate(section_breaks):
            sec_size = brk_offset - prev_offset
            sec_size_hdr = sec_size - FW_HDR_CHKSUM_SIZE
            print(f"[+] Section #{idx + 1} ends before frame {brk_frame}: "
                  f"{sec_size} bytes uploaded "
                  f"(={sec_size_hdr} + {FW_HDR_CHKSUM_SIZE}-byte chksum)")
            prev_offset = brk_offset
        last_sec_size = len(out) - prev_offset
        last_sec_hdr = last_sec_size - FW_HDR_CHKSUM_SIZE
        print(f"[+] Final section: {last_sec_size} bytes uploaded "
              f"(={last_sec_hdr} + {FW_HDR_CHKSUM_SIZE}-byte chksum)")
    return bytes(out)


def verify_against(blob: bytes, reference_path: Path) -> bool:
    ref = reference_path.read_bytes()
    body = ref[FW_HDR_SIZE:]
    print(f"[*] Reference: {reference_path} ({len(ref)} bytes, "
          f"header={FW_HDR_SIZE}B)")
    print(f"[*] Reference body length: {len(body)}, extracted length: {len(blob)}")

    if blob == body:
        print("[+] BYTE-FOR-BYTE MATCH against linux-firmware "
              "(header-stripped body).")
        return True

    if len(blob) != len(body):
        print(f"[-] Size mismatch: extracted {len(blob)} vs reference body {len(body)}")
        common = min(len(blob), len(body))
        diffs = [i for i in range(common) if blob[i] != body[i]]
        if diffs:
            print(f"[-] First diff within common range at offset 0x{diffs[0]:x}: "
                  f"extracted=0x{blob[diffs[0]]:02x} "
                  f"reference=0x{body[diffs[0]]:02x}")
        else:
            print("[-] Common prefix matches; one side is a prefix of the other.")
    else:
        diffs = [i for i, (a, b) in enumerate(zip(blob, body)) if a != b]
        print(f"[-] Length matches but {len(diffs)} bytes differ.")
        if diffs:
            print(f"    First diff at offset 0x{diffs[0]:x}: "
                  f"extracted=0x{blob[diffs[0]]:02x} "
                  f"reference=0x{body[diffs[0]]:02x}")
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pcap", type=Path, help="Path to USB capture .pcap")
    p.add_argument("output", type=Path, help="Path for extracted FW blob (.bin)")
    p.add_argument("--bus", type=int, default=None, help="usbmon bus filter")
    p.add_argument("--device", type=int, default=None, help="usbmon device filter")
    p.add_argument("--start-frame", type=int, default=1,
                   help="Begin scanning at frame N (inclusive)")
    p.add_argument("--end-frame", type=int, default=None,
                   help="Stop scanning after frame N (inclusive)")
    p.add_argument("--verify", type=Path, default=None,
                   help="Compare to linux-firmware rtw8822b_fw.bin "
                        "(skipping the 64-byte FW header)")
    args = p.parse_args()

    blob = extract(args.pcap, args.bus, args.device,
                   args.start_frame, args.end_frame)
    if not blob:
        print("[-] No firmware bytes extracted. "
              "Check --bus/--device/--start-frame/--end-frame filters.")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(blob)
    print(f"[+] Wrote {args.output} ({len(blob)} bytes)")

    if args.verify is not None:
        ok = verify_against(blob, args.verify)
        return 0 if ok else 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
