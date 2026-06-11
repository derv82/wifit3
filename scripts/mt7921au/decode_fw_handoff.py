"""
Decode the firmware-load handshake around FW_START from a usbmon pcapng, using
the now-visible device->host RX (captured only with disable_usb_sg=1).

For the mt7921 device it builds an ordered event timeline:
  - EP 0x08 OUT  : MCU command (decodes connac2 cid + seq + len)
  - EP 0x04 OUT  : FW_SCATTER / TX bulk
  - EP 0x84 IN   : completion — the MCU responses + the firmware-up signal we
                   could never see before. Dumps eid/seq + leading bytes.
Then it prints the window around FW_START_REQ (cid=0x02) so we can read exactly
what the chip sends back at the handoff that wedges our userland driver.

Usage: uv run python scripts/mt7921au/decode_fw_handoff.py <pcap> <devnum>
"""
import struct
import sys
from pathlib import Path

FW_SCATTER_CID = 0xEE   # MCU_CMD(FW_SCATTER) low byte — not used to match, FW_SCATTER has no txd
# connac2 MCU command cids of interest (low byte of the cid field on the wire)
CID_NAMES = {
    0x10: "PATCH_SEM_CONTROL", 0x05: "PATCH_START_REQ", 0x07: "PATCH_FINISH_REQ",
    0x01: "TARGET_ADDRESS_LEN_REQ", 0x02: "FW_START_REQ", 0x03: "FW_START_REQ?",
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


def hexb(b, n):
    return " ".join(f"{x:02x}" for x in b[:n])


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cap, dev = sys.argv[1], int(sys.argv[2])
    pkts = parse_pcapng(cap)
    t0 = None
    events = []   # (t, kind, ep_in, ep, lencap, desc)
    for pkt in pkts:
        if len(pkt) < 64 or pkt[11] != dev:
            continue
        ev, xfer, ep = pkt[8], pkt[9], pkt[10]
        if xfer != 0x03:                      # bulk only
            continue
        ts = struct.unpack_from("<q", pkt, 16)[0] + struct.unpack_from("<i", pkt, 24)[0] / 1e6
        if t0 is None:
            t0 = ts
        ep_in = bool(ep & 0x80)
        epn = ep & 0x7F
        lencap = struct.unpack_from("<I", pkt, 36)[0]
        data = pkt[64:64 + min(lencap, len(pkt) - 64)]
        if not ep_in and ev == 0x53 and epn == 0x08 and len(data) >= 44:
            # MCU command: SDIO(4) + connac2 txd; cid@40 seq@43, sdio len @0
            cid, seq = data[40], data[43]
            sdio = struct.unpack_from("<H", data, 0)[0]
            name = CID_NAMES.get(cid, "?")
            events.append((ts, "OUT", False, epn, lencap,
                           f"MCU cmd cid=0x{cid:02x} ({name}) seq=0x{seq:02x} sdio_len={sdio}"))
        elif not ep_in and ev == 0x53 and epn == 0x04:
            sdio = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0
            events.append((ts, "OUT", False, epn, lencap,
                           f"FW_SCATTER/TX bulk {lencap}B (sdio_len={sdio})"))
        elif ep_in and ev == 0x43 and epn in (0x04, 0x05):
            # COMPLETE on 0x84/0x85. connac2 rxd: rxd[6]=24B hw, len@24, pkt_type@26,
            # eid@28, seq@29, option@30, ext_eid@32.
            status = struct.unpack_from("<i", pkt, 28)[0]
            if lencap == 0:
                events.append((ts, "IN ", True, epn, 0,
                               f"ZLP/empty completion status={status}"))
            else:
                eid = data[28] if len(data) > 28 else None
                seq = data[29] if len(data) > 29 else None
                ext = data[32] if len(data) > 32 else None
                eids = (f"eid=0x{eid:02x} ext=0x{ext:02x} seq=0x{seq:02x} "
                        if eid is not None else "")
                events.append((ts, "IN ", True, epn, lencap,
                               f"RX {lencap}B {eids}[{hexb(data, 36)}]"))

    # locate FW_START_REQ
    fw_idx = next((i for i, e in enumerate(events)
                   if e[1] == "OUT" and "FW_START_REQ" in e[5]), None)
    print(f"{cap} dev {dev}: {len(events)} bulk events; FW_START at event {fw_idx}")

    if "--head" in sys.argv:
        n = int(sys.argv[sys.argv.index("--head") + 1])
        print(f"\n  first {n} bulk events (all directions):")
        for i, e in enumerate(events[:n]):
            t, kind, ep_in, epn, lc, desc = e
            print(f"  ev{i:>4}  t={(t - t0) * 1000:9.3f}ms  {kind} 0x{(0x80 if ep_in else 0)|epn:02x} {lc:>5}  {desc}")
        return 0

    if "--list-in" in sys.argv:
        # Every device->host IN completion up to (and a little past) FW_START —
        # answers "does the chip respond during patch/RAM load, or only at boot?"
        hi = (fw_idx + 6) if fw_idx is not None else len(events)
        print("\n  IN completions through the load phase:")
        for i, e in enumerate(events[:hi]):
            if e[2]:   # ep_in
                t, _, _, epn, lc, desc = e
                print(f"  ev{i:>4}  t={(t - t0) * 1000:9.3f}ms  0x{0x80|epn:02x}  {desc}")
        return 0
    if fw_idx is None:
        print("FW_START_REQ not found — dumping last 30 bulk events instead:")
        lo, hi = max(0, len(events) - 30), len(events)
    else:
        lo, hi = max(0, fw_idx - 8), min(len(events), fw_idx + 40)
    print(f"\n  {'t(ms)':>9}  dir  ep   {'len':>5}  detail")
    for i in range(lo, hi):
        t, kind, ep_in, epn, lc, desc = events[i]
        mark = "  <== FW_START" if i == fw_idx else ""
        print(f"  {(t - t0) * 1000:9.3f}  {kind} 0x{(0x80 if ep_in else 0)|epn:02x} {lc:>5}  {desc}{mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
