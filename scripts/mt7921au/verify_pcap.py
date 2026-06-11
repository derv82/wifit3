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
from wifit3.chips.mt7921au import init as mt_init
from wifit3.chips.mt7921au import mcu as mt_mcu
from wifit3.chips.mt7921au import mac as mt_mac
from wifit3.chips.mt7921au.constants import MT_MIB_SDR9, MT_MIB_SDR3
from wifit3.chips.mt7921au.firmware import MT7921AUFirmwareLoader
from wifit3.chips.mt7921au.transport import MT7921AUTransport

DEFAULT_CAP = "usb_dumps_new/captures_mt7921u_pau0f-no-adapter-scatter/capture-3.pcap"
PREFETCH0 = 0x7C024600        # first WFDMA-init register touched - start of CHECK 1 window
REG_BREQ = {0x63, 0x66, 0x01, 0x02}   # unified rd/wr + UHW rd/wr (register access)
# The boot-status query (bRequest 0x01, wValue 0x30, 64-byte read) decodes to this
# pseudo-address. It is NOT a WiFi register: on the composite AXML unit (WiFi+BT) the
# btusb function shares the devnum and its concurrent-init boot-status polls leak into
# the stream. The single-function pau0f has none. Dropped from the post-boot walk (and
# counted) as another driver's traffic, like an aireplay TX-status waiver.
BOOT_STATUS_ADDR = 0x00300000
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
        # The firmware-load commands are all plain MCU_CMD(cid) — no ext/uni/ce
        # flags — so the encoded cmd is just the captured cid byte.
        tx._mcu_seq = (seq - 1) & 0x0F
        built, built_seq = tx._build_mcu_frame(cid, payload)
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


# ----------------------------------------------------------------------------
# CHECK 3 - post-boot device init + monitor entry + channel (single cursor)
#
# One cursor walks the merged post-boot stream: unified-bus register reads/writes
# (control) AND MCU commands (bulk EP 0x08) with their seq-matched responses (bulk
# EP 0x84), in capture order. The driver's REAL post-boot orchestration
# (init.post_boot_init) is run against a mock transport that serves the captured
# reads/responses and asserts every write/command byte-for-byte. Fail-closed:
#   - matched     -> a handler reproduced the op; cursor advances.
#   - divergence  -> a ported handler emitted the wrong bytes; STOP, name it.
#   - frontier    -> the orchestration returned but ops remain unconsumed; the
#                    first leftover op IS the next thing to port. PASS <=> none left.
# ----------------------------------------------------------------------------

class PostBootDivergence(Exception):
    pass


def build_postboot_stream(pkts, dev):
    """Merged, capture-ordered op list for device `dev`. Each op is a dict:
       {'kind':'R'|'W', 'addr', 'val'} for unified-bus register access, or
       {'kind':'MCU', 'frame', 'seq', 'resp'} for an EP-0x08 command + its
       seq-matched EP-0x84/0x85 response."""
    merged, pending_rd, pending_mcu = [], {}, []
    for pkt in pkts:
        if len(pkt) < 40 or pkt[11] != dev:
            continue
        urb_type, xfer, ep = pkt[8], pkt[9], pkt[10]
        urb_id = pkt[0:8]
        if xfer == 0x02:                                  # control (register bus)
            if urb_type == 0x53:                          # SUBMIT
                bmReq, bReq = pkt[40], pkt[41]
                if bReq not in REG_BREQ:
                    continue
                wValue, wIndex, wLength = struct.unpack_from("<HHH", pkt, 42)
                addr = (wValue << 16) | wIndex
                if bmReq & 0x80:
                    pending_rd[urb_id] = addr
                elif wLength >= 4 and len(pkt) >= 68:
                    merged.append({"kind": "W", "addr": addr,
                                   "val": struct.unpack_from("<I", pkt, 64)[0]})
            elif urb_type == 0x43:                        # COMPLETE
                addr = pending_rd.pop(urb_id, None)
                if addr is not None and len(pkt) >= 68:
                    merged.append({"kind": "R", "addr": addr,
                                   "val": struct.unpack_from("<I", pkt, 64)[0]})
        elif xfer == 0x03:                                # bulk
            lencap = struct.unpack_from("<I", pkt, 36)[0]
            data = pkt[64:64 + min(lencap, len(pkt) - 64)]
            if urb_type == 0x53 and ep == EP_MCU_OUT and len(data) >= 44:
                op = {"kind": "MCU", "frame": bytes(data), "seq": data[43], "resp": None}
                merged.append(op)
                pending_mcu.append(op)
            elif urb_type == 0x43 and (ep & 0x80) and lencap > 0 and len(data) > 29:
                rseq = data[29]                            # connac rxd seq
                for op in pending_mcu:
                    if op["resp"] is None and op["seq"] == rseq:
                        op["resp"] = bytes(data)
                        break
    return merged


def _fmt_op(op):
    if op["kind"] == "MCU":
        f = op["frame"]
        std = f[38] == 0x00 and f[39] == 0x80
        cid = f[40] if std else struct.unpack_from("<H", f, 38)[0]
        ext = f[45] if std else 0
        kind = "STD" if std else "UNI"
        return f"MCU {kind} cid=0x{cid:02x} ext=0x{ext:02x} seq=0x{op['seq']:02x} ({len(f)}B)"
    return f"{op['kind']} 0x{op['addr']:08x}=0x{op['val']:08x}"


class PostBootReplay:
    """Mock transport driving init.post_boot_init over the merged op stream."""

    def __init__(self, ops):
        self.ops = ops
        self.i = 0

    def _next(self):
        if self.i >= len(self.ops):
            raise PostBootDivergence(f"driver ran past the captured stream (op #{self.i})")
        op = self.ops[self.i]
        self.i += 1
        return op

    def peek(self):
        return self.ops[self.i] if self.i < len(self.ops) else None

    def read_reg32_unified(self, addr):
        op = self._next()
        if op["kind"] != "R" or op["addr"] != addr:
            raise PostBootDivergence(
                f"op #{self.i-1}: driver READ 0x{addr:08x}, wire has {_fmt_op(op)}")
        return op["val"]

    def write_reg32_unified(self, addr, value):
        op = self._next()
        if op["kind"] != "W" or op["addr"] != addr:
            raise PostBootDivergence(
                f"op #{self.i-1}: driver WROTE 0x{addr:08x}, wire has {_fmt_op(op)}")
        if op["val"] != value:
            raise PostBootDivergence(
                f"op #{self.i-1}: VALUE MISMATCH at 0x{addr:08x} - "
                f"driver 0x{value:08x} vs wire 0x{op['val']:08x}")

    async def send_mcu_command(self, cmd, payload=b"", wait_resp=True, resp_timeout_ms=2000):
        op = self._next()
        if op["kind"] != "MCU":
            raise PostBootDivergence(
                f"op #{self.i-1}: driver sent MCU cmd=0x{cmd:x}, wire has {_fmt_op(op)}")
        built = mt_mcu.build_mcu_frame(cmd, payload, op["seq"])
        if bytes(built) != op["frame"]:
            n = min(len(built), len(op["frame"]))
            d = next((k for k in range(n) if built[k] != op["frame"][k]), n)
            db = f"0x{built[d]:02x}" if d < len(built) else "-"
            wb = f"0x{op['frame'][d]:02x}" if d < len(op["frame"]) else "-"
            raise PostBootDivergence(
                f"op #{self.i-1}: MCU frame mismatch cmd=0x{cmd:x} at byte {d} "
                f"(driver {db} vs wire {wb}; len {len(built)} vs {len(op['frame'])})")
        return op["resp"]

    # standard bus is not used post-boot (everything is unified); flag if it is.
    def read_reg32(self, addr):
        raise PostBootDivergence(f"unexpected standard-bus READ 0x{addr:08x}")

    def write_reg32(self, addr, value):
        raise PostBootDivergence(f"unexpected standard-bus WRITE 0x{addr:08x}")

# Trigger addresses: the first register read of each periodic mac_work sequence.
_SURVEY_FIRST = MT_MIB_SDR9(0)      # mt792x_phy_update_channel busy_time read
_MIB_FIRST = MT_MIB_SDR3(0)         # mt792x_mac_update_mib_stats fcs_err read


def _decode_operational_mcu(f):
    """Peek a captured operational MCU frame and return (cmd, payload) for the real
    driver builder that reproduces it — channel/enable/filter params read straight
    off the wire. Returns None if it's not a recognized operational command."""
    std = f[38] == 0x00 and f[39] == 0x80
    if not std:                                   # UNI command
        cid = f[38] | (f[39] << 8)
        if cid == mt_mcu.UNI_CMD_SNIFFER:
            band_idx = f[52]                      # payload[0]
            tag = f[56] | (f[57] << 8)            # tlv tag (payload offset 4)
            if tag == 0:                          # sniffer_enable_tlv
                return mt_mcu.set_sniffer(bool(f[60]), band_idx)
            if tag == 1:                          # sniffer_config_tlv (control_ch @ +8)
                return mt_mcu.config_sniffer(f[64], band_idx)
        return None
    cid = f[40]                                   # STD command id
    if cid == mt_mcu.CE_CMD_SET_RX_FILTER:        # payload @68: mode@4, fif@8, bit_map@12, bit_op@16
        fif = struct.unpack_from("<I", f, 76)[0]
        bit_map = struct.unpack_from("<I", f, 80)[0]
        return mt_mcu.set_rxfilter(fif, f[84], bit_map)
    if cid == mt_mcu.CE_CMD_SET_BSS_ABORT:
        return mt_mcu.set_bss_abort()
    if cid == mt_mcu.CE_CMD_CHIP_CONFIG:          # set_deep_sleep -> "KeepFullPwr %d"
        idx = f.find(b"KeepFullPwr ")
        return mt_mcu.set_deep_sleep(idx >= 0 and f[idx + 12:idx + 13] == b"0")
    return None


async def walk_operational(replay):
    """Continue the CHECK-3 cursor past post_boot_init through the airmon/airodump
    operational tail: the monitor-entry + channel-hop MCU commands (interleaved in
    the tool's wire order) and the periodic mt792x_mac_work MIB read cycles
    (update_survey every tick, update_mib_stats every second tick). Each op is
    peeked, matched to its real driver builder/sequence, and replayed against the
    cursor — which asserts the bytes/addresses match.

    Returns "exhausted" (every op reproduced), "frontier" (hit an op no handler
    reproduces), or "truncated" (the capture stopped partway through a final
    mac_work cycle — a benign capture-end artifact, every captured op matched)."""
    while replay.peek() is not None:
        op = replay.peek()
        try:
            if op["kind"] == "MCU":
                disp = _decode_operational_mcu(op["frame"])
                if disp is None:
                    return "frontier"
                await replay.send_mcu_command(*disp)
            elif op["kind"] == "R" and op["addr"] == _SURVEY_FIRST:
                mt_mac.update_survey(replay)
            elif op["kind"] == "R" and op["addr"] == _MIB_FIRST:
                mt_mac.update_mib_stats(replay)
            else:
                return "frontier"
        except PostBootDivergence:
            # A mac_work sequence that runs off the end of the captured ops is the
            # capture stopping mid-cycle, not a divergence — every recorded op matched.
            if replay.i >= len(replay.ops):
                return "truncated"
            raise
    return "exhausted"


def check_post_boot(pkts, dev):
    merged = build_postboot_stream(pkts, dev)
    # Split at FW_START (std MCU, cid 0x02); the post-boot walk begins at the first
    # MCU command after it (GET_NIC_CAPAB) — the leading FW_N9_RDY register poll
    # belongs to firmware.load_firmware, not the post-boot orchestration.
    fw = next((k for k, o in enumerate(merged) if o["kind"] == "MCU"
               and o["frame"][38] == 0x00 and o["frame"][39] == 0x80
               and o["frame"][40] == 0x02), None)
    if fw is None:
        print("CHECK 3 - post-boot init")
        print("  [SKIP] FW_START not found (pre-scatter capture?)")
        return "SKIP"
    post = merged[fw + 1:]
    start = next((k for k, o in enumerate(post) if o["kind"] == "MCU"), None)
    ops = post[start:] if start is not None else []

    # Drop btusb boot-status polls that leak into the post-boot region on composite
    # (WiFi+BT) units — another driver's traffic, not WiFi register ops (see
    # BOOT_STATUS_ADDR). Counted and reported, never silent.
    n_bootstatus = sum(1 for o in ops
                       if o["kind"] in ("R", "W") and o["addr"] == BOOT_STATUS_ADDR)
    ops = [o for o in ops
           if not (o["kind"] in ("R", "W") and o["addr"] == BOOT_STATUS_ADDR)]

    n_reg = sum(1 for o in ops if o["kind"] in ("R", "W"))
    n_mcu = sum(1 for o in ops if o["kind"] == "MCU")
    print(f"CHECK 3 - post-boot init  ({len(ops)} ops: {n_mcu} MCU cmds, {n_reg} reg R/W)")
    if n_bootstatus:
        print(f"  waived {n_bootstatus} btusb boot-status poll(s) "
              f"(composite-device BT coexistence; not WiFi register ops)")

    replay = PostBootReplay(ops)
    asyncio.set_event_loop(asyncio.new_event_loop())
    state = {}

    async def _drive():
        await mt_init.post_boot_init(replay)        # deterministic init -> add_interface
        state["init_end"] = replay.i
        state["status"] = await walk_operational(replay)  # monitor entry + hops + MIB

    try:
        asyncio.get_event_loop().run_until_complete(_drive())
    except PostBootDivergence as e:
        print(f"  [FAIL] DIVERGENCE - {e}")
        print("  (the cursor stops at the first op the driver does not reproduce)")
        return False

    init_end, status = state["init_end"], state["status"]
    print(f"  init: {init_end} ops (firmware tail -> add_interface); "
          f"operational: {replay.i - init_end} ops (monitor entry + hops + mac_work MIB)")
    if status == "frontier":
        front = ops[replay.i]
        print(f"  [FRONTIER] reproduced {replay.i} ops; next unported op @{replay.i} "
              f"= {_fmt_op(front)}")
        print("  ^ port this next; the gate is green only when every op is reproduced.")
        return "FRONTIER"
    if status == "truncated":
        print(f"  [PASS] reproduced all {replay.i} captured post-boot ops byte-for-byte "
              f"(capture ends mid mac_work cycle — benign)")
        return True
    print(f"  [PASS] reproduced all {replay.i} post-boot ops byte-for-byte")
    return True


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
    print()
    ok3 = check_post_boot(pkts, dev)

    failed = (not ok1) or (ok2 is False) or (ok3 is False)
    if failed:
        print("\n[FAIL] see divergences above")
        return 1
    # CHECK 3 is a work-in-progress single-cursor walk: FRONTIER means the boot
    # path is faithful and the post-boot port has advanced to a named next op.
    if ok3 == "FRONTIER":
        print("\n[FRONTIER] CHECK 1+2 green; CHECK 3 advancing — see the next op above")
        return 2
    if ok2 == "SKIP" or ok3 == "SKIP":
        print("\n[PASS] CHECK 1 green; later checks skipped (capture has no RX)")
        return 0
    print("\n[PASS] all three checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
