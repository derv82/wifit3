"""Replay-diff engine for the mt76-USB FAMILY (MediaTek MT76x2U / MT76x0U).

STRICT mode: this engine asserts that the port emits the EXACT USB op stream the kernel
recorded, in order, with a single monotonic cursor that stops at the first byte that
differs. There are no waivers, no off-cursor serving, no address maps — every vendor
control op (reads INCLUDED, since reads on this silicon can be read-to-clear / posted
barriers) and every bulk-OUT frame is matched positionally against the wire. The only
device->host data fed back is the MCU response stream (EP 0x85), in capture order; that is
input the port consumes, not host output we verify.

A divergence at op #1 is a valid, honest result: it means the port's bring-up does something
different from the kernel cold boot from the very start (a defensive read the kernel skips, a
live-EEPROM read where the kernel slurped upfront, a reordered write, ...). The gate's job is
to localize that first difference exactly — not to paper over it.

FAMILY wire format (mt76u, ``driver_sources/mt76-source-v6.18/usb.c``):

    bRequest 0x06 = MT_VEND_MULTI_WRITE   (bmRequestType 0x40, OUT, 4-byte data)
    bRequest 0x07 = MT_VEND_MULTI_READ    (bmRequestType 0xC0, IN,  4-byte data)
    bRequest 0x46/0x47 = WRITE_CFG/READ_CFG  (CFG-bus register access)
    bRequest 0x66/0x63 = WRITE_EXT/READ_EXT
    bRequest 0x42 = WRITE_FCE   (FW-DMA programming; value rides in wValue, no payload)
    bRequest 0x01 = MT_VEND_DEV_MODE   (FW reset / IVB trigger; value in wValue, no payload)
    bRequest 0x09 = MT_VEND_READ_EEPROM (IN; 4 bytes)
    bmRequestType 0x20 = class OUT (mt76x2u ROM-patch enable_patch / reset_wmt payloads)
    register ADDRESS encodes as wValue = addr>>16, wIndex = addr & 0xFFFF.

[WIRE] On the bus, bRequest 0x06 / 0x09 / 0x01 COLLIDE with the standard GET_DESCRIPTOR /
SET_CONFIGURATION / CLEAR_FEATURE requests issued during enumeration. They are told apart by
the bmRequestType *type* field: vendor = (bm & 0x60) == 0x40, class = 0x20; standard (0x00)
is enumeration noise and is dropped.

USB endpoints (mt76u; [SRC] mt76.h:632): bulk-OUT 0x08 (inband cmd + FW), 0x04/05/06/07/09
(AC queues / HCCA, TX); bulk-IN 0x84 (PKT_RX, device->host — ignored), 0x85 (CMD_RESP, MCU
responses — fed back in capture order).
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

# Vendor requests that carry a host-issued register/FW/EEPROM op (all positional).
VENDOR_BREQ = {0x06, 0x46, 0x66, 0x42, 0x01, 0x07, 0x47, 0x63, 0x09}

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


def extract(pkts: list[bytes], dev: int) -> dict:
    """Demux device ``dev`` into the strict cursor's structures:

      host_ops   ordered positional cursor: EVERY vendor ctrl op (reads + writes, EEPROM
                 included) + every bulk-OUT, in capture order. Each op:
                 {kind:'ctrl'|'bulk', dir:'IN'|'OUT', breq|ep, wval, widx, data(bytes), frame}
      responses  ordered EP-0x85 IN payloads (MCU responses), fed back in capture order.
    """
    host_ops: list[dict] = []
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
                    pending_rd[urb] = {"kind": "ctrl", "dir": "IN", "breq": breq,
                                       "wval": wval, "widx": widx, "wlen": wlen,
                                       "frame": frame_no}
                else:                           # OUT (write) — data on the submit
                    data = bytes(pkt[_OFF_DATA:_OFF_DATA + min(wlen, len(pkt) - _OFF_DATA)])
                    host_ops.append({"kind": "ctrl", "dir": "OUT", "breq": breq,
                                     "wval": wval, "widx": widx, "wlen": wlen,
                                     "data": data, "frame": frame_no})
            elif utype == _URB_COMPLETE:
                op = pending_rd.pop(urb, None)
                if op is None:
                    continue
                op["data"] = bytes(
                    pkt[_OFF_DATA:_OFF_DATA + min(op["wlen"], len(pkt) - _OFF_DATA)])
                host_ops.append(op)

        elif xfer == _XFER_BULK:
            lencap = struct.unpack_from("<I", pkt, _OFF_LENCAP)[0]
            data = bytes(pkt[_OFF_DATA:_OFF_DATA + min(lencap, len(pkt) - _OFF_DATA)])
            if utype == _URB_SUBMIT and not (ep & 0x80) and ep in EP_TX_OUT | {EP_MCU_OUT}:
                host_ops.append({"kind": "bulk", "dir": "OUT", "ep": ep, "data": data,
                                 "frame": frame_no})
            elif utype == _URB_COMPLETE and ep == EP_RESP_IN and lencap > 0:
                responses.append(data)

    return {"host_ops": host_ops, "responses": responses}


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
    stream so the REAL chip transport drives it unchanged. STRICT: the first op that does
    not match — wrong direction, request, address, value, or endpoint — raises Divergence.
    Nothing is served off-cursor; reads are positional (read-to-clear regs make that
    mandatory). MCU responses (EP 0x85) are fed back in capture order."""

    def __init__(self, host_ops: list[dict], responses: list[bytes], start: int = 0):
        self.ops = host_ops
        self.i = start
        self.responses = responses
        self.resp_i = 0

    def _next(self) -> dict:
        if self.i >= len(self.ops):
            raise Divergence(f"port issued an op past the end of the capture (op #{self.i})")
        op = self.ops[self.i]
        self.i += 1
        return op

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex,
                      data_or_wLength, timeout=None):
        op = self._next()
        is_in = bool(bmRequestType & 0x80)
        exp = "IN" if is_in else "OUT"
        addr = (wValue << 16) | wIndex
        if op["kind"] != "ctrl":
            raise Divergence(
                f"op #{self.i-1}: port {exp} ctrl breq=0x{bRequest:02x} 0x{addr:08x}, "
                f"wire has {fmt_op(op)}")
        op_addr = (op["wval"] << 16) | op["widx"]
        if op["dir"] != exp or op["breq"] != bRequest or op_addr != addr:
            raise Divergence(
                f"op #{self.i-1}: port {exp} breq=0x{bRequest:02x} 0x{addr:08x}, "
                f"wire has {fmt_op(op)}")
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
        return len(data)

    def read(self, ep, length, timeout=None):
        if ep != EP_RESP_IN:
            raise Divergence(f"port read unexpected IN ep=0x{ep:02x}")
        if self.resp_i >= len(self.responses):
            raise Divergence("port awaited an EP-0x85 response the wire never carried")
        r = self.responses[self.resp_i]
        self.resp_i += 1
        return bytearray(r)
