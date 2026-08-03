"""Extract rtw8821a firmware from a USB capture (cleanroom).

The rtw88 kernel driver uploads 8821A firmware via USB control transfers:

    bmRequestType = 0x40  (Vendor, Host->Device)
    bRequest      = 0x05  (RTW_USB_CMD_REQ)
    wValue        = target address (16-bit), starting at FW_START_ADDR_LEGACY = 0x1000
    wIndex        = 0x00  (RTW_USB_VENQT_CMD_IDX)
    data          = up to 196 bytes (then 8, then 1) of raw firmware

Each "page" is 4096 bytes (DLFW_PAGE_SIZE_LEGACY). After a page completes, the
driver writes the next page index into BIT_ROM_PGE of REG_MCUFW_CTRL (0x0080)
and starts the next page from wValue = 0x1000 again. We detect that wrap and
keep concatenating in order, so the output is a flat blob of all FW bytes.

The original FW file in linux-firmware has a 32-byte rtw_fw_hdr_legacy prefix
that the driver STRIPS before upload. So:

    extracted_blob == linux_firmware/rtw88/rtw8821a_fw.bin[32:]

Use --verify <path> to assert that equality.

We parse the pcap (pcapng or legacy) directly because pyshark's USB
dissector does not expose setup-packet fields for usbmon captures. The
usbmon URB header places setup at offset 40 and payload at offset 64 of
every record, regardless of pcapng vs legacy container.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

FW_START_ADDR_LEGACY = 0x1000
FW_PAGE_SIZE = 0x1000  # 4096 bytes
FW_HDR_LEGACY_SIZE = 32

USB_CMD_REQ = 0x05
USB_REQTYPE_VENDOR_OUT = 0x40

# Each chunk length the kernel uses (rtw_usb_write_firmware_page)
CHUNK_OK = {1, 8, 196}

# usbmon URB record layout (LINKTYPE_USB_LINUX, basic header)
#  off  size  field
#  0    8     id
#  8    1    event_type ('S' submit, 'C' complete, 'E' error)
#  9    1    xfer_type (0=iso 1=intr 2=ctrl 3=bulk)
# 10    1    epnum (high bit = direction; 0x00 = OUT EP0 control)
# 11    1    devnum
# 12    2    busnum (LE)
# 14    1    setup_flag ('\0' = setup present)
# 15    1    data_flag ('\0' = data present)
# 16    8    ts (sec/usec)
# 24    4    status
# 28    4    urb_len
# 32    4    data_len
# 40    8    setup packet: bmRequestType, bRequest, wValue(LE16), wIndex(LE16), wLength(LE16)
# 48   16    error_count/numdesc/interval/start_frame/xfer_flags/ndesc
# 64   ...   payload (URB transfer data for control-OUT submit)


PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_EPB = 0x00000006
PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D


def _iter_pcap_legacy(f, endian: str):
    """Yield raw packet bytes from a legacy pcap stream (header already read)."""
    frame_no = 0
    while True:
        pkt_hdr = f.read(16)
        if not pkt_hdr or len(pkt_hdr) < 16:
            return
        _, _, incl_len, _ = struct.unpack(f"{endian}IIII", pkt_hdr)
        data = f.read(incl_len)
        frame_no += 1
        if len(data) < 48:
            continue
        yield frame_no, data


def _iter_pcapng(f):
    """Yield raw packet bytes from a pcapng stream (SHB magic already consumed).

    Only Enhanced Packet Blocks (type 0x06) are emitted; other block types
    (IDBs, Interface Stats, etc.) are skipped.
    """
    # We've consumed the first 4 bytes (block_type 0x0A0D0D0A). Read the rest
    # of the Section Header Block to detect endianness.
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
    # Skip the rest of the SHB (we've already read 12 bytes: type+len+magic).
    # The block ends with another 4-byte block_total_length trailer.
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
        body_len = block_len - 12  # subtract type(4) + len(4) + trailing len(4)
        body = f.read(body_len)
        trailer = f.read(4)  # block_total_length again
        if len(trailer) < 4:
            return

        if block_type != PCAPNG_EPB:
            continue

        # EPB body: interface_id(4) ts_high(4) ts_low(4) cap_len(4) orig_len(4) data
        if len(body) < 20:
            continue
        cap_len = struct.unpack(f"{endian}I", body[12:16])[0]
        if cap_len < 48:
            continue
        # Account for 4-byte padding of the packet data
        data = body[20:20 + cap_len]
        frame_no += 1
        if len(data) < 48:
            continue
        yield frame_no, data


def _iter_urbs(pcap_path: Path):
    """Yield (frame_number, packet_bytes) for every record in the capture."""
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

        # Legacy pcap: consume rest of global header (20 more bytes)
        f.read(20)
        yield from _iter_pcap_legacy(f, endian)


def extract(pcap_path: Path, bus: int | None, device: int | None) -> bytes:
    print(f"[*] Reading: {pcap_path}")
    if bus is not None or device is not None:
        print(f"[*] Filtering bus={bus} device={device}")

    out = bytearray()
    addr_expected: int | None = None
    page_count = 0
    chunk_count = 0
    first_frame: int | None = None
    last_frame: int | None = None
    unexpected_lens: dict[int, int] = {}

    for frame_no, data in _iter_urbs(pcap_path):
        # Must be a Submit ('S') Control (2) OUT (epnum high bit clear) URB
        if data[8] != ord("S"):
            continue
        if data[9] != 2:
            continue
        if data[10] & 0x80:
            continue

        # Bus/device filter
        urb_devnum = data[11]
        urb_busnum = struct.unpack("<H", data[12:14])[0]
        if bus is not None and urb_busnum != bus:
            continue
        if device is not None and urb_devnum != device:
            continue

        # Setup packet at offset 40
        bm_req_type = data[40]
        b_request = data[41]
        w_value = struct.unpack("<H", data[42:44])[0]
        # wIndex = data[44:46]; wLength = data[46:48]

        if b_request != USB_CMD_REQ or bm_req_type != USB_REQTYPE_VENDOR_OUT:
            continue
        if not (FW_START_ADDR_LEGACY <= w_value < FW_START_ADDR_LEGACY + FW_PAGE_SIZE):
            continue

        # Payload begins at offset 64
        payload = data[64:]
        n = len(payload)
        if n == 0:
            continue
        if n not in CHUNK_OK:
            unexpected_lens[n] = unexpected_lens.get(n, 0) + 1
            continue

        # State machine: lock onto first FW chunk, then track contiguity
        if addr_expected is None:
            if w_value != FW_START_ADDR_LEGACY or n != 196:
                continue
            addr_expected = FW_START_ADDR_LEGACY
            page_count = 1
            first_frame = frame_no
            print(f"[+] FW stream begins at frame {frame_no}")

        if w_value == addr_expected:
            out.extend(payload)
            addr_expected += n
            chunk_count += 1
            last_frame = frame_no
        elif (
            w_value == FW_START_ADDR_LEGACY
            and (addr_expected - FW_START_ADDR_LEGACY) >= FW_PAGE_SIZE - 196
        ):
            # Page wrap. (Driver wrote BIT_ROM_PGE to REG_MCUFW_CTRL between pages,
            # which our wValue filter excluded.) The previous page should be near
            # its 4096-byte boundary; a clean wrap can land at exactly 4096 or a few
            # 8-byte tail chunks short — be permissive.
            page_count += 1
            out.extend(payload)
            addr_expected = FW_START_ADDR_LEGACY + n
            chunk_count += 1
            last_frame = frame_no
        else:
            print(
                f"[!] Sequence broke at frame {frame_no}: "
                f"wValue=0x{w_value:04x} expected=0x{addr_expected:04x}; stopping."
            )
            break

    if unexpected_lens:
        print(f"[!] Ignored chunks with unexpected lengths: {unexpected_lens}")

    print(f"[+] Pages: {page_count}, chunks: {chunk_count}, total bytes: {len(out)}")
    if first_frame is not None and last_frame is not None:
        print(f"[+] Frame range: {first_frame}..{last_frame}")

    return bytes(out)


def verify_against(blob: bytes, reference_path: Path) -> bool:
    ref = reference_path.read_bytes()
    body = ref[FW_HDR_LEGACY_SIZE:]
    print(f"[*] Reference: {reference_path} ({len(ref)} bytes, header={FW_HDR_LEGACY_SIZE}B)")
    print(f"[*] Reference body length: {len(body)}, extracted length: {len(blob)}")

    if blob == body:
        print("[+] BYTE-FOR-BYTE MATCH against linux-firmware (header-stripped body).")
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
    p.add_argument("--bus", type=int, default=3, help="usbmon bus filter (default: 3)")
    p.add_argument("--device", type=int, default=21, help="usbmon device filter (default: 21)")
    p.add_argument("--verify", type=Path, default=None,
                   help="Compare to linux-firmware rtw8821a_fw.bin (offset 32 onwards)")
    args = p.parse_args()

    blob = extract(args.pcap, args.bus, args.device)
    if not blob:
        print("[-] No firmware bytes extracted. Check --bus/--device filters and pcap.")
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
