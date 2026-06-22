"""Replay-diff engine for the mt76-USB FAMILY (MediaTek MT76x2U / MT76x0U).

Reconstructs the ordered USB conversation an mt76-USB driver had with the chip from a
cold-boot capture, then lets a port drive its real bring-up against a fake usb.core.Device
that replays the chip's recorded reads/responses and byte-checks every write. No hardware,
no tshark — the pcapng is parsed in-process.

FAMILY wire format (mt76u, ``data_dumps/mt76-source-v6.18/usb.c``; mirrors the Ralink
``rt2x00_pcap_replay`` 0x06/0x07 split but the register semantics are mt76's):

    bRequest 0x06 = MT_VEND_MULTI_WRITE   (bmRequestType 0x40, OUT, 4-byte data)
    bRequest 0x07 = MT_VEND_MULTI_READ    (bmRequestType 0xC0, IN,  4-byte data)
    bRequest 0x46/0x47 = WRITE_CFG/READ_CFG  (CFG-bus register access)
    bRequest 0x66/0x63 = WRITE_EXT/READ_EXT
    bRequest 0x42 = WRITE_FCE   (FW-DMA programming; value rides in wValue, no payload)
    bRequest 0x01 = MT_VEND_DEV_MODE   (FW reset / IVB trigger; value in wValue, no payload)
    bRequest 0x09 = MT_VEND_READ_EEPROM (IN; 4 bytes; see EEPROM note below)
    bmRequestType 0x20 = class OUT (mt76x2u ROM-patch enable_patch / reset_wmt payloads)
    register ADDRESS encodes as wValue = addr>>16, wIndex = addr & 0xFFFF.

[WIRE] On the bus, bRequest 0x06 / 0x09 / 0x01 COLLIDE with the standard GET_DESCRIPTOR /
SET_CONFIGURATION / CLEAR_FEATURE requests issued during enumeration. They are told apart by
the bmRequestType *type* field: vendor = (bm & 0x60) == 0x40, class = 0x20; standard (0x00)
is enumeration noise and is dropped.

EEPROM (bRequest 0x09) is special. The kernel slurps the whole 512-byte EEPROM into a buffer
once at probe (the 128 contiguous 0x09 reads seen on the wire) and every later EEPROM field
read hits that cache — it issues NO further control transfer. A faithful wifit3 port instead
reads EEPROM fields *live* at the moment it needs them, so its 0x09 reads land at different
points than the kernel's upfront slurp. We therefore serve EEPROM reads from an ADDRESS MAP
built from all the wire's 0x09 reads, OUT of the positional cursor — the EEPROM content is
what matters, not when it is read. Every other read (live register state, poll loops) stays
positional.

USB endpoints (mt76u; [SRC] mt76.h:632): bulk-OUT 0x08 (inband cmd + FW), 0x04/05/06/07/09
(AC queues / HCCA, TX); bulk-IN 0x84 (PKT_RX, device->host — ignored), 0x85 (CMD_RESP, MCU
responses — served by seq). MCU command/response pairing is by the 4-bit seq the driver
stamps into the EP-0x08 TXINFO (bits 19:16); read(0x85) returns the next recorded response
carrying that seq, so the port's wait_resp loop matches on its first read.
"""
from __future__ import annotations

import struct
from collections import Counter
from pathlib import Path

# usbmon mon_bin record offsets ([WIRE], confirmed against the captures).
_OFF_TYPE, _OFF_XFER, _OFF_EP, _OFF_DEV = 8, 9, 10, 11
_OFF_LENCAP = 36
_OFF_SETUP = 40        # bmReq@40 bReq@41 wValue@42 wIndex@44 wLength@46
_OFF_DATA = 64

_URB_SUBMIT, _URB_COMPLETE = 0x53, 0x43
_XFER_CTRL, _XFER_BULK = 0x02, 0x03

# Vendor requests that carry register/FW ops (NOT EEPROM, which is mapped separately).
REG_WRITE_BREQ = {0x06, 0x46, 0x66, 0x42, 0x01}   # MULTI/CFG/EXT/FCE write + DEV_MODE
REG_READ_BREQ = {0x07, 0x47, 0x63}                 # MULTI/CFG/EXT read
EEPROM_BREQ = 0x09
CLASS_BREQ = 0x01                                   # bmReq 0x20 ROM-patch payloads

EP_MCU_OUT = 0x08
EP_RESP_IN = 0x85
EP_RX_IN = 0x84
EP_TX_OUT = {0x04, 0x05, 0x06, 0x07, 0x09}


class Divergence(AssertionError):
    """Raised at the first op the port does not reproduce byte-for-byte."""


def parse_pcapng(path: str) -> list[bytes]:
    """Extract Enhanced-Packet-Block payloads (the usbmon records) from a pcapng."""
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:                 # Enhanced Packet Block
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def _is_vendor(bm: int) -> bool:
    return (bm & 0x60) == 0x40


def _is_class(bm: int) -> bool:
    return (bm & 0x60) == 0x20


def detect_card(pkts: list[bytes]) -> int | None:
    """The card is the device issuing the most vendor MULTI_WRITE/READ (0x06/0x07)
    control transfers — robust against the per-plug-in devnum shuffle."""
    counts: Counter = Counter()
    for pkt in pkts:
        if len(pkt) < 48 or pkt[_OFF_XFER] != _XFER_CTRL or pkt[_OFF_TYPE] != _URB_SUBMIT:
            continue
        bm, breq = pkt[_OFF_SETUP], pkt[_OFF_SETUP + 1]
        if _is_vendor(bm) and breq in (0x06, 0x07):
            counts[pkt[_OFF_DEV]] += 1
    return counts.most_common(1)[0][0] if counts else None


def _mcu_seq(frame: bytes) -> int:
    """The 4-bit CMD_SEQ the driver stamped into an EP-0x08 TXINFO (bits 19:16)."""
    if len(frame) < 4:
        return 0
    return (struct.unpack_from("<I", frame, 0)[0] >> 16) & 0xF


def extract(pkts: list[bytes], dev: int) -> dict:
    """Demux device ``dev`` into the structures the ReplayDevice needs:

      host_ops   ordered positional cursor: vendor ctrl R/W (minus EEPROM) + bulk-OUT.
                 Each op: {kind:'ctrl'|'bulk', dir:'IN'|'OUT', breq|ep, wval, widx,
                           data(bytes), seq(0x08 only), frame}
      eeprom     {addr -> 4 bytes}, built from every 0x09 read (served by address).
      responses  ordered list of EP-0x85 IN payloads (MCU responses, served by seq).
    """
    host_ops: list[dict] = []
    eeprom: dict[int, bytes] = {}
    responses: list[bytes] = []
    pending_rd: dict[bytes, dict] = {}      # urb_id -> read op awaiting its completion
    frame_no = 0

    for pkt in pkts:
        frame_no += 1
        if len(pkt) < 48 or pkt[_OFF_DEV] != dev:
            continue
        utype, xfer, ep = pkt[_OFF_TYPE], pkt[_OFF_XFER], pkt[_OFF_EP]
        urb = bytes(pkt[0:8])

        if xfer == _XFER_CTRL:
            if utype == _URB_SUBMIT:
                bm, breq = pkt[_OFF_SETUP], pkt[_OFF_SETUP + 1]
                wval, widx, wlen = struct.unpack_from("<HHH", pkt, _OFF_SETUP + 2)
                if not (_is_vendor(bm) or _is_class(bm)):
                    continue                    # standard enumeration request — drop
                if bm & 0x80:                   # IN (read) — value on the completion
                    op = {"kind": "ctrl", "dir": "IN", "breq": breq, "wval": wval,
                          "widx": widx, "wlen": wlen, "frame": frame_no}
                    pending_rd[urb] = op
                else:                           # OUT (write) — data on the submit
                    data = bytes(pkt[_OFF_DATA:_OFF_DATA + min(wlen, len(pkt) - _OFF_DATA)])
                    host_ops.append({"kind": "ctrl", "dir": "OUT", "breq": breq,
                                     "wval": wval, "widx": widx, "wlen": wlen,
                                     "data": data, "frame": frame_no})
            elif utype == _URB_COMPLETE:
                op = pending_rd.pop(urb, None)
                if op is None:
                    continue
                data = bytes(pkt[_OFF_DATA:_OFF_DATA + min(op["wlen"], len(pkt) - _OFF_DATA)])
                op["data"] = data
                addr = (op["wval"] << 16) | op["widx"]
                if op["breq"] == EEPROM_BREQ:   # static EEPROM slurp — address map
                    eeprom[addr] = data
                else:
                    host_ops.append(op)

        elif xfer == _XFER_BULK:
            lencap = struct.unpack_from("<I", pkt, _OFF_LENCAP)[0]
            data = bytes(pkt[_OFF_DATA:_OFF_DATA + min(lencap, len(pkt) - _OFF_DATA)])
            if utype == _URB_SUBMIT and not (ep & 0x80) and ep in EP_TX_OUT | {EP_MCU_OUT}:
                host_ops.append({"kind": "bulk", "dir": "OUT", "ep": ep, "data": data,
                                 "seq": _mcu_seq(data), "frame": frame_no})
            elif utype == _URB_COMPLETE and ep == EP_RESP_IN and lencap > 0:
                responses.append(data)

    return {"host_ops": host_ops, "eeprom": eeprom, "responses": responses}


def find_anchor(ops: list[dict], pred) -> int | None:
    """Index of the first op satisfying ``pred`` (used to start an anchored block)."""
    return next((i for i, o in enumerate(ops) if pred(o)), None)


def fmt_op(op: dict) -> str:
    if op["kind"] == "bulk":
        return f"bulk-OUT ep=0x{op['ep']:02x} ({len(op['data'])}B) @f{op['frame']}"
    addr = (op["wval"] << 16) | op["widx"]
    if op["dir"] == "IN":
        return f"ctrl-RD breq=0x{op['breq']:02x} 0x{addr:08x} @f{op['frame']}"
    val = op["data"][:4].hex() if op["data"] else "(no data)"
    return f"ctrl-WR breq=0x{op['breq']:02x} 0x{addr:08x}={val} @f{op['frame']}"


class ReplayDevice:
    """A fake ``usb.core.Device``: ctrl_transfer / write / read walk the recorded op
    stream so the REAL chip transport drives it unchanged. First mismatch -> Divergence.

    The positional cursor (``host_ops[start:]``) covers vendor register R/W + bulk-OUT.
    EEPROM reads are served by address from ``eeprom`` (off-cursor). MCU responses are
    served by seq from ``responses``."""

    def __init__(self, host_ops: list[dict], eeprom: dict, responses: list[bytes],
                 start: int = 0, extra_reads: dict | None = None,
                 extra_reads_limit: int = 1):
        self.ops = host_ops
        self.i = start
        self.eeprom = eeprom
        self.responses = responses
        self.resp_i = 0
        self._last_cmd_seq = 0
        # {addr -> u32} served off-cursor for the first ``extra_reads_limit`` driver
        # reads of that addr that the kernel cold-boot wire never made (e.g. an
        # idempotency poll the kernel skips); later reads of the same addr fall through
        # to the positional cursor (e.g. the post-upload poll IS on the wire). Counted
        # in ``extra_hits`` so the caller can name + print them.
        self.extra_reads = extra_reads or {}
        self.extra_reads_limit = extra_reads_limit
        self.extra_hits: Counter = Counter()

    # -- positional cursor ----------------------------------------------------
    def _next(self) -> dict:
        if self.i >= len(self.ops):
            raise Divergence(f"port issued an op past the end of the capture (op #{self.i})")
        op = self.ops[self.i]
        self.i += 1
        return op

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    # -- usb.core.Device surface ---------------------------------------------
    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex,
                      data_or_wLength, timeout=None):
        is_in = bool(bmRequestType & 0x80)
        if is_in and bRequest == EEPROM_BREQ:        # static EEPROM — off-cursor map
            addr = (wValue << 16) | wIndex
            if addr not in self.eeprom:
                raise Divergence(f"EEPROM read 0x{addr:08x} absent from the capture")
            return bytearray(self.eeprom[addr])
        if is_in:                                    # driver-extra reads — off-cursor
            addr = (wValue << 16) | wIndex
            if addr in self.extra_reads and self.extra_hits[addr] < self.extra_reads_limit:
                self.extra_hits[addr] += 1
                return bytearray(struct.pack("<I", self.extra_reads[addr] & 0xFFFFFFFF))
        op = self._next()
        if op["kind"] != "ctrl":
            raise Divergence(f"op #{self.i-1}: port did a ctrl xfer, wire has {fmt_op(op)}")
        exp = "IN" if is_in else "OUT"
        addr = (wValue << 16) | wIndex
        op_addr = (op["wval"] << 16) | op["widx"]
        if op["dir"] != exp or op["breq"] != bRequest or op_addr != addr:
            want = (f"{exp} breq=0x{bRequest:02x} 0x{addr:08x}")
            raise Divergence(f"op #{self.i-1}: port {want}, wire has {fmt_op(op)}")
        if is_in:
            return bytearray(op["data"])
        payload = bytes(data_or_wLength) if data_or_wLength else b""
        if op["data"] != payload:
            raise Divergence(
                f"op #{self.i-1}: WRITE 0x{addr:08x} value mismatch — port "
                f"{payload.hex() or '(none)'} vs wire {op['data'].hex() or '(none)'} "
                f"@f{op['frame']}")
        return len(payload)

    def write(self, ep, data, timeout=None):
        op = self._next()
        data = bytes(data)
        if op["kind"] != "bulk" or op["dir"] != "OUT" or op["ep"] != ep:
            raise Divergence(f"op #{self.i-1}: port bulk-OUT ep=0x{ep:02x}, "
                             f"wire has {fmt_op(op)}")
        if op["data"] != data:
            n = min(len(op["data"]), len(data))
            d = next((k for k in range(n) if op["data"][k] != data[k]), n)
            pb = data[d:d+4].hex() if d < len(data) else "-"
            wb = op["data"][d:d+4].hex() if d < len(op["data"]) else "-"
            raise Divergence(
                f"op #{self.i-1}: bulk-OUT ep=0x{ep:02x} mismatch at byte {d} — "
                f"port {pb} vs wire {wb} (len {len(data)} vs {len(op['data'])}) "
                f"@f{op['frame']}")
        if ep == EP_MCU_OUT:
            self._last_cmd_seq = _mcu_seq(data)
        return len(data)

    def read(self, ep, length, timeout=None):
        if ep != EP_RESP_IN:
            raise Divergence(f"port read unexpected IN ep=0x{ep:02x}")
        # Serve the next recorded response carrying the last command's seq, so the
        # port's wait_resp loop matches on its first read.
        want = self._last_cmd_seq
        j = self.resp_i
        while j < len(self.responses):
            r = self.responses[j]
            got = (struct.unpack_from("<I", r, 0)[0] >> 16) & 0xF if len(r) >= 4 else -1
            if got == want:
                self.resp_i = j + 1
                return bytearray(r)
            j += 1
        raise Divergence(f"no recorded EP-0x85 response with seq={want} "
                         f"(port awaited a response the wire never carried)")
