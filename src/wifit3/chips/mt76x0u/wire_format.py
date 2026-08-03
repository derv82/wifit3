"""Single source of truth for the mt76x0u wire-dump line format.

Used by both:
  - `scripts/chips/mt76x0u/mt76x0u_wire_dump.py` (extracts from a pcap)
  - `src/wifit3/chips/mt76x0u/wire_log.py`  (live in-driver logger)

Each `fmt_*` function returns exactly one line of text. Two outputs from
either source for an equivalent USB transaction must produce byte-identical
strings; this is what makes `diff -u kernel.wire.txt ours.wire.txt` work.

Don't add a leading `[f=N t=Xs]` prefix here — that's a wire_dump.py
display concern. The format here is the "stable identifier" for a
transaction; prefixes are added on top when needed for inspection.
"""
from __future__ import annotations

import struct


# ---- mt76 vendor request opcodes (mt76.h:618-629) -------------------------
VEND_NAMES = {
    0x01: "DEV_MODE",
    0x02: "WRITE",
    0x04: "POWER_ON",
    0x06: "MULTI_WRITE",
    0x07: "MULTI_READ",
    0x09: "READ_EEPROM",
    0x42: "WRITE_FCE",
    0x46: "WRITE_CFG",
    0x47: "READ_CFG",
    0x63: "READ_EXT",
    0x66: "WRITE_EXT",
    0x91: "FEATURE_SET",
}

DEV_MODE_NAMES = {
    0x01: "RESET",
    0x02: "CLEAR_FCE",
    0x04: "STARTING",
    0x08: "FW_LOADED",
    0x21: "READ_EEPROM_PARAM",
}


# ---- MCU command IDs (mt76x02_mcu.h CMD_*) --------------------------------
MCU_CMD_NAMES = {
    1:  "CMD_FUN_SET_OP",
    8:  "CMD_BURST_WRITE",
    10: "CMD_RANDOM_READ",
    12: "CMD_RANDOM_WRITE",
    13: "CMD_RANDOM_WRITE_alt",
    31: "CMD_CALIBRATION_OP",
}

FUN_NAMES = {
    1: "Q_SELECT",
    2: "BW_SETTING",
    6: "ATOMIC_TSSI_INFO",
}

CAL_NAMES = {
    1: "MCU_CAL_R",
    2: "MCU_CAL_RXDCOC",
    3: "MCU_CAL_LC",
    4: "MCU_CAL_LOFT",
    5: "MCU_CAL_TXIQ",
    6: "MCU_CAL_BW",
    7: "MCU_CAL_DPD",
    8: "MCU_CAL_RXIQ",
    9: "MCU_CAL_TXDCOC",
    10: "MCU_CAL_RX_GROUP_DELAY",
    11: "MCU_CAL_TX_GROUP_DELAY",
    12: "MCU_CAL_VCO",
    0xFE: "MCU_CAL_NO_SIGNAL",
    0xFF: "MCU_CAL_FULL",
}

EVT_NAMES = {
    0: "EVT_CMD_DONE",
    1: "EVT_CMD_ERROR",
    2: "EVT_CMD_RETRY",
    3: "EVT_CMD_TIMEOUT",
}

ADDR_BASES = {
    0x80000000: "RF",
    0x00410000: "MAC",
}

# MCU commands cap at ~2 KB payload; larger bulk OUT on EP 0x08 is a FW chunk.
MCU_CMD_MAX_LEN = 2048


# ---- Internal helpers -----------------------------------------------------

def _addr_to_name(wire_addr: int) -> str:
    """Factor a wire address back into `RF+0xNN` / `MAC+0xNN` form."""
    for base, name in ADDR_BASES.items():
        if base <= wire_addr < base + 0x100000:
            return f"{name}+0x{wire_addr - base:04x}"
    return f"0x{wire_addr:08x}"


def _summarize_mcu_payload(cmd: int, payload: bytes) -> str:
    if cmd == 1:  # CMD_FUN_SET_OP
        if len(payload) >= 8:
            func = struct.unpack_from("<I", payload, 0)[0]
            val = struct.unpack_from("<I", payload, 4)[0]
            return f"func={FUN_NAMES.get(func, func)} val=0x{val:x}"
    elif cmd in (12, 13):  # CMD_RANDOM_WRITE
        pairs = []
        for i in range(0, len(payload), 8):
            if i + 8 > len(payload):
                break
            addr, val = struct.unpack_from("<II", payload, i)
            pairs.append((addr, val))
        if len(pairs) == 1:
            a, v = pairs[0]
            return f"WRITE  {_addr_to_name(a)} = 0x{v:02x}"
        names = [_addr_to_name(a) for a, _ in pairs]
        head = ", ".join(names[:4])
        more = f"  (+{len(pairs)-4} more)" if len(pairs) > 4 else ""
        return f"WRITE [{len(pairs)} pairs] {head}{more}"
    elif cmd == 10:  # CMD_RANDOM_READ
        addrs = []
        for i in range(0, len(payload), 8):
            if i + 4 > len(payload):
                break
            addr = struct.unpack_from("<I", payload, i)[0]
            addrs.append(addr)
        names = [_addr_to_name(a) for a in addrs]
        head = ", ".join(names[:4])
        more = f"  (+{len(addrs)-4} more)" if len(addrs) > 4 else ""
        return f"READ [{len(addrs)} addr] {head}{more}"
    elif cmd == 31:  # CMD_CALIBRATION_OP
        if len(payload) >= 8:
            cal_type = struct.unpack_from("<I", payload, 0)[0]
            param = struct.unpack_from("<I", payload, 4)[0]
            return f"type={CAL_NAMES.get(cal_type, cal_type)} param=0x{param:x}"
    return f"({len(payload)}B raw)"


# ---- Public format API ----------------------------------------------------

def fmt_vendor(bRequest: int, is_in: bool, wValue: int, wIndex: int,
               wLength: int, data: bytes) -> str:
    """Format any vendor control transfer to one line."""
    name = VEND_NAMES.get(bRequest, f"req=0x{bRequest:02x}")
    direction = "IN " if is_in else "OUT"
    addr = wIndex

    # Register r/w via MULTI_WRITE/READ or WRITE_CFG/READ_CFG.
    if bRequest in (0x06, 0x07, 0x46, 0x47):
        if wLength == 4 and data and len(data) >= 4:
            val = struct.unpack_from("<I", data, 0)[0]
            arrow = "->" if is_in else "= "
            return f"VEND_{direction} {name:12s} 0x{addr:04x}  {arrow} 0x{val:08x}"
        if wLength > 4 and data:
            preview = data[:8].hex()
            return (f"VEND_{direction} {name:12s} 0x{addr:04x}  "
                    f"({wLength}B) {preview}...")
        return f"VEND_{direction} {name:12s} 0x{addr:04x}  ({wLength}B, no data)"

    # DEV_MODE: wValue = mode constant
    if bRequest == 0x01:
        mode = DEV_MODE_NAMES.get(wValue, f"mode=0x{wValue:x}")
        return f"VEND_{direction} {name:12s} {mode}  wIdx=0x{addr:04x}"

    # POWER_ON: bare command, no payload usually
    if bRequest == 0x04:
        return f"VEND_{direction} {name:12s} (power on)"

    # READ_EEPROM: wIndex = eeprom offset, len-byte payload
    if bRequest == 0x09:
        return f"VEND_{direction} {name:12s} 0x{addr:04x}  ({wLength}B)"

    # WRITE_FCE: wValue carries a u16, usually no payload
    if bRequest == 0x42:
        if wLength == 0:
            return f"VEND_{direction} {name:12s} wVal=0x{wValue:04x} wIdx=0x{addr:04x}"
        preview = data[:8].hex() if data else ""
        return (f"VEND_{direction} {name:12s} wVal=0x{wValue:04x} "
                f"wIdx=0x{addr:04x} ({wLength}B) {preview}")

    return (f"VEND_{direction} {name:12s} wVal=0x{wValue:04x} "
            f"wIdx=0x{addr:04x} wLen={wLength}")


def fmt_mcu_out(raw: bytes) -> str:
    """Decode a bulk OUT on EP 0x08. Distinguishes MCU commands from FW
    upload chunks by payload size."""
    if len(raw) < 4:
        return f"MCU_OUT  (truncated, {len(raw)}B)"
    info = struct.unpack_from("<I", raw, 0)[0]
    length = info & 0xFFFF
    if length > MCU_CMD_MAX_LEN or len(raw) > MCU_CMD_MAX_LEN + 16:
        return fmt_fw_chunk(len(raw))
    cmd = (info >> 20) & 0x7F
    seq = (info >> 16) & 0xF
    payload = raw[4:4 + length]
    cmd_name = MCU_CMD_NAMES.get(cmd, f"CMD_0x{cmd:02x}")
    summary = _summarize_mcu_payload(cmd, payload)
    return (f"MCU_OUT  {cmd_name:<22s} seq={seq:>2d} len={length:>3d}  "
            f"{summary}")


def fmt_mcu_in(raw: bytes) -> str:
    """Decode a bulk IN on EP 0x85 (MCU response)."""
    if len(raw) < 4:
        return f"MCU_IN   (truncated, {len(raw)}B)"
    rxfce = struct.unpack_from("<I", raw, 0)[0]
    pkt_len = rxfce & 0x3FFF
    seq = (rxfce >> 16) & 0xF
    evt = (rxfce >> 20) & 0xF
    payload = raw[4:4 + max(0, pkt_len - 4)]
    evt_name = EVT_NAMES.get(evt, f"EVT_{evt}")
    preview = payload[:16].hex(" ") if payload else ""
    return (f"MCU_IN   {evt_name:<14s} seq={seq:>2d} pkt_len={pkt_len:>3d}  "
            f"{preview}")


def fmt_fw_chunk(size: int) -> str:
    """Bulk OUT on EP 0x08 that's too large to be an MCU command."""
    return f"FW_CHUNK EP08  {size}B"
