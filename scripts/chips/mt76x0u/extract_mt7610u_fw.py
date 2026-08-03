"""Extract MT7610U firmware blob from a USB capture (cleanroom).

The mt76x0u kernel driver uploads ONE firmware blob (no ROM patch) per
`mt76x0u_upload_firmware` in mt76x0/usb_mcu.c:17. Sequence:

  1. ILM body @ chip offset 0x40    (file bytes [hdr+IVB_SIZE..hdr+ilm_len])
  2. DLM body @ chip offset 0x80000 (file bytes [hdr+ilm_len..hdr+ilm_len+dlm_len])
  3. IVB trigger:
        bReq=0x01 (MT_VEND_DEV_MODE)
        wValue=0x0012
        payload=64 bytes (the IVB body — first 0x40 bytes of FW payload)
  4. FW_READY poll on MT_MCU_COM_REG0 BIT(0).

For each ILM/DLM chunk the kernel emits:
  vendor write (bReq=0x42 MT_VEND_WRITE_FCE):
      [wValue=val&0xFFFF, wIndex=0x0230, no payload]  (DMA_ADDR low)
      [wValue=val>>16,    wIndex=0x0232, no payload]  (DMA_ADDR high)
      [wValue=val&0xFFFF, wIndex=0x0234, no payload]  (DMA_LEN  low)
      [wValue=val>>16,    wIndex=0x0236, no payload]  (DMA_LEN  high)
  bulk OUT on out_ep[0]=0x08 (MT_EP_OUT_INBAND_CMD):
      [4B  mt76 info: PORT|LEN|TYPE_CMD]
      [N   chunk bytes]
      [4B  trailing zero pad]

The single FW reset (`bReq=0x01 wValue=0x0001`) is what STARTS the FW
upload — after that we're in the upload window. We capture the IVB
payload (the 64-byte data stage of the `wValue=0x0012` control transfer)
verbatim so we can byte-verify the reassembled file against
linux-firmware/mt76/mt7610u.bin.

Reassembled file layout:
    [32-byte mt76x02_fw_header][64-byte IVB][ILM body][DLM body]
                                ^^^^^^^^^^^^^^^^^^^^^
                                first 64B of "ILM section" per kernel split

Per driver_sources/mt76-source-v6.18/mt76x0/usb_mcu.c:29-44 the kernel
holds back the first MT_MCU_IVB_SIZE (0x40) bytes of the FW payload for
the IVB trigger and uploads the remainder of the ILM section to chip
offset 0x40. So when we see chunks uploaded to chip dst 0x40, 0x40+X,
0x40+2X, ... we're seeing the post-IVB ILM bytes — to reconstruct the
linux-firmware file we need to PREPEND the captured IVB.
"""
from __future__ import annotations
import argparse
import struct
import sys
from pathlib import Path

PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
PCAPNG_EPB = 0x00000006

MT_VEND_WRITE_FCE = 0x42
MT_VEND_DEV_MODE  = 0x01
MT_FCE_DMA_ADDR   = 0x0230
MT_FCE_DMA_LEN    = 0x0234
EP_OUT_INBAND_CMD = 0x08
MT_INFO_HDR_LEN   = 4
MT_INFO_TAIL_LEN  = 4
MT_MCU_IVB_SIZE   = 0x40
MT_MCU_DLM_OFFSET = 0x80000


def iter_pcapng(path: Path):
    """Yield (frame_no, usbmon_pseudo_header_bytes) for each EPB."""
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


def extract(pcap_path: Path, frame_limit: int):
    """Walk the cold-boot window and reassemble the FW.

    `frame_limit` caps the search (set to the pcap_slicer's end-of-
    <hardware_plugin_and_initialization> frame; nothing useful after
    that for FW extraction).
    """
    fw_records: list[tuple[int, bytes]] = []
    upload_started = False
    captured_ivb: bytes | None = None
    last_dst_lo = 0
    last_dst_hi = 0
    pending_dst = None
    first_chunk_frame = None
    last_chunk_frame = None
    fw_reset_frames: list[int] = []
    ivb_frame = None

    for frame_no, data in iter_pcapng(pcap_path):
        if frame_no > frame_limit:
            break
        if len(data) < 16:
            continue
        evt = chr(data[8])
        xfer = data[9]
        ep = data[10]

        # Submit only (Completion duplicates everything).
        if evt != "S":
            continue

        # Control transfer.
        if xfer == 2:
            if len(data) < 48:
                continue
            setup = data[40:48]
            bmReq, bReq = setup[0], setup[1]
            wVal = struct.unpack("<H", setup[2:4])[0]
            wIdx = struct.unpack("<H", setup[4:6])[0]

            if bmReq == 0x40 and bReq == MT_VEND_DEV_MODE and wVal == 0x0001:
                # mt76x02u_mcu_fw_reset — upload starts after this.
                fw_reset_frames.append(frame_no)
                upload_started = True
                continue

            if bmReq == 0x40 and bReq == MT_VEND_DEV_MODE and wVal == 0x0012:
                # mt76x0u IVB trigger — data stage carries the IVB body.
                ivb_frame = frame_no
                # libusb's URB submit-side payload follows the 64-byte
                # usbmon pseudo-header.
                ivb_payload = data[64:]
                # Trim to exactly the wLength value declared in setup.
                wLen = struct.unpack("<H", setup[6:8])[0]
                if wLen > 0:
                    captured_ivb = bytes(ivb_payload[:wLen])
                # IVB trigger marks end of upload sequence.
                break

            if bmReq == 0x40 and bReq == MT_VEND_WRITE_FCE and upload_started:
                if wIdx == MT_FCE_DMA_ADDR:
                    last_dst_lo = wVal
                elif wIdx == MT_FCE_DMA_ADDR + 2:
                    last_dst_hi = wVal
                    pending_dst = (last_dst_hi << 16) | last_dst_lo

        # Bulk-OUT chunk on out_ep[0]=0x08.
        elif xfer == 3 and ep == EP_OUT_INBAND_CMD and upload_started:
            urb_payload = data[64:]
            if len(urb_payload) <= MT_INFO_HDR_LEN + MT_INFO_TAIL_LEN:
                continue
            chunk = urb_payload[MT_INFO_HDR_LEN:-MT_INFO_TAIL_LEN]
            dst = pending_dst if pending_dst is not None else 0
            pending_dst = None
            fw_records.append((dst, chunk))
            if first_chunk_frame is None:
                first_chunk_frame = frame_no
            last_chunk_frame = frame_no

    # Split into ILM (dst < 0x80000) and DLM (dst >= 0x80000).
    ilm_records = [r for r in fw_records if r[0] < MT_MCU_DLM_OFFSET]
    dlm_records = [r for r in fw_records if r[0] >= MT_MCU_DLM_OFFSET]
    ilm_body = b"".join(c for _, c in ilm_records)
    dlm_body = b"".join(c for _, c in dlm_records)
    ilm_base = ilm_records[0][0] if ilm_records else 0
    dlm_base = dlm_records[0][0] if dlm_records else 0

    print("=== mt76x0u single-stage FW extraction ===")
    print(f"  FW reset frames     : {fw_reset_frames}")
    print(f"  IVB trigger frame   : {ivb_frame}")
    print(f"  chunk frame range   : {first_chunk_frame}..{last_chunk_frame}")
    print(f"  total chunks        : {len(fw_records)}")
    print(f"  ILM: {len(ilm_records)} chunks, {len(ilm_body)} bytes  "
          f"@ first dst 0x{ilm_base:08x}")
    print(f"  DLM: {len(dlm_records)} chunks, {len(dlm_body)} bytes  "
          f"@ first dst 0x{dlm_base:08x}")
    if captured_ivb is not None:
        print(f"  IVB payload         : {len(captured_ivb)} bytes "
              f"(expected {MT_MCU_IVB_SIZE})")
    else:
        print("  IVB payload         : <NOT CAPTURED>")

    return ilm_body, dlm_body, captured_ivb, ilm_base, dlm_base


def _verify(blob: bytes, reference: Path, name: str) -> int:
    ref = reference.read_bytes()
    # mt7610u.bin has a 32-byte mt76x02_fw_header. Try that first, then
    # other plausible offsets if it doesn't match.
    for hdr in (32, 0, 14, 64):
        if ref[hdr:] == blob:
            print(f"[+] {name}: BYTE-FOR-BYTE MATCH against "
                  f"{reference} (skipping {hdr}-byte header)")
            return 0
    # Closer diagnostic: show first divergence.
    body_with_default_hdr = ref[32:]
    print(f"[-] {name}: no match against {reference} for any header size "
          f"in (32, 0, 14, 64)")
    print(f"    extracted len = {len(blob)}, "
          f"reference len = {len(ref)} (body-after-32B-hdr = {len(body_with_default_hdr)})")
    if len(blob) == len(body_with_default_hdr):
        for i, (a, b) in enumerate(zip(blob, body_with_default_hdr)):
            if a != b:
                print(f"    first divergence @ byte {i}: "
                      f"extracted=0x{a:02x} vs reference=0x{b:02x}")
                break
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pcap", type=Path, help="Path to USB capture .pcap")
    p.add_argument("--frame-limit", type=int, default=777,
                   help="Stop scanning after this frame (default: 777 — "
                        "end of cold-boot window per pcap_slicer)")
    p.add_argument("--out-dir", type=Path,
                   default=Path("src/wifit3/chips/mt76x0u/assets"),
                   help="Output dir (default: src/wifit3/chips/mt76x0u/assets/)")
    p.add_argument("--verify-fw", type=Path, default=None,
                   help="linux-firmware mt7610u.bin to byte-verify against "
                        "(extracted body = IVB + ILM + DLM, reference is "
                        "ref[32:] after stripping mt76x02_fw_header)")
    args = p.parse_args()

    print(f"[*] Reading {args.pcap} (frame limit {args.frame_limit})")
    ilm_body, dlm_body, ivb, ilm_base, dlm_base = extract(
        args.pcap, args.frame_limit,
    )
    if not ilm_body or not dlm_body:
        print("[-] ILM or DLM body is empty. Check pcap content.")
        return 1
    if ivb is None or len(ivb) != MT_MCU_IVB_SIZE:
        print(f"[-] IVB capture missing or wrong size "
              f"(got {len(ivb) if ivb else 0}, expected {MT_MCU_IVB_SIZE}).")
        return 1

    # Reassemble the FW body in linux-firmware order:
    #   [IVB (first 0x40 bytes of ILM section)][ILM remainder][DLM]
    fw_body = ivb + ilm_body + dlm_body

    args.out_dir.mkdir(parents=True, exist_ok=True)
    body_path = args.out_dir / "mt7610u_pcap_body.bin"
    body_path.write_bytes(fw_body)
    print(f"[+] Wrote {body_path} ({len(fw_body)} bytes — "
          f"IVB={MT_MCU_IVB_SIZE} + ILM={len(ilm_body)} + DLM={len(dlm_body)})")

    rc = 0
    if args.verify_fw:
        rc |= _verify(fw_body, args.verify_fw, "main FW (mt7610u)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
