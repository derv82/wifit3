"""
Decode the POST-boot MCU command stream (everything on EP 0x08 after the
FW_START boot signal) from a scatter-disabled cold-boot capture.

This is the RE map for the RX-path port: the kernel's mt7921u device
registration + airmon monitor entry + channel hop, as a sequence of MCU
commands. For each EP-0x08 OUT frame after FW_START it prints the connac2 cid /
ext_cid / set_query / seq / payload, classifies UNI vs non-UNI txd by the
txd length implied by the SDIO header vs mcu len, and pairs the device response
on EP 0x84.

Usage: uv run python scripts/mt7921au/decode_postboot.py [<pcap>] [--full]
"""
import struct
import sys
from pathlib import Path

DEFAULT_CAP = "usb_dumps_new/captures_mt7921u_pau0f-no-adapter-scatter/capture-3.pcap"
PREFETCH0 = 0x7C024600
REG_BREQ = {0x63, 0x66, 0x01, 0x02}
EP_MCU_OUT, EP_FW_OUT, EP_IN_RX, EP_IN_CMD = 0x08, 0x04, 0x84, 0x85

# connac mcu command field decode helpers (mt76_connac_mcu.h).
# We only see cid(8) + ext_cid(8) on the wire; the UNI/CE/WA prefix bits live in
# the host-side `cmd` int and are NOT on the wire. We infer UNI from txd length.
EXT_CID_NAMES = {
    0x01: "EFUSE_ACCESS", 0x6e: "EFUSE_FREE_BLOCK", 0x3a: "MULTIPLE_REG_ACCESS",
    0x2a: "PM_STATE_CTRL", 0x07: "CHANNEL_SWITCH", 0x58: "PWR_LIMIT?",
    0x84: "RXFILTER?", 0x80: "WTBL_UPDATE", 0x07: "CHANNEL_SWITCH",
}
CID_NAMES = {
    0x01: "TARGET_ADDR_LEN", 0x02: "FW_START", 0x10: "PATCH_SEM",
    0xe0: "EXT_CID", 0x6f: "UNI? / mt76_connac",
}


def parse_pcapng(path):
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def detect_dev(pkts):
    for pkt in pkts:
        if len(pkt) < 48 or pkt[9] != 0x02 or pkt[8] != 0x53 or pkt[41] not in REG_BREQ:
            continue
        wValue, wIndex = struct.unpack_from("<HH", pkt, 42)
        if ((wValue << 16) | wIndex) == PREFETCH0:
            return pkt[11]
    return None


def bulk_events(pkts, dev):
    evs = []
    for pkt in pkts:
        if len(pkt) < 64 or pkt[11] != dev or pkt[9] != 0x03:
            continue
        ev, ep = pkt[8], pkt[10]
        lencap = struct.unpack_from("<I", pkt, 36)[0]
        data = pkt[64:64 + min(lencap, len(pkt) - 64)]
        if ev == 0x53 and not (ep & 0x80):
            evs.append(("OUT", ep, data))
        elif ev == 0x43 and (ep & 0x80) and lencap > 0:
            evs.append(("IN", ep & 0x7f, data))
    return evs


def decode_cmd(frame):
    """Return dict of decoded mcu command fields.

    Both txd shapes start with __le32 txd[8] (32 B), so sdio-mcu_len can't tell
    them apart. The discriminator is offset 38-39: the non-UNI mcu_txd carries
    pq_id = MCU_PQ_ID = 0x8000 there (byte39=0x80); the UNI uni_txd carries the
    16-bit cid there (small -> byte39=0x00)."""
    sdio = struct.unpack_from("<I", frame, 0)[0] & 0xFFFF   # = skb->len = txd_len + payload
    mcu_len = struct.unpack_from("<H", frame, 36)[0]        # txd.len = skb->len - 32
    is_std = (frame[38] == 0x00 and frame[39] == 0x80)      # pq_id == 0x8000
    if not is_std:
        kind = "UNI"
        cid = struct.unpack_from("<H", frame, 38)[0]
        ext_cid = 0
        seq = frame[43]
        s2d = frame[46]
        option = frame[47]
        sq = (option >> 2) & 1   # UNI_CMD_OPT_BIT_SET_QUERY
        payload = frame[4 + 48: 4 + 48 + (sdio - 48)]
    else:  # 64-byte mcu_txd (CE / EXT / plain)
        kind = "STD"
        cid = frame[40]
        seq = frame[43]
        sq = frame[42]
        ext_cid = frame[45]
        s2d = frame[46]
        option = frame[47]   # ext_cid_ack
        payload = frame[4 + 64: 4 + 64 + (sdio - 64)]
    return {"kind": kind, "sdio": sdio, "mcu_len": mcu_len,
            "cid": cid, "ext_cid": ext_cid, "set_query": sq, "seq": seq,
            "s2d": s2d, "option": option, "payload": bytes(payload)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cap = args[0] if args else DEFAULT_CAP
    full = "--full" in sys.argv
    pkts = parse_pcapng(cap)
    dev = detect_dev(pkts)
    evs = bulk_events(pkts, dev)
    print(f"{cap}: dev {dev}, {len(evs)} bulk events")

    # Find FW_START_REQ (STD txd, cid 0x02) and split there.
    fw_idx = None
    for i, (kind, ep, data) in enumerate(evs):
        if kind == "OUT" and ep == EP_MCU_OUT and len(data) >= 48:
            d = decode_cmd(data)
            if d["kind"] == "STD" and d["cid"] == 0x02:
                fw_idx = i
                break
    print(f"FW_START at bulk event {fw_idx}\n")

    # Walk post-boot, pairing each EP-0x08 cmd with the next EP-0x84 IN response.
    n = 0
    pending = None
    counts = {}
    for kind, ep, data in evs[fw_idx + 1:]:
        if kind == "OUT" and ep == EP_MCU_OUT and len(data) >= 44:
            if pending:
                _emit(pending, None, n - 1, full)
            d = decode_cmd(data)
            pending = d
            key = (d["kind"], d["cid"], d["ext_cid"])
            counts[key] = counts.get(key, 0) + 1
            n += 1
        elif kind == "OUT" and ep == EP_FW_OUT:
            pass  # TX bulk / scatter - ignore in command map
        elif kind == "IN" and ep in (EP_IN_RX, EP_IN_CMD) and pending is not None:
            # First IN after a command: treat as its response if it's an MCU rxd
            # (eid present, small-ish). 802.11 RX frames also land on 0x84; an MCU
            # response has rxd seq matching our cmd seq.
            if len(data) > 29 and data[29] == pending["seq"] and len(data) < 512:
                _emit(pending, data, n - 1, full)
                pending = None
    if pending:
        _emit(pending, None, n - 1, full)

    print(f"\n=== {n} post-boot MCU commands ===")
    print("by (kind, cid, ext_cid):")
    for k in sorted(counts, key=lambda k: -counts[k]):
        kind, cid, ext = k
        print(f"  {kind}  cid=0x{cid:02x}  ext=0x{ext:02x}   x{counts[k]}")
    return 0


def _emit(d, resp, idx, full):
    rdesc = ""
    if resp is not None and len(resp) > 32:
        rdesc = f"  -> resp eid=0x{resp[28]:02x} ext_eid=0x{resp[32]:02x} seq=0x{resp[29]:02x} ({len(resp)}B)"
    elif resp is None:
        rdesc = "  -> (no seq-matched resp)"
    pl = d["payload"]
    plprev = pl[:24].hex()
    print(f"  #{idx:<3} {d['kind']}  cid=0x{d['cid']:02x} ext=0x{d['ext_cid']:02x} "
          f"sq={d['set_query']} seq=0x{d['seq']:02x} s2d={d['s2d']} opt=0x{d['option']:02x} "
          f"plen={len(pl):<4} pl[{plprev}]{rdesc}")
    if full and len(pl) > 24:
        print(f"        full payload: {pl.hex()}")


if __name__ == "__main__":
    sys.exit(main())
