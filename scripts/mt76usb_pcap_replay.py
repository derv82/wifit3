"""Replay-diff engine for the mt76-USB FAMILY (MediaTek MT76x2U / MT76x0U).

STRICT mode: this engine asserts that the port emits the EXACT USB op stream the kernel
recorded, in order, with a single monotonic cursor that stops at the first byte that
differs. Every MMIO vendor control op (reads INCLUDED, since MMIO reads on this silicon can
be read-to-clear / posted barriers) and every bulk-OUT frame is matched positionally against
the wire. The only device->host data fed back is the MCU response stream (EP 0x85), in
capture order; that is input the port consumes, not host output we verify.

Two opt-in accommodations exist for legitimate port-vs-kernel structure differences, both
principled (see ``ReplayDevice`` args):

  - ``start`` anchors the cursor past a port-specific PROLOGUE the capture does not share
    (e.g. a warm-reattach probe the kernel cold boot never issues). Like mt7921's PREFETCH0
    anchor: everything from the anchor on is still strict.
  - ``eeprom`` serves static EEPROM/efuse reads (breq 0x09) OFF-cursor from the values the
    capture recorded, so a port that reads the ROM lazily is served correctly wherever it
    reads. Safe precisely because a ROM is NOT read-to-clear: unlike MMIO, EEPROM reads are
    idempotent and order-independent. Every MMIO op stays strictly positional.

Absent those, a divergence at op #1 is a valid, honest result: the port's bring-up does
something the kernel cold boot doesn't from the very start. The gate localizes that first
difference exactly, and never papers over an MMIO divergence.

FAMILY wire format (mt76u, ``data_dumps/mt76-source-v6.18/usb.c``):

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


EEPROM_BREQ = 0x09          # MT_VEND_READ_EEPROM: the static-ROM read (idempotent)


def build_eeprom(host_ops: list[dict], breq: int = EEPROM_BREQ) -> bytes:
    """Flatten the capture's EEPROM/efuse reads into an offset-indexed byte buffer.

    The kernel slurps the whole EEPROM upfront; a port that reads the ROM lazily needs
    those values served wherever it reads them. Safe off-cursor because a ROM is not
    read-to-clear (see the module docstring)."""
    buf = bytearray()
    for o in host_ops:
        if (o["kind"] == "ctrl" and o["dir"] == "IN" and o.get("breq") == breq
                and o.get("data")):
            addr = (o["wval"] << 16) | o["widx"]
            end = addr + len(o["data"])
            if end > len(buf):
                buf.extend(b"\x00" * (end - len(buf)))
            buf[addr:end] = o["data"]
    return bytes(buf)


def anchor_index(host_ops: list[dict], breq: int, addr: int,
                 direction: str = "IN") -> int | None:
    """Index of the first ctrl op matching (direction, breq, addr) — where to start the
    strict cursor, skipping a port-specific prologue the capture does not share."""
    for i, o in enumerate(host_ops):
        if (o["kind"] == "ctrl" and o["dir"] == direction and o.get("breq") == breq
                and ((o["wval"] << 16) | o["widx"]) == addr):
            return i
    return None


class ReplayDevice:
    """A fake ``usb.core.Device``: ctrl_transfer / write / read walk the recorded op
    stream so the REAL chip transport drives it unchanged. STRICT: the first MMIO op that
    does not match — wrong direction, request, address, value, or endpoint — raises
    Divergence. MCU responses (EP 0x85) are fed back in capture order.

    ``start`` anchors the cursor past a port-specific prologue. ``eeprom`` (a byte buffer
    from ``build_eeprom``) serves breq-``EEPROM_BREQ`` reads off-cursor from the recorded
    ROM; all MMIO reads stay positional. Both are principled (see the module docstring)."""

    def __init__(self, host_ops: list[dict], responses: list[bytes], start: int = 0,
                 eeprom: bytes | None = None, rxfilter_addr: int | None = None,
                 rxfilter_mask: int = 0, wpdma_addr: int | None = None):
        self.ops = host_ops
        self.i = start
        self.responses = responses
        self.resp_i = 0
        self.eeprom = eeprom
        self.eeprom_served = 0
        # Monitor RX-filter accommodation: a port in monitor mode clears extra
        # drop bits (rxfilter_mask) for promiscuous RX, so a RX_FILTR_CFG write
        # (rxfilter_addr) that differs from the wire ONLY within those bits is
        # accepted. skip_configure_filter() then skips the kernel's separate
        # configure_filter ops the port folds in. Deliberate, documented.
        self.rxfilter_addr = rxfilter_addr
        self.rxfilter_mask = rxfilter_mask
        self.wpdma_addr = wpdma_addr
        self.rxfilter_masked = 0
        self.empty_reads_served = 0

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
        is_in = bool(bmRequestType & 0x80)
        # Static EEPROM/efuse read: serve off-cursor from the recorded ROM (idempotent,
        # order-independent — not a read-to-clear MMIO reg). Does not advance the cursor.
        if is_in and self.eeprom is not None and bRequest == EEPROM_BREQ:
            addr = (wValue << 16) | wIndex
            self.eeprom_served += 1
            return bytearray(self.eeprom[addr:addr + int(data_or_wLength)])
        op = self._next()
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
            if not op["data"]:
                # Capture gap: a MATCHED IN read (breq/addr/dir all checked above)
                # whose data stage was not recorded. Serve zeros so a poll loop
                # reads "not ready" and advances to the next recorded poll read,
                # keeping the cursor aligned. Not a skipped/patched-away op.
                self.empty_reads_served += 1
                n = int(data_or_wLength) if isinstance(data_or_wLength, int) else 4
                return bytearray(n)
            return bytearray(op["data"])
        payload = bytes(data_or_wLength) if data_or_wLength else b""
        if op["data"] != payload:
            # Monitor RX-filter write: accept a value differing only within the
            # monitor bits (the port opens the filter for promiscuous RX).
            if (self.rxfilter_addr is not None and addr == self.rxfilter_addr
                    and len(payload) == 4 and len(op["data"]) == 4
                    and ((int.from_bytes(payload, "little")
                          ^ int.from_bytes(op["data"], "little"))
                         & ~self.rxfilter_mask) == 0):
                self.rxfilter_masked += 1
                return len(payload)
            raise Divergence(
                f"op #{self.i-1}: WRITE 0x{addr:08x} value mismatch — port "
                f"{payload.hex() or '(none)'} vs wire {op['data'].hex() or '(none)'} "
                f"@f{op['frame']}")
        return len(payload)

    def skip_configure_filter(self) -> int:
        """Skip the kernel's separate configure_filter ops (a WPDMA read + one or
        more RX_FILTR_CFG writes) that the port folds into its monitor mac_start.
        Advances the cursor over that contiguous run; returns the count skipped."""
        skipped = 0
        while self.i < len(self.ops):
            op = self.ops[self.i]
            if op["kind"] != "ctrl":
                break
            addr = (op["wval"] << 16) | op["widx"]
            is_wpdma_rd = op["dir"] == "IN" and addr == self.wpdma_addr
            is_rxfilter_wr = op["dir"] == "OUT" and addr == self.rxfilter_addr
            if not (is_wpdma_rd or is_rxfilter_wr):
                break
            self.i += 1
            skipped += 1
        return skipped

    def skip_read(self, addr: int) -> bool:
        """Skip one wire read of ``addr`` if it is the next op (a survey/snapshot
        read the port does not issue). Returns True if it skipped one."""
        if self.i < len(self.ops):
            op = self.ops[self.i]
            if (op["kind"] == "ctrl" and op["dir"] == "IN"
                    and ((op["wval"] << 16) | op["widx"]) == addr):
                self.i += 1
                return True
        return False

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
