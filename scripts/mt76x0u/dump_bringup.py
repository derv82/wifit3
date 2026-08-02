"""Dump the mt76x0u bring-up window as a register-named transcript.

Walks capture-2.pcap (the actual cold-boot — capture-1 is warm; see
find_fw_window.py output) and prints each Submit-side USB transfer in
the bring-up window, decoded into kernel-symbol names so we can diff
wire-vs-source directly.

Usage:
    uv run python scripts/mt76x0u/dump_bringup.py [first_frame] [last_frame]

Default range is 1..500 — the FW reset is at frame 289 and the IVB
trigger is at frame 393, so 1..500 captures the pre-reset init, the
reset itself, the FCE config writes, all 6 FW chunks, the IVB trigger,
and the FW_READY poll.
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

PCAPNG_EPB = 0x00000006

# Per mt76x02_regs.h / mt76x02_mcu.h / mt76x0/mcu.h.
REG_NAMES = {
    0x0230: "MT_FCE_DMA_ADDR",
    0x0234: "MT_FCE_DMA_LEN",
    0x0238: "MT_USB_DMA_CFG",
    0x0730: "MT_MCU_COM_REG0  (= MT_COM_REG0; FW_READY = BIT(0))",
    0x0800: "MT_FCE_PSE_CTRL",
    0x09a0: "MT_TX_CPU_FROM_FCE_BASE_PTR",
    0x09a4: "MT_TX_CPU_FROM_FCE_MAX_COUNT",
    0x09a8: "MT_TX_CPU_FROM_FCE_CPU_DESC_IDX",
    0x09c4: "MT_FCE_PDMA_GLOBAL_CONF",
    0x0a6c: "MT_FCE_SKIP_FS",
    0x1004: "(undocumented init; kernel writes 0x2c here)",
}

# bRequest -> name. Per driver_sources/mt76-source-v6.18/mt76x02_usb.h-ish.
BREQ_NAMES = {
    0x01: "MT_VEND_DEV_MODE",
    0x06: "MT_VEND_MULTI_WRITE",
    0x07: "MT_VEND_MULTI_READ",
    0x42: "MT_VEND_WRITE_FCE",
    0x46: "MT_VEND_WRITE_CFG",
    0x47: "MT_VEND_READ_CFG",
    0x09: "MT_VEND_READ_EEPROM",
}

# DEV_MODE wValue meanings.
DEV_MODE_VALS = {
    0x0001: "FW_RESET",
    0x0012: "IVB_TRIGGER",
}


def iter_pcapng(path):
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


def decode_addr(wVal, wIdx):
    """For non-FCE vendor writes/reads, the mt76 encoding is
       wValue = addr >> 16, wIndex = addr & 0xFFFF."""
    return (wVal << 16) | wIdx


def fmt_reg(addr):
    nm = REG_NAMES.get(addr)
    return f"0x{addr:04x} {nm}" if nm else f"0x{addr:04x}"


def fmt_breq(bReq):
    nm = BREQ_NAMES.get(bReq, f"<unknown 0x{bReq:02x}>")
    return f"0x{bReq:02x} {nm}"


def dump_window(pcap_path, first_frame, last_frame):
    chunk_num = 0
    pending_chunk_dst = None
    last_dst_lo = 0
    last_dst_hi = 0
    last_len_lo = 0
    last_len_hi = 0

    for frame_no, data in iter_pcapng(pcap_path):
        if frame_no < first_frame:
            continue
        if frame_no > last_frame:
            break
        if len(data) < 16:
            continue
        evt = chr(data[8])
        xfer = data[9]
        ep = data[10]
        if evt != "S":
            continue

        # Control transfer.
        if xfer == 2 and len(data) >= 48:
            setup = data[40:48]
            bmReq, bReq = setup[0], setup[1]
            wVal = struct.unpack("<H", setup[2:4])[0]
            wIdx = struct.unpack("<H", setup[4:6])[0]
            wLen = struct.unpack("<H", setup[6:8])[0]
            direction = "OUT" if (bmReq & 0x80) == 0 else "IN "

            # Standard requests
            if bmReq < 0x40 or bmReq == 0x80 or bmReq == 0x81:
                print(f"f{frame_no:5d}  CTRL {direction}  STANDARD  "
                      f"bmReq=0x{bmReq:02x} bReq=0x{bReq:02x} "
                      f"wVal=0x{wVal:04x} wIdx=0x{wIdx:04x} wLen=0x{wLen:04x}")
                continue

            line = (f"f{frame_no:5d}  CTRL {direction}  VENDOR    "
                    f"{fmt_breq(bReq):28s}")

            # MT_VEND_DEV_MODE — control plane.
            if bReq == 0x01:
                tag = DEV_MODE_VALS.get(wVal, "")
                payload = ""
                if wVal == 0x0012 and len(data) >= 64 + wLen:
                    body = bytes(data[64:64 + wLen])
                    payload = f"  payload[{len(body)}]= {body[:16].hex()}..."
                print(f"{line}  wVal=0x{wVal:04x} {tag:14s} wLen=0x{wLen:04x}{payload}")
                continue

            # MT_VEND_WRITE_FCE — single_wr to 0x0230..0x0237 (ADDR low/high) or
            # 0x0234..0x0237 (LEN low/high). Value is encoded in wValue.
            if bReq == 0x42:
                if wIdx == 0x0230:
                    last_dst_lo = wVal
                    role = "DMA_ADDR LO"
                elif wIdx == 0x0232:
                    last_dst_hi = wVal
                    pending_chunk_dst = (last_dst_hi << 16) | last_dst_lo
                    role = f"DMA_ADDR HI  -> dst=0x{pending_chunk_dst:08x}"
                elif wIdx == 0x0234:
                    last_len_lo = wVal
                    role = "DMA_LEN  LO"
                elif wIdx == 0x0236:
                    last_len_hi = wVal
                    full_len = (last_len_hi << 16) | last_len_lo
                    role = f"DMA_LEN  HI  -> raw=0x{full_len:08x} (chunk_len={full_len >> 16})"
                else:
                    role = f"FCE single_wr to 0x{wIdx:04x}"
                print(f"{line}  wVal=0x{wVal:04x} wIdx=0x{wIdx:04x}  {role}")
                continue

            # MULTI_READ / MULTI_WRITE — 4-byte register at (wVal<<16)|wIdx
            if bReq in (0x06, 0x07):
                addr = decode_addr(wVal, wIdx)
                role = fmt_reg(addr)
                payload = ""
                if bReq == 0x06 and len(data) >= 64 + wLen and wLen >= 4:
                    val = struct.unpack("<I", bytes(data[64:68]))[0]
                    payload = f"  val=0x{val:08x}"
                print(f"{line}  addr={role:50s} wLen=0x{wLen:04x}{payload}")
                continue

            print(f"{line}  wVal=0x{wVal:04x} wIdx=0x{wIdx:04x} wLen=0x{wLen:04x}  (decode TBD)")
            continue

        # Bulk transfer.
        if xfer == 3:
            payload_len = len(data) - 64
            dir_label = "IN " if (ep & 0x80) else "OUT"
            extra = ""
            if ep == 0x08 and payload_len > 0:
                chunk_num += 1
                extra = f"  FW-chunk #{chunk_num}"
                if pending_chunk_dst is not None:
                    extra += f" dst=0x{pending_chunk_dst:08x}"
                pending_chunk_dst = None
                # Decode the 4-byte mt76 info header.
                if payload_len >= 4:
                    info = struct.unpack("<I", bytes(data[64:68]))[0]
                    msg_len_field = info & 0xFFFF
                    msg_type_cmd = (info >> 30) & 1
                    msg_port = (info >> 27) & 0x7
                    extra += (f"  info=0x{info:08x} "
                              f"(port={msg_port}, len={msg_len_field}, type_cmd={msg_type_cmd})")
            print(f"f{frame_no:5d}  BULK {dir_label}  ep=0x{ep:02x}  bytes={payload_len}{extra}")


def main():
    pcap = Path("driver_captures/captures_mt76x0u/capture-2.pcap")
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    last = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    print(f"=== mt76x0u bring-up transcript: {pcap.name} frames {first}..{last} ===\n")
    dump_window(pcap, first, last)


if __name__ == "__main__":
    main()
