"""
verify_pcap for MT7921AU - the faithfulness gate for the cold-boot bring-up.

Runs two offline checks against a Linux usbmon cold-boot capture, exercising the
driver's REAL code (no reimplementation):

  CHECK 1 - WFDMA register init. One cursor walks the captured control-transfer
  stream (reads + writes, with VALUES parsed straight from the pcapng bytes). It
  runs the driver's real `_dma_init` against a mock transport that serves the
  captured reads and asserts every write the driver emits matches the kernel's
  wire, in order. PASS <=> zero divergences. A transposed magic number (Q_MAP,
  SCHED_SET, ...) fails it instantly.

  CHECK 2 - firmware-load handshake. NEW, and only possible with a scatter-disabled
  capture (`options mt76_usb disable_usb_sg=1`): the device->host RX is finally
  recorded, so every MCU response is visible. For each MCU command on EP 0x08 the
  driver's real `_build_mcu_frame` is rebuilt (seq forced to the captured value)
  and asserted byte-for-byte against the wire, then each command is paired with its
  response on EP 0x84 - the PATCH_SEM acks (eid=0x04) and, critically, the
  FW_START -> eid=0x01 (MCU_EVENT_FW_START) boot signal the chip emits ~15 ms after
  FW_START_REQ. (Pre-scatter captures recorded zero RX, which is why the prior doc
  wrongly concluded FW_START produces no response.)

The mt7921 device address is auto-detected (it differs per plug-in: the device
that touches the WFDMA prefetch register is the one we want).

Usage: uv run python scripts/mt7921au/verify_pcap.py [<pcap>]
"""
import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import wifit3.chips.mt7921au as mt_pkg
from wifit3.chips.mt7921au.firmware import MT7921AUFirmwareLoader
from wifit3.chips.mt7921au.transport import MT7921AUTransport

DEFAULT_CAP = "usb_dumps_new/captures_mt7921u_pau0f-no-adapter-scatter/capture-3.pcap"
PREFETCH0 = 0x7C024600        # first WFDMA-init register touched - start of CHECK 1 window
REG_BREQ = {0x63, 0x66, 0x01, 0x02}   # unified rd/wr + UHW rd/wr (register access)
EP_MCU_OUT, EP_FW_OUT, EP_IN_RX, EP_IN_CMD = 0x08, 0x04, 0x84, 0x85

# connac2 MCU command ids (low byte) seen during firmware load.
CID = {0x10: "PATCH_SEM_CONTROL", 0x05: "PATCH_START_REQ", 0x07: "PATCH_FINISH_REQ",
       0x01: "TARGET_ADDR_LEN_REQ", 0x02: "FW_START_REQ"}
# connac2 MCU event ids (mt76_connac_mcu.h). 0x04 = MT_PATCH_SEM; 0x01 is reused for
# TARGET_ADDRESS_LEN / FW_START / GENERIC - i.e. the generic "command accepted" ack.
EID = {0x01: "FW_START/GENERIC", 0x04: "MT_PATCH_SEM"}
# ext_eid on a PATCH_SEM event = the semaphore status code.
PATCH_SEM_STATUS = {0x02: "NOT_DL_SEM_SUCCESS", 0x03: "REL_SEM_SUCCESS"}


def parse_pcapng(path):
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:  # Enhanced Packet Block
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def detect_dev(pkts):
    """The mt7921 is the device whose control stream touches the WFDMA prefetch
    register - unique to its cold init, and robust against the per-plug-in
    devnum shuffle (3/4/5 across pau0f captures, 8 on the axml)."""
    for pkt in pkts:
        if len(pkt) < 48 or pkt[9] != 0x02 or pkt[8] != 0x53 or pkt[41] not in REG_BREQ:
            continue
        wValue, wIndex = struct.unpack_from("<HH", pkt, 42)
        if ((wValue << 16) | wIndex) == PREFETCH0:
            return pkt[11]
    return None


def build_op_stream(pkts, dev):
    """Ordered list of ('R'|'W', addr, value) for device-`dev` register control ops."""
    ops, pending = [], {}
    for pkt in pkts:
        if len(pkt) < 64:
            continue
        urb_type, xfer, devnum = pkt[8], pkt[9], pkt[11]
        if devnum != dev or xfer != 0x02:        # control transfers only
            continue
        urb_id = pkt[0:8]
        if urb_type == 0x53:                      # SUBMIT (has setup)
            bmReq, bReq = pkt[40], pkt[41]
            if bReq not in REG_BREQ:
                continue
            wValue, wIndex, wLength = struct.unpack_from("<HHH", pkt, 42)
            addr = (wValue << 16) | wIndex
            if bmReq & 0x80:                      # IN = read; value arrives on COMPLETE
                pending[urb_id] = addr
            elif wLength >= 4 and len(pkt) >= 68:  # OUT = write; value in data
                ops.append(("W", addr, struct.unpack_from("<I", pkt, 64)[0]))
        elif urb_type == 0x43:                    # COMPLETE
            addr = pending.pop(urb_id, None)
            if addr is not None and len(pkt) >= 68:
                ops.append(("R", addr, struct.unpack_from("<I", pkt, 64)[0]))
    return ops


def build_bulk_stream(pkts, dev):
    """Ordered bulk events for device `dev`: ('OUT', ep, data) on SUBMIT (the host
    write), ('IN', ep, data) on COMPLETE (the device response/RX)."""
    evs = []
    for pkt in pkts:
        if len(pkt) < 64 or pkt[11] != dev or pkt[9] != 0x03:   # bulk only
            continue
        ev, ep = pkt[8], pkt[10]
        lencap = struct.unpack_from("<I", pkt, 36)[0]
        data = pkt[64:64 + min(lencap, len(pkt) - 64)]
        if ev == 0x53 and not (ep & 0x80):        # OUT submit (host->device payload)
            evs.append(("OUT", ep, data))
        elif ev == 0x43 and (ep & 0x80) and lencap > 0:  # IN complete with data
            evs.append(("IN", ep, data))
    return evs


# ----------------------------------------------------------------------------
# CHECK 1 - _dma_init register-write faithfulness
# ----------------------------------------------------------------------------

class Divergence(Exception):
    pass


class ReplayTransport:
    """Serves captured reads; asserts driver writes match the captured stream."""
    def __init__(self, ops):
        self.ops = ops
        self.i = 0
        self.matched = []      # (addr, value) writes confirmed against the wire

    def _next(self):
        if self.i >= len(self.ops):
            raise Divergence(f"driver did an extra op past the captured stream (op #{self.i})")
        op = self.ops[self.i]
        self.i += 1
        return op

    def read_reg32_unified(self, addr):
        kind, a, v = self._next()
        if kind != "R" or a != addr:
            raise Divergence(f"op #{self.i-1}: driver READ 0x{addr:08x}, wire has {kind} 0x{a:08x}")
        return v

    def write_reg32_unified(self, addr, value):
        kind, a, v = self._next()
        if kind != "W" or a != addr:
            raise Divergence(f"op #{self.i-1}: driver WROTE 0x{addr:08x}, wire has {kind} 0x{a:08x}")
        if v != value:
            raise Divergence(f"op #{self.i-1}: VALUE MISMATCH at 0x{addr:08x} - "
                             f"driver 0x{value:08x} vs wire 0x{v:08x}")
        self.matched.append((addr, value))


def check_dma_init(pkts, dev):
    ops = build_op_stream(pkts, dev)
    start = next((k for k, o in enumerate(ops) if o[1] == PREFETCH0), None)
    if start is None:
        print("[FAIL] CHECK 1: WFDMA prefetch register not found in capture")
        return False
    window = ops[start:]
    loader = MT7921AUFirmwareLoader(ReplayTransport(window),
                                    Path(mt_pkg.__file__).parent / "assets")
    print(f"CHECK 1 - _dma_init register init  ({len(ops)} reg ops; window @ op {start})")
    try:
        loader._dma_init()
    except Divergence as e:
        print(f"  [FAIL] DIVERGENCE - {e}")
        print("  (the cursor stops at the first op the driver does not reproduce - that IS the bug)")
        return False
    t = loader.transport
    print(f"  [PASS] reproduced {len(t.matched)} register writes byte-for-byte "
          f"({t.i} ops consumed)")
    return True


# ----------------------------------------------------------------------------
# CHECK 2 - firmware-load command/response handshake
# ----------------------------------------------------------------------------

def parse_mcu_command(frame):
    """Pull (cid, set_query, ext_cid, seq, payload) out of a captured EP-0x08 frame.
    Layout: SDIO(4) + connac2 txd(64) + payload + pad. cid@40 pkt_type@41
    set_query@42 seq@43 ext_cid@45; mcu_txd.len@36 = 32 + payload_len."""
    cid, set_query, seq, ext_cid = frame[40], frame[42], frame[43], frame[45]
    mcu_len = struct.unpack_from("<H", frame, 36)[0]
    payload_len = max(0, mcu_len - 32)
    payload = bytes(frame[68:68 + payload_len])
    return cid, set_query, ext_cid, seq, payload


def parse_mcu_response(data):
    """connac2 rxd: rxd[6]=24B, len@24, pkt_type@26, eid@28, seq@29, option@30,
    ext_eid@32."""
    if len(data) < 33:
        return None
    return {"eid": data[28], "seq": data[29], "ext_eid": data[32]}


def pair_load_handshake(evs):
    """Walk the bulk stream up to (and including) FW_START_REQ, pairing each MCU
    command on EP 0x08 with the next device response on EP 0x84/0x85. Returns the
    ordered list of (command_frame, response_or_None) and the FW_SCATTER count."""
    pairs, fw_chunks = [], 0
    for kind, ep, data in evs:
        if kind == "OUT" and ep == EP_FW_OUT:
            fw_chunks += 1
        elif kind == "OUT" and ep == EP_MCU_OUT:
            if len(data) >= 46:
                pairs.append((data, None))
        elif kind == "IN" and ep in (EP_IN_RX, EP_IN_CMD):
            if pairs and pairs[-1][1] is None:
                pairs[-1] = (pairs[-1][0], data)
                if pairs[-1][0][40] == 0x02:   # FW_START_REQ answered -> handshake done
                    break
    return pairs, fw_chunks


def check_handshake(pkts, dev):
    evs = build_bulk_stream(pkts, dev)
    if not any(k == "IN" for k, _, _ in evs):
        print("CHECK 2 - firmware-load handshake")
        print("  [SKIP] this capture has no device->host RX - it predates "
              "disable_usb_sg=1, so MCU responses are invisible. Re-capture with "
              "`options mt76_usb disable_usb_sg=1` to verify the handshake.")
        return "SKIP"
    pairs, fw_chunks = pair_load_handshake(evs)
    if not pairs:
        print("[FAIL] CHECK 2: no MCU commands found in capture")
        return False

    # Real frame builder, exercised once per captured command (seq forced to match).
    asyncio.set_event_loop(asyncio.new_event_loop())
    tx = MT7921AUTransport(None)

    print(f"CHECK 2 - firmware-load handshake  ({len(pairs)} MCU commands, "
          f"{fw_chunks} FW_SCATTER chunks)")
    print(f"  {'cid':>22}  seq  {'frame':>5}  rebuilt  response")
    ok = True
    cids = []
    for frame, resp in pairs:
        cid, set_query, ext_cid, seq, payload = parse_mcu_command(frame)
        cids.append(cid)
        name = CID.get(cid, f"0x{cid:02x}?")

        # Rebuild via the driver's REAL frame builder, forcing the captured seq.
        tx._mcu_seq = (seq - 1) & 0x0F
        built, built_seq = tx._build_mcu_frame(cid, payload, set_query=set_query, ext_cid=ext_cid)
        if built_seq != seq:
            print(f"  [FAIL] {name}: builder produced seq 0x{built_seq:02x}, wanted 0x{seq:02x}")
            ok = False
        elif bytes(built) != bytes(frame):
            diff = next((k for k in range(min(len(built), len(frame))) if built[k] != frame[k]), None)
            print(f"  [FAIL] {name} seq=0x{seq:02x}: frame mismatch at byte {diff} "
                  f"(driver 0x{built[diff]:02x} vs wire 0x{frame[diff]:02x}; "
                  f"len {len(built)} vs {len(frame)})")
            ok = False
            continue

        # Pair + validate the response.
        r = parse_mcu_response(resp) if resp else None
        if r is None:
            rdesc = "-  (no response captured)"
            if cid in (0x10, 0x02):   # PATCH_SEM and FW_START are answered on the wire
                rdesc = "MISSING (expected a response)"
                ok = False
        else:
            edesc = EID.get(r["eid"], f"eid=0x{r['eid']:02x}?")
            if cid == 0x10:           # PATCH_SEM -> eid=0x04, ext_eid carries status
                exp = PATCH_SEM_STATUS.get(r["ext_eid"], f"ext=0x{r['ext_eid']:02x}?")
                rdesc = f"{edesc}/{exp}"
                if r["eid"] != 0x04:
                    rdesc += "  [FAIL eid!=0x04]"
                    ok = False
            else:                      # everything else -> eid=0x01 generic/boot ack
                rdesc = edesc
                if r["eid"] != 0x01:
                    rdesc += "  [FAIL eid!=0x01]"
                    ok = False
            if r["seq"] != seq:        # device must echo our seq
                rdesc += f"  [FAIL resp seq 0x{r['seq']:02x}!=0x{seq:02x}]"
                ok = False
            if cid == 0x02:
                rdesc += "   <<< MCU_EVENT_FW_START (boot signal)"

        print(f"  {name:>22}  0x{seq:02x}  {len(frame):>5}  {'==':>7}  {rdesc}")

    # Orchestration shape: SEM_GET, START, FINISH, SEM_REL, NxTARGET_ADDR, FW_START.
    head = cids[:4]
    if head != [0x10, 0x05, 0x07, 0x10]:
        print(f"  [FAIL] unexpected patch-phase command order: {[hex(c) for c in head]}")
        ok = False
    if cids[-1] != 0x02:
        print(f"  [FAIL] last command is 0x{cids[-1]:02x}, expected FW_START_REQ (0x02)")
        ok = False
    n_target = sum(1 for c in cids if c == 0x01)
    print(f"  {n_target} TARGET_ADDR_LEN_REQ (one per downloaded RAM region); "
          f"sequence {'OK' if ok else 'FAILED'}")
    print(f"  [{'PASS' if ok else 'FAIL'}] every MCU command rebuilt byte-faithfully "
          f"and paired with its device response")
    return ok


def main():
    cap = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CAP
    pkts = parse_pcapng(cap)
    if not pkts:
        print(f"[FAIL] no packets parsed from {cap}")
        return 1
    dev = detect_dev(pkts)
    if dev is None:
        print(f"[FAIL] could not locate the mt7921 device (no WFDMA prefetch op) in {cap}")
        return 1
    print(f"verify_pcap mt7921au - {cap}")
    print(f"  {len(pkts)} packets; mt7921 auto-detected as device {dev}\n")

    ok1 = check_dma_init(pkts, dev)
    print()
    ok2 = check_handshake(pkts, dev)

    failed = (not ok1) or (ok2 is False)
    if failed:
        print("\n[FAIL] see divergences above")
    elif ok2 == "SKIP":
        print("\n[PASS] CHECK 1 green; CHECK 2 skipped (capture has no RX)")
    else:
        print("\n[PASS] both checks green")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
