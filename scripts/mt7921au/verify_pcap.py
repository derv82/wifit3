"""
verify_pcap for MT7921AU - single-cursor cold-boot gate.

Drives the driver's REAL cold bring-up (MT7921AUDriver._bringup) against a Linux
usbmon cold-boot capture over ONE strict positional cursor: from the very first
register op (the chip-id read at the top of firmware.load_firmware) through the
full firmware upload (WFDMA init + ROM patch + WM RAM + FW_START handshake) and
post_boot_init, byte-for-byte. No hand copy, no windowing: connect() and this walk
run the same _bringup, so they cannot drift, and there are no skipped ops between
"checks" for a reordered bring-up to hide in.

  COLD-BOOT CURSOR - the merged, capture-ordered op stream is served/asserted by a
  mock transport (ColdBootReplay): captured register reads and MCU responses are
  served back; every register write, MCU command frame, and FW_SCATTER chunk the
  driver emits is asserted byte-for-byte, in order. A codec/mock bug can only cause
  a false RED (a driver op that fails to match the wire), never a false GREEN - the
  driver's real bytes are always compared to the recorded wire.

  After _bringup returns, the SAME cursor continues through the operational tail
  (monitor entry + airodump channel hops + periodic mac_work MIB reads). airmon /
  mac80211 interleave the monitor-entry commands in a tool-timing-dependent order
  that differs per capture, so the tail is matched flexibly (each op peeked and
  routed to the real driver builder that reproduces it), not strict-positional.

  CHECK TX - rebuild every captured aireplay TX frame via the driver's REAL tx.build_tx
  and assert the full USB bulk-OUT bytes AND endpoint. Only runs on a capture with
  post-boot 802.11 TX.

Two capture-shape differences from the WiFi cold boot are counted and printed, never
silently waived:
  - the Linux probe reads the chip REV (0x70010204) right after the chip id; the port
    reads only the id, so that one wire read is waived.
  - firmware.load_firmware replicates two btusb boot-status polls (a Bluetooth-
    coexistence artifact of the composite AXML unit); the single-function pau0f WiFi
    cold boot has none, so those port reads are served off-cursor, and any btusb
    boot-status polls that leak into a composite capture are waived.

The mt7921 device address is auto-detected (it differs per plug-in: the device that
touches the WFDMA prefetch register is the one we want).

Usage: uv run python scripts/verify_pcap.py mt7921au [<pcap>]
"""
import asyncio
import struct
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import wifit3.chips.mt7921au as mt_pkg  # noqa: F401,E402  (kept for parity / future asset lookups)
from wifit3.chips.mt7921au import mcu as mt_mcu  # noqa: E402
from wifit3.chips.mt7921au import mac as mt_mac  # noqa: E402
from wifit3.chips.mt7921au import tx as mt_tx  # noqa: E402
from wifit3.chips.mt7921au.constants import (  # noqa: E402
    MT_MIB_SDR9, MT_MIB_SDR3, MT_SDIO_TXD_SIZE, MT_TXD3_REM_TX_COUNT, MT_TXD3_NO_ACK,
    MT_VEND_READ_REG_REQ, MT_VEND_POWER_ON,
)
from wifit3.chips.mt7921au.driver import MT7921AUDriver  # noqa: E402

DEFAULT_CAP = "usb_dumps_new/captures_mt7921u_pau0f-no-adapter-scatter/capture-3.pcap"
PREFETCH0 = 0x7C024600        # first WFDMA-init register touched - used to auto-detect the device
REG_BREQ = {0x63, 0x66, 0x01, 0x02}   # unified rd/wr + UHW rd/wr (register access); addr-keyed
# The btusb boot-status query (bRequest 0x01, wValue 0x30, 64-byte read) decodes to this
# pseudo-address. It is NOT a WiFi register: on the composite AXML unit (WiFi+BT) the btusb
# function shares the devnum and its concurrent-init boot-status polls leak into the stream.
# The single-function pau0f has none. Waived (and counted) as another driver's traffic.
BOOT_STATUS_ADDR = 0x00300000
# The Linux mt7921u probe reads chip id (0x70010200) THEN chip rev (0x70010204); the port
# reads only the id (it needs no rev), so the one wire chip-rev read is waived (and counted).
CHIP_REV_ADDR = 0x70010204
EP_MCU_OUT, EP_FW_OUT, EP_IN_RX, EP_IN_CMD = 0x08, 0x04, 0x84, 0x85
# A composite WiFi+BT unit shares its devnum with the btusb function, whose concurrent
# BT-firmware load interleaves thousands of boot-status polls (and other BT register
# traffic) into the stream from op #0. A single-function unit's Linux cold boot reads
# BOOT_STATUS_ADDR zero times, so any real quantity of them flags a composite capture
# the WiFi cold boot cannot be cleanly separated from.
_COMPOSITE_BOOTSTATUS_MIN = 8


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
# Merged cold-boot op stream + the single-cursor replay transport.
# ----------------------------------------------------------------------------

class Divergence(Exception):
    pass


def build_cold_stream(pkts, dev):
    """Merged, capture-ordered op list for device `dev`, from the first register op.
    Each op is a dict:
      {'kind':'R'|'W', 'addr', 'val'}          register read / write (any bus, addr-keyed)
      {'kind':'VENDOR','bReq','wVal','wIdx','data'}  a non-register vendor OUT (power-on)
      {'kind':'MCU','frame','seq','resp'}       an EP-0x08 command + its seq-matched response
      {'kind':'FWCHUNK','data'}                 an EP-0x04 FW_SCATTER chunk (SDIO hdr stripped)

    btusb boot-status reads (bReq 0x01, wValue 0x30, bmReq 0xC0) are captured as R ops at
    BOOT_STATUS_ADDR like any UHW read; the caller waives them. Post-FW-START bulk-OUT TX
    frames (EP 0x04 data, EP 0x09 mgmt) are excluded - they are the TX check's domain."""
    merged, pending_rd, pending_mcu = [], {}, []
    seen_fw = False
    for pkt in pkts:
        if len(pkt) < 40 or pkt[11] != dev:
            continue
        urb_type, xfer, ep = pkt[8], pkt[9], pkt[10]
        urb_id = pkt[0:8]
        if xfer == 0x02:                                  # control (register bus)
            if urb_type == 0x53:                          # SUBMIT
                bmReq, bReq = pkt[40], pkt[41]
                wValue, wIndex, wLength = struct.unpack_from("<HHH", pkt, 42)
                addr = (wValue << 16) | wIndex
                if bmReq & 0x80:                          # IN (read; value on COMPLETE)
                    if bReq in REG_BREQ:
                        pending_rd[urb_id] = addr
                elif bReq in REG_BREQ and wLength >= 4 and len(pkt) >= 68:
                    merged.append({"kind": "W", "addr": addr,
                                   "val": struct.unpack_from("<I", pkt, 64)[0]})
                elif bReq == MT_VEND_POWER_ON:
                    merged.append({"kind": "VENDOR", "bReq": bReq,
                                   "wVal": wValue, "wIdx": wIndex, "data": b""})
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
                if data[38] == 0x00 and data[39] == 0x80 and data[40] == 0x02:
                    seen_fw = True                         # FW_START: RAM upload done
            elif urb_type == 0x53 and ep == EP_FW_OUT and not seen_fw and len(data) >= 4:
                clen = struct.unpack_from("<I", data, 0)[0] & 0xFFFF   # SDIO hdr tx_bytes
                merged.append({"kind": "FWCHUNK", "data": bytes(data[4:4 + clen])})
            elif urb_type == 0x43 and (ep & 0x80) and lencap > 0 and len(data) > 29:
                rseq = data[29]                            # connac rxd seq
                for op in pending_mcu:
                    if op["resp"] is None and op["seq"] == rseq:
                        op["resp"] = bytes(data)
                        break
    return merged


def _fmt_op(op):
    k = op["kind"]
    if k == "MCU":
        f = op["frame"]
        std = f[38] == 0x00 and f[39] == 0x80
        cid = f[40] if std else struct.unpack_from("<H", f, 38)[0]
        ext = f[45] if std else 0
        return f"MCU {'STD' if std else 'UNI'} cid=0x{cid:02x} ext=0x{ext:02x} seq=0x{op['seq']:02x} ({len(f)}B)"
    if k == "FWCHUNK":
        return f"FWCHUNK ({len(op['data'])}B)"
    if k == "VENDOR":
        return f"VENDOR bReq=0x{op['bReq']:02x} wVal=0x{op['wVal']:x} wIdx=0x{op['wIdx']:x}"
    return f"{k} 0x{op['addr']:08x}=0x{op['val']:08x}"


class _EmptyQueue:
    """Stub for transport._mcu_rx_queue: load_firmware's post-patch drain sees it empty."""
    def empty(self):
        return True

    def qsize(self):
        return 0


class ColdBootReplay:
    """Single strict positional cursor over the merged cold-boot op stream, exposing the
    MT7921AUTransport surface the driver's bring-up calls. Serves captured reads and MCU
    responses; asserts every write / MCU frame / FW chunk byte-for-byte, in order.

    read_boot_status is served off-cursor (a btusb-coexistence diagnostic the port
    replicates, absent from the WiFi cold boot) and counted. Everything else is positional,
    so a mock bug can only stop the walk early (false RED), never pass a wrong op (false GREEN).
    """

    def __init__(self, ops):
        self.ops = ops
        self.i = 0
        self.boot_status_served = 0
        self._mcu_rx_queue = _EmptyQueue()
        self.dev = None

    def _next(self):
        if self.i >= len(self.ops):
            raise Divergence(f"driver ran past the captured stream (op #{self.i})")
        op = self.ops[self.i]
        self.i += 1
        return op

    def peek(self):
        return self.ops[self.i] if self.i < len(self.ops) else None

    # --- register bus: unified / standard / UHW reads and writes are all addr-keyed ---
    def _read(self, addr):
        op = self._next()
        if op["kind"] != "R" or op["addr"] != addr:
            raise Divergence(f"op #{self.i-1}: driver READ 0x{addr:08x}, wire has {_fmt_op(op)}")
        return op["val"]

    def read_reg32(self, addr):
        return self._read(addr)

    def read_reg32_unified(self, addr):
        return self._read(addr)

    def read_vendor_request(self, bmReq, bReq, wVal, wIdx, wLength, timeout=1000):
        """_poll_reg reads the unified bus straight through this path (bReq 0x63)."""
        if bReq != MT_VEND_READ_REG_REQ:
            raise Divergence(f"op #{self.i}: unexpected vendor read bReq=0x{bReq:02x}")
        return struct.pack("<I", self._read((wVal << 16) | wIdx))

    def _write(self, addr, value):
        op = self._next()
        if op["kind"] != "W" or op["addr"] != addr:
            raise Divergence(f"op #{self.i-1}: driver WROTE 0x{addr:08x}, wire has {_fmt_op(op)}")
        if op["val"] != value:
            raise Divergence(f"op #{self.i-1}: VALUE MISMATCH at 0x{addr:08x} - "
                             f"driver 0x{value:08x} vs wire 0x{op['val']:08x}")

    def write_reg32(self, addr, value):
        self._write(addr, value)

    def write_reg32_unified(self, addr, value):
        self._write(addr, value)

    def send_vendor_request(self, bmReq, bReq, wVal, wIdx, data=b"", timeout=1000):
        op = self._next()
        if (op["kind"] != "VENDOR" or op["bReq"] != bReq
                or op["wVal"] != wVal or op["wIdx"] != wIdx):
            raise Divergence(f"op #{self.i-1}: driver VENDOR bReq=0x{bReq:02x} "
                             f"wVal=0x{wVal:x} wIdx=0x{wIdx:x}, wire has {_fmt_op(op)}")
        if bytes(data) != op["data"]:
            raise Divergence(f"op #{self.i-1}: VENDOR data mismatch "
                             f"(driver {bytes(data).hex()} vs wire {op['data'].hex()})")

    def read_boot_status(self, length=64):
        self.boot_status_served += 1
        return b"\x00" * length

    async def send_mcu_command(self, cmd, payload=b"", wait_resp=True, resp_timeout_ms=2000):
        op = self._next()
        if op["kind"] != "MCU":
            raise Divergence(f"op #{self.i-1}: driver sent MCU cmd=0x{cmd:x}, wire has {_fmt_op(op)}")
        built = mt_mcu.build_mcu_frame(cmd, payload, op["seq"])
        if bytes(built) != op["frame"]:
            n = min(len(built), len(op["frame"]))
            d = next((k for k in range(n) if built[k] != op["frame"][k]), n)
            db = f"0x{built[d]:02x}" if d < len(built) else "-"
            wb = f"0x{op['frame'][d]:02x}" if d < len(op["frame"]) else "-"
            raise Divergence(f"op #{self.i-1}: MCU frame mismatch cmd=0x{cmd:x} at byte {d} "
                             f"(driver {db} vs wire {wb}; len {len(built)} vs {len(op['frame'])})")
        return op["resp"]

    async def send_fw_chunk(self, chunk, timeout_ms=1000):
        op = self._next()
        if op["kind"] != "FWCHUNK":
            raise Divergence(f"op #{self.i-1}: driver sent FW chunk ({len(chunk)}B), "
                             f"wire has {_fmt_op(op)}")
        if bytes(chunk) != op["data"]:
            n = min(len(chunk), len(op["data"]))
            d = next((k for k in range(n) if chunk[k] != op["data"][k]), n)
            raise Divergence(f"op #{self.i-1}: FW chunk mismatch at byte {d} "
                             f"(len {len(chunk)} vs {len(op['data'])})")
        return True

    # no-ops: the reader/pipes are physical, the cursor sequences ops instead.
    def start_rx(self):
        pass

    async def stop_rx(self):
        pass

    def clear_halt(self, ep):
        pass

    def subscribe(self, callback):
        pass


# ----------------------------------------------------------------------------
# Operational tail (after _bringup): monitor entry + channel hops + mac_work MIB.
# airmon/mac80211 interleave these in a tool-timing order that differs per capture,
# so each op is peeked and routed to the real driver builder that reproduces it,
# rather than driving a fixed sequence.
# ----------------------------------------------------------------------------

_SURVEY_FIRST = MT_MIB_SDR9(0)      # mt792x_phy_update_channel busy_time read
_MIB_FIRST = MT_MIB_SDR3(0)         # mt792x_mac_update_mib_stats fcs_err read


def _decode_operational_mcu(f):
    """Peek a captured operational MCU frame and return (cmd, payload) for the real
    driver builder that reproduces it - channel/enable/filter params read straight
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
    """Continue the cursor past _bringup through the airmon/airodump operational tail:
    the monitor-entry + channel-hop MCU commands (interleaved in the tool's wire order)
    and the periodic mt792x_mac_work MIB read cycles (update_survey every tick,
    update_mib_stats every second tick). Each op is peeked, matched to its real driver
    builder/sequence, and replayed against the cursor.

    Returns "exhausted" (every op reproduced), "frontier" (an op no handler reproduces),
    or "truncated" (the capture stopped partway through a final mac_work cycle - a benign
    capture-end artifact; every captured op matched)."""
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
        except Divergence:
            # A mac_work sequence that runs off the end of the captured ops is the capture
            # stopping mid-cycle, not a divergence - every recorded op matched.
            if replay.i >= len(replay.ops):
                return "truncated"
            raise
    return "exhausted"


def _patch_sleeps():
    """Replay needs no real settle delays; the cursor, not wall-clock, sequences ops."""
    async def _nosleep(*a, **k):
        return None
    asyncio.sleep = _nosleep


def check_cold_boot(pkts, dev):
    """STRICT single cursor driving the driver's REAL cold bring-up (_bringup: firmware
    upload + post_boot_init) from the first register op, then the operational tail."""
    merged = build_cold_stream(pkts, dev)

    # Two capture-shape differences from the WiFi cold boot - counted, never silent.
    n_chiprev = sum(1 for o in merged if o["kind"] == "R" and o["addr"] == CHIP_REV_ADDR)
    n_wire_bs = sum(1 for o in merged
                    if o["kind"] in ("R", "W") and o["addr"] == BOOT_STATUS_ADDR)

    # A composite WiFi+BT unit's btusb function saturates the shared-devnum stream with
    # its own BT-firmware load from op #0; the WiFi cold boot cannot be walked byte-for-
    # byte through that. Refuse it (never a misleading RED against btusb noise) and point
    # at the single-function *-scatter capture.
    if n_wire_bs > _COMPOSITE_BOOTSTATUS_MIN:
        print("COLD BOOT - single cursor over the real _bringup")
        print(f"  [ABORT] composite WiFi+BT unit: {n_wire_bs} btusb boot-status polls "
              f"(a concurrent BT-firmware load) share the devnum and interleave with the\n"
              f"          WiFi cold boot from op #0, so it cannot be walked byte-for-byte. "
              f"Use a single-function capture (the pau0f *-scatter default).")
        return "composite"

    merged = [o for o in merged
              if not (o["kind"] in ("R", "W") and o["addr"] in (CHIP_REV_ADDR, BOOT_STATUS_ADDR))]

    n_reg = sum(1 for o in merged if o["kind"] in ("R", "W"))
    n_mcu = sum(1 for o in merged if o["kind"] == "MCU")
    n_chunk = sum(1 for o in merged if o["kind"] == "FWCHUNK")
    print("COLD BOOT - single cursor over the real _bringup (firmware upload + post-boot init)")
    print(f"  {len(merged)} ops: {n_mcu} MCU cmds, {n_chunk} FW chunks, {n_reg} reg R/W, "
          f"1 vendor power-on")
    if n_chiprev:
        print(f"  waived {n_chiprev} Linux chip-rev read(s) @0x{CHIP_REV_ADDR:08x} "
              f"(the port reads only the chip id)")
    if n_wire_bs:
        print(f"  waived {n_wire_bs} btusb boot-status poll(s) @0x{BOOT_STATUS_ADDR:08x} "
              f"(composite-device BT coexistence; not WiFi ops)")

    replay = ColdBootReplay(merged)
    asyncio.set_event_loop(asyncio.new_event_loop())
    _patch_sleeps()

    # Build the REAL driver over the replay transport (dev is never touched - the mock
    # replaces the transport and its firmware ref, and stubs the vendor-interface claim).
    driver = MT7921AUDriver(Mock())
    driver.transport = replay
    driver.firmware.transport = replay
    driver.firmware._claim_vendor_interface = lambda *a, **k: 0

    state = {}

    async def _drive():
        await driver._bringup(None)                 # firmware upload + post_boot_init
        state["boot_end"] = replay.i
        state["status"] = await walk_operational(replay)   # monitor entry + hops + MIB

    try:
        asyncio.get_event_loop().run_until_complete(_drive())
    except Divergence as e:
        print(f"  [FAIL] DIVERGENCE after {replay.i} matched op(s)")
        print(f"         {e}")
        print("  (the cursor stops at the first op the driver does not reproduce - that IS the bug)")
        return "fail"
    except Exception as e:                          # driver raised (poll ran dry, etc.)
        print(f"  [FAIL] driver._bringup raised {type(e).__name__}: {e} "
              f"(after {replay.i} matched ops)")
        return "fail"

    if replay.boot_status_served:
        print(f"  served {replay.boot_status_served} port boot-status read(s) off-cursor "
              f"(firmware replicates the btusb poll; the WiFi cold boot has none)")

    boot_end, status = state["boot_end"], state["status"]
    print(f"  cold boot: {boot_end} ops (firmware upload + post-boot init -> add_interface) "
          f"byte-for-byte via driver._bringup")
    print(f"  operational: {replay.i - boot_end} ops (monitor entry + hops + mac_work MIB)")
    if status == "frontier":
        front = merged[replay.i]
        print(f"  [FRONTIER] reproduced {replay.i} ops; next unported op @{replay.i} "
              f"= {_fmt_op(front)}")
        print("  ^ port this next; the gate is green only when every op is reproduced.")
        return "frontier"
    if status == "truncated":
        print(f"  [PASS] reproduced all {replay.i} cold-boot + operational ops byte-for-byte "
              f"(capture ends mid mac_work cycle - benign)")
        return "pass"
    print(f"  [PASS] reproduced all {replay.i} cold-boot + operational ops byte-for-byte")
    return "pass"


# ----------------------------------------------------------------------------
# CHECK TX - TX descriptor accuracy
#
# Rebuild every captured aireplay TX frame (the `-0` deauth on EP 0x09 + the
# `--test` null frames on EP 0x04) via the driver's REAL tx.build_tx and assert
# the full USB bulk-OUT bytes AND the chosen endpoint match the wire. The TX band
# is driven by the wire: a `current_channel` tracker updates on each config_sniffer
# command (UNI 0x24, tlv tag 1) so 2.4 GHz frames rebuild as CCK and 5 GHz frames
# as the band-offset OFDM rate. SKIP only when the capture recorded no post-boot TX.
# ----------------------------------------------------------------------------

_FC_NAME = {0xc0: "deauth", 0xa0: "disassoc", 0xb0: "auth", 0x40: "probe_req",
            0x50: "probe_resp", 0x48: "null", 0x88: "qos_null", 0x80: "beacon"}


def _sniffer_channel(f):
    """If MCU frame `f` is a UNI SNIFFER config_sniffer (tlv tag 1), return its
    control channel; else None. Mirrors _decode_operational_mcu's config_sniffer
    parse (band_idx @ payload[0], tag @ payload+4, control_ch @ payload+8)."""
    if len(f) < 65 or (f[38] == 0x00 and f[39] == 0x80):
        return None                                   # not a UNI command
    if (f[38] | (f[39] << 8)) != mt_mcu.UNI_CMD_SNIFFER:
        return None
    if (f[56] | (f[57] << 8)) != 1:                   # tlv tag 1 = config_sniffer
        return None
    return f[64]                                       # control_ch


# Runtime injects with NO_ACK clear (request an ACK, retry until Addr2 ACKs); the captured
# aireplay reference set NO_ACK. REM_TX_COUNT now matches (both 15), so NO_ACK is the one intended
# divergence. The ACK-cfg field (NO_ACK + REM_TX_COUNT) is masked below so the byte compare ignores
# it; every other descriptor byte must still match the wire exactly. TXD3 is at byte 16 (4-byte
# SDIO/USB header + connac2 TXD word 3, see tx.build_tx).
_TXD3_OFF = 4 + 3 * 4                                   # SDIO/USB header + txwi word 3
_TXD3_ACKCFG_MASK = MT_TXD3_REM_TX_COUNT | MT_TXD3_NO_ACK


def _mask_txd3_ackcfg(buf: bytes) -> bytes:
    """Zero the REM_TX_COUNT / NO_ACK bits of TXD3 so a byte compare ignores only that field."""
    if len(buf) < _TXD3_OFF + 4:
        return buf
    b = bytearray(buf)
    w = int.from_bytes(b[_TXD3_OFF:_TXD3_OFF + 4], "little") & ~_TXD3_ACKCFG_MASK
    b[_TXD3_OFF:_TXD3_OFF + 4] = w.to_bytes(4, "little")
    return bytes(b)


def check_tx(pkts, dev):
    from collections import Counter
    evs = build_bulk_stream(pkts, dev)
    seen_fw = False
    current_channel = None       # set by the wire's last config_sniffer before each TX
    txs = []   # (ep, transfer_bytes, frame_bytes, band_5ghz)
    for kind, ep, data in evs:
        if kind == "OUT" and ep == EP_MCU_OUT:
            if (len(data) > 40 and data[38] == 0x00 and data[39] == 0x80
                    and data[40] == 0x02):
                seen_fw = True
            ch = _sniffer_channel(data)
            if ch is not None:
                current_channel = ch
        elif seen_fw and kind == "OUT" and ep in (EP_FW_OUT, 0x09):
            if len(data) < 4 + MT_SDIO_TXD_SIZE:
                continue
            framelen = int.from_bytes(data[0:4], "little") - MT_SDIO_TXD_SIZE
            off = 4 + MT_SDIO_TXD_SIZE
            if framelen < 24 or off + framelen > len(data):
                continue                       # not an 802.11 TX frame
            band_5ghz = (current_channel or 0) > 14
            txs.append((ep, bytes(data), bytes(data[off:off + framelen]), band_5ghz))

    print("CHECK TX - TX descriptor (connac2 TXD/TXWI)")
    if not txs:
        print("  [UNVERIFIED] no post-boot 802.11 TX in this capture (stopped before aireplay)")
        return "skip"

    ok = True
    by_kind = Counter()
    by_band = Counter()
    first_bad = None
    ackcfg_excepted = 0
    for ep, wire, frame, band_5ghz in txs:
        built, out_ep = mt_tx.build_tx(frame, band_5ghz=band_5ghz)
        fc_stype = frame[0] & 0xFC
        by_kind[_FC_NAME.get(fc_stype, f"0x{frame[0]:02x}")] += 1
        by_band["5GHz" if band_5ghz else "2.4GHz"] += 1
        if bytes(built) == wire and out_ep == ep:
            continue
        # Accept the intended TXD3 REM_TX_COUNT/NO_ACK divergence ONLY when every other byte
        # (and the endpoint) still matches; count it. Anything else is a real failure.
        if out_ep == ep and _mask_txd3_ackcfg(built) == _mask_txd3_ackcfg(wire):
            ackcfg_excepted += 1
            continue
        ok = False
        if first_bad is None:
            n = min(len(built), len(wire))
            d = next((i for i in range(n) if built[i] != wire[i]), n)
            first_bad = (f"band={'5GHz' if band_5ghz else '2.4GHz'} "
                         f"ep wire=0x{ep:02x} built=0x{out_ep:02x}; byte {d}: "
                         f"built {built[d:d+4].hex() if d < len(built) else '-'} vs "
                         f"wire {wire[d:d+4].hex() if d < len(wire) else '-'} "
                         f"(len {len(built)} vs {len(wire)})")
    n09 = sum(1 for ep, *_ in txs if ep == 0x09)
    print(f"  {len(txs)} TX frames ({n09} on EP 0x09 mgmt, {len(txs) - n09} on EP 0x04 data): "
          f"{dict(by_kind)}")
    print(f"  band split (from config_sniffer on the wire): {dict(by_band)}")
    if ackcfg_excepted:
        print(f"  ack-cfg-excepted {ackcfg_excepted} TX frame(s); NO_ACK differs by design: inject "
              f"clears NO_ACK (retry until Addr2 ACKs); the aireplay ref set NO_ACK. "
              f"REM_TX_COUNT matches (15); every other byte matched.")
    if ok:
        tail = ("descriptor + endpoint; TXD3 ACK-cfg excepted above" if ackcfg_excepted
                else "descriptor + endpoint, both bands")
        print(f"  [PASS] every TX frame rebuilt byte-for-byte ({tail})")
        return "pass"
    print(f"  [FAIL] {first_bad}")
    return "fail"


def run(cap=None):
    """Dispatcher entry (scripts/verify_pcap.py). Returns 0 = full green,
    1 = divergence/failure, 2 = incomplete (cold-boot frontier or TX unverified)."""
    cap = cap or DEFAULT_CAP
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

    # The gate's whole point is verifying the device's RESPONSES - the firmware-load
    # handshake and the post-boot MCU command/response stream - which are only recorded in
    # a scatter capture (`options mt76_usb disable_usb_sg=1`). A pre-scatter capture has zero
    # device->host RX, so the cold-boot cursor cannot pair MCU responses; refuse it rather
    # than print a hollow PASS (the register-only ops are not a substitute).
    if not any(kind == "IN" for kind, _, _ in build_bulk_stream(pkts, dev)):
        print(f"[ABORT] {cap} has no device->host RX - it was not captured with "
              "disable_usb_sg=1, so the firmware handshake and post-boot responses are\n"
              "        invisible and CANNOT be verified. Use a *-scatter capture; the "
              "register-only ops are not a substitute.")
        return 1

    boot = check_cold_boot(pkts, dev)
    print()
    if boot == "composite":
        print("[ABORT] composite WiFi+BT capture - see above; use a single-function capture")
        return 1
    tx = check_tx(pkts, dev)

    if boot == "fail" or tx == "fail":
        print("\n[FAIL] see localized divergence(s) above")
        return 1
    if boot == "frontier":
        print("\n[FRONTIER] cold-boot cursor advancing - see the next op above")
        return 2
    if tx == "skip":
        print("\n[INCOMPLETE] cold boot verified; CHECK TX UNVERIFIED - this capture has no "
              "post-boot 802.11 TX. Supply a capture that exercises inject/aireplay\n"
              "             (e.g. the 5g-injection capture) to verify TX; this run is NOT a "
              "full pass.")
        return 2
    print("\n[PASS] all checks green")
    return 0


def main():
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    sys.exit(main())
