"""Single-cursor replay engine for the mt76-USB family (connac MT7921AU/MT7925U and the
MULTI_WRITE MT76x0U/MT76x2U). Drives the real port bring-up over a captured usbmon op
stream and reports which ops matched, which were waived by name, and the first that did
not. Per-chip wiring lives in each chip's scripts/<chip>/verify_pcap.py.
"""
from __future__ import annotations

import struct
from collections import Counter, namedtuple
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


class UsbmonOff(IntEnum):
    """Byte offsets into a usbmon mon_bin record."""
    TYPE = 8       # 0x53 'S' submit / 0x43 'C' complete
    XFER = 9       # 0x02 control / 0x03 bulk
    EP = 10        # endpoint (bulk); bit 7 = IN
    DEV = 11       # devnum
    LEN_CAP = 36   # captured data length
    SETUP = 40     # bmRequestType@40 bRequest@41 wValue@42 wIndex@44 wLength@46
    DATA = 64      # payload


# A record must reach through the 8-byte setup packet (ends at SETUP+8) to be parseable.
_MIN_RECORD = int(UsbmonOff.SETUP) + 8

_URB_SUBMIT, _URB_COMPLETE = 0x53, 0x43
_XFER_CTRL, _XFER_BULK = 0x02, 0x03


class Divergence(AssertionError):
    """Raised at the first op the port does not reproduce byte-for-byte. Carries the op
    index so the Walk can render a trace around it."""

    def __init__(self, msg: str, op_index: int | None = None):
        super().__init__(msg)
        self.op_index = op_index


def reqtype(bm: int) -> str:
    """USB bmRequestType type field to 'standard' | 'class' | 'vendor' | 'reserved'."""
    return {0x00: "standard", 0x20: "class", 0x40: "vendor", 0x60: "reserved"}[bm & 0x60]


def parse_pcapng(path: str) -> list[bytes]:
    """Enhanced-Packet-Block payloads (the usbmon records) from a pcapng file."""
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:                  # Enhanced Packet Block
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def busiest_vendor_devnum(pkts: list[bytes]) -> int | None:
    """Devnum issuing the most vendor control transfers (the wifi device), or None. Works
    for both mt76 register dialects; enumeration requests are excluded so a hub can't win."""
    counts: Counter = Counter()
    for pkt in pkts:
        if len(pkt) < _MIN_RECORD or pkt[UsbmonOff.XFER] != _XFER_CTRL \
                or pkt[UsbmonOff.TYPE] != _URB_SUBMIT:
            continue
        if reqtype(pkt[UsbmonOff.SETUP]) == "vendor":
            counts[pkt[UsbmonOff.DEV]] += 1
    return counts.most_common(1)[0][0] if counts else None


@dataclass
class Op:
    """One host-to-device op the port must reproduce: a control read/write or a bulk-OUT.
    `data` is the write/bulk payload, or the served completion bytes for a read."""
    idx: int                 # position in the host-to-device stream
    frame: int               # pcap frame number (1-based)
    cls: str                 # 'ctrl' | 'bulk'
    reqtype: str = ""        # ctrl: 'standard' | 'class' | 'vendor' | 'reserved'
    is_in: bool = False      # ctrl: read (IN) vs write (OUT)
    bm: int = 0
    breq: int = 0
    wval: int = 0
    widx: int = 0
    wlen: int = 0
    ep: int = 0              # bulk endpoint
    data: bytes = b""

    @property
    def addr(self) -> int:
        return (self.wval << 16) | self.widx      # wValue = addr>>16, wIndex = addr & 0xFFFF

    def fmt(self) -> str:
        """One-line rendering for reports and traces."""
        if self.cls == "bulk":
            return (f"bulk-OUT ep=0x{self.ep:02x} "
                    f"({len(self.data)}B {self.data[:8].hex()}...) @f{self.frame}")
        arrow = "RD" if self.is_in else "WR"
        val = "" if self.is_in else (f"={self.data[:8].hex()}" if self.data else "=()")
        return (f"ctrl-{arrow} [{self.reqtype[:4]}] breq=0x{self.breq:02x} "
                f"0x{self.addr:08x}{val} @f{self.frame}")


@dataclass
class Capture:
    """The op stream and device-to-host responses from one pcap. Responses feed the port's
    read path as input; only the host-to-device ops are checked against the port."""
    dev: int
    ops: list[Op]
    responses: list[bytes] = field(default_factory=list)


def extract(pkts: list[bytes], dev: int) -> Capture:
    """Demux device `dev`: control reads/writes and bulk-OUTs become the ordered op stream;
    device-to-host bulk completions become `responses`."""
    ops: list[Op] = []
    responses: list[bytes] = []
    pending_rd: dict[bytes, Op] = {}
    frame_no = 0
    idx = 0

    for pkt in pkts:
        frame_no += 1
        if len(pkt) < _MIN_RECORD or pkt[UsbmonOff.DEV] != dev:
            continue
        utype, xfer, ep = pkt[UsbmonOff.TYPE], pkt[UsbmonOff.XFER], pkt[UsbmonOff.EP]
        urb = bytes(pkt[0:8])

        if xfer == _XFER_CTRL:
            if utype == _URB_SUBMIT:
                bm, breq = pkt[UsbmonOff.SETUP], pkt[UsbmonOff.SETUP + 1]
                wval, widx, wlen = struct.unpack_from("<HHH", pkt, UsbmonOff.SETUP + 2)
                op = Op(idx=idx, frame=frame_no, cls="ctrl", reqtype=reqtype(bm),
                        is_in=bool(bm & 0x80), bm=bm, breq=breq,
                        wval=wval, widx=widx, wlen=wlen)
                idx += 1
                if op.is_in:                     # value arrives on the COMPLETE record
                    pending_rd[urb] = op
                    ops.append(op)               # data filled in below
                else:
                    dlen = min(wlen, len(pkt) - UsbmonOff.DATA)
                    op.data = bytes(pkt[UsbmonOff.DATA:UsbmonOff.DATA + dlen]) if dlen > 0 else b""
                    ops.append(op)
            elif utype == _URB_COMPLETE:
                op = pending_rd.pop(urb, None)
                if op is not None:
                    dlen = min(op.wlen, len(pkt) - UsbmonOff.DATA)
                    op.data = bytes(pkt[UsbmonOff.DATA:UsbmonOff.DATA + dlen]) if dlen > 0 else b""

        elif xfer == _XFER_BULK:
            lencap = struct.unpack_from("<I", pkt, UsbmonOff.LEN_CAP)[0]
            data = bytes(pkt[UsbmonOff.DATA:UsbmonOff.DATA + min(lencap, len(pkt) - UsbmonOff.DATA)])
            if utype == _URB_SUBMIT and not (ep & 0x80):
                ops.append(Op(idx=idx, frame=frame_no, cls="bulk", ep=ep, data=data))
                idx += 1
            elif utype == _URB_COMPLETE and (ep & 0x80) and lencap > 0:
                responses.append(data)

    return Capture(dev=dev, ops=ops, responses=responses)


# What the port asked for, when the wire op at the cursor did not match it. A substitution
# or extra waiver decides whether that difference is a known one.
PortCall = namedtuple("PortCall", "is_bulk is_in breq addr payload ep")


@dataclass
class Waiver:
    """One named reason an op is not a plain wire match. Sets exactly one predicate: `match`
    skips a wire op the port never emits, `sub` accepts a deliberate port divergence at this
    op, `extra` covers a port op the wire has no counterpart for. All count against 100%."""
    name: str
    why: str
    match: "callable | None" = None       # match(op) -> bool
    sub: "callable | None" = None         # sub(port, op) -> bool
    extra: "callable | None" = None       # extra(port) -> bool


class WaiverSet:
    """Ordered Waivers. first_match / first_sub / first_extra return the covering waiver of
    each kind, first one wins."""

    def __init__(self, *waivers: Waiver):
        self.waivers = list(waivers)

    def add(self, waiver: Waiver) -> "WaiverSet":
        self.waivers.append(waiver)
        return self

    def first_match(self, op: Op) -> Waiver | None:
        return next((w for w in self.waivers if w.match is not None and w.match(op)), None)

    def first_sub(self, port: PortCall, op: Op) -> Waiver | None:
        return next((w for w in self.waivers if w.sub is not None and w.sub(port, op)), None)

    def first_extra(self, port: PortCall) -> Waiver | None:
        return next((w for w in self.waivers if w.extra is not None and w.extra(port)), None)


class _FakeInterface:
    """Stand-in for the vendor interface the firmware loader claims."""

    def __init__(self, num: int):
        self.bInterfaceClass = 0xFF
        self.bInterfaceNumber = num


class ReplayDevice:
    """Stand-in for usb.core.Device over a slice of the op stream, starting at absolute
    index `base`. The real transport drives it; each call must match the op at the cursor,
    or a waiver must cover the difference, or it raises Divergence."""

    def __init__(self, ops: list[Op], responses: list[bytes] | None = None, base: int = 0,
                 waivers: WaiverSet | None = None):
        self.ops = ops
        self.i = 0                 # local cursor: every consumed op, matched or waived
        self.base = base
        self.responses = responses or []
        self.resp_i = 0
        self.waivers = waivers
        self.matched = 0           # ops matched by the port
        self.waived: list[tuple] = []   # (Waiver, Op) skipped or substituted mid-handler
        self.extra: list[tuple] = []    # (Waiver, PortCall) port emitted, no wire op
        self.async_interleave = None    # hook draining a separate producer's spliced ops
        self._in_interleave = False

    def get_active_configuration(self):
        return [_FakeInterface(0)]

    def is_kernel_driver_active(self, n):
        return False

    def detach_kernel_driver(self, n):
        pass

    def clear_halt(self, ep):
        pass

    def set_configuration(self, *a):
        pass

    def _next(self) -> Op:
        """Next op the port must match, consuming and booking any SKIP-waived ops before it."""
        while self.i < len(self.ops):
            op = self.ops[self.i]
            w = self.waivers.first_match(op) if self.waivers is not None else None
            if w is None:
                self.i += 1
                return op
            self.waived.append((w, op))
            self.i += 1
        raise Divergence(f"port issued an op past the end of the walk (local op #{self.i})",
                         self.base + self.i)

    def peek(self) -> Op | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def _next_matchable(self) -> Op | None:
        """The op `_next` would return, without consuming or booking anything."""
        j = self.i
        while j < len(self.ops):
            op = self.ops[j]
            if not (self.waivers is not None and self.waivers.first_match(op)):
                return op
            j += 1
        return None

    @staticmethod
    def _wire_matches(op: Op | None, port: PortCall) -> bool:
        if op is None:
            return False
        if port.is_bulk:
            return op.cls == "bulk" and op.ep == port.ep and op.data == port.payload
        return (op.cls == "ctrl" and op.is_in == port.is_in and op.breq == port.breq
                and op.addr == port.addr and (port.is_in or op.data == port.payload))

    def _try_sub(self, port: PortCall, op: Op) -> Waiver | None:
        """Book a SUBSTITUTE waiver if one accepts this (port, wire) divergence."""
        w = self.waivers.first_sub(port, op) if self.waivers is not None else None
        if w is not None:
            self.waived.append((w, op))
        return w

    def _maybe_extra(self, port: PortCall) -> bool:
        """Book an EXTRA waiver when the port emits an op the wire has no match for here, and
        consume no wire op. Checked against the real upcoming op so it can't mask a match."""
        if self.waivers is None or self._wire_matches(self._next_matchable(), port):
            return False
        w = self.waivers.first_extra(port)
        if w is None:
            return False
        self.extra.append((w, port))
        return True

    def _drain_async(self, port: PortCall) -> None:
        """Let the async-producer hook drain ops spliced into a mid-flight handler so the next
        port op lines up. Re-entrancy guarded: the hook itself issues USB calls."""
        if self.async_interleave is None or self._in_interleave:
            return
        if self._wire_matches(self._next_matchable(), port):
            return
        self._in_interleave = True
        try:
            self.async_interleave(self)
        finally:
            self._in_interleave = False

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex,
                      data_or_wLength, timeout=None):
        is_in = bool(bmRequestType & 0x80)
        addr = (wValue << 16) | wIndex
        arrow = "RD" if is_in else "WR"
        payload = b"" if is_in else (bytes(data_or_wLength) if data_or_wLength else b"")
        port = PortCall(False, is_in, bRequest, addr, payload, 0)
        self._drain_async(port)
        if self._maybe_extra(port):
            return bytearray(data_or_wLength) if is_in else len(payload)

        op = self._next()
        hdr_ok = op.cls == "ctrl" and op.is_in == is_in and op.breq == bRequest and op.addr == addr
        val_ok = is_in or op.data == payload
        if hdr_ok and val_ok:
            self.matched += 1
            return bytearray(op.data) if is_in else len(payload)
        if self._try_sub(port, op) is not None:
            return bytearray(op.data) if is_in else len(payload)
        if not hdr_ok:
            raise Divergence(
                f"op #{op.idx}: port {arrow} breq=0x{bRequest:02x} 0x{addr:08x}, "
                f"wire has {op.fmt()}", op.idx)
        raise Divergence(
            f"op #{op.idx}: WRITE 0x{addr:08x} value mismatch: port {payload.hex() or '()'} "
            f"vs wire {op.data.hex() or '()'} @f{op.frame}", op.idx)

    def write(self, ep, data, timeout=None):
        data = bytes(data)
        port = PortCall(True, False, 0, 0, data, ep)
        self._drain_async(port)
        if self._maybe_extra(port):
            return len(data)

        op = self._next()
        if op.cls == "bulk" and op.ep == ep and op.data == data:
            self.matched += 1
            return len(data)
        if self._try_sub(port, op) is not None:
            return len(data)
        if op.cls != "bulk" or op.ep != ep:
            raise Divergence(f"op #{op.idx}: port bulk-OUT ep=0x{ep:02x} ({len(data)}B), "
                             f"wire has {op.fmt()}", op.idx)
        n = min(len(op.data), len(data))
        d = next((k for k in range(n) if op.data[k] != data[k]), n)
        pb = data[d:d + 4].hex() if d < len(data) else "-"
        wb = op.data[d:d + 4].hex() if d < len(op.data) else "-"
        raise Divergence(
            f"op #{op.idx}: bulk-OUT ep=0x{ep:02x} byte {d}: port {pb} vs wire {wb} "
            f"(len {len(data)} vs {len(op.data)}) @f{op.frame}", op.idx)

    def read(self, ep, length, timeout=None):
        if self.resp_i >= len(self.responses):
            raise Divergence("port awaited a device-to-host response the wire never carried")
        r = self.responses[self.resp_i]
        self.resp_i += 1
        return bytearray(r)


@dataclass
class _Bucket:
    why: str
    ops: list[int] = field(default_factory=list)      # absolute op indices (wire ops)
    frames: list[int] = field(default_factory=list)
    n_extra: int = 0                                  # port-emitted ops with no wire op


class Ledger:
    """Counts each walked op as reproduced (by handler), waived (by name), or the one
    frontier op. `coverage()` is reproduced/total."""

    def __init__(self, total: int):
        self.total = total
        self.reproduced = 0
        self.by_handler: Counter = Counter()          # label -> reproduced count
        self.waived: dict[str, _Bucket] = {}          # name -> bucket
        self.frontier: Op | None = None
        self.frontier_reason: str = ""

    def credit(self, label: str, n: int) -> None:
        self.reproduced += n
        self.by_handler[label] += n

    def waive(self, waiver: Waiver, op: Op) -> None:
        b = self.waived.setdefault(waiver.name, _Bucket(why=waiver.why))
        b.ops.append(op.idx)
        b.frames.append(op.frame)

    def book_extra(self, waiver: Waiver) -> None:
        self.waived.setdefault(waiver.name, _Bucket(why=waiver.why)).n_extra += 1

    @property
    def waived_count(self) -> int:
        return sum(len(b.ops) + b.n_extra for b in self.waived.values())

    @property
    def extras_count(self) -> int:
        return sum(b.n_extra for b in self.waived.values())

    @property
    def wire_waived(self) -> int:
        """Wire ops consumed by SKIP or SUBSTITUTE waivers (part of `total`, unlike extras)."""
        return self.waived_count - self.extras_count

    def coverage(self) -> float:
        return 100.0 * self.reproduced / self.total if self.total else 0.0


class Walk:
    """One forward-only cursor over the capture, plus the ledger. `run` drives a real port
    handler at the cursor; `waive` consumes named non-port ops."""

    def __init__(self, capture: Capture, waivers: WaiverSet | None = None):
        self.cap = capture
        self.ops = capture.ops
        self.i = 0
        self.waivers = waivers or WaiverSet()
        self.ledger = Ledger(len(self.ops))

    def peek(self) -> Op | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def peek_matchable(self) -> Op | None:
        """Next op no SKIP waiver covers, without consuming. What a dispatch loop keys on."""
        j = self.i
        while j < len(self.ops):
            if self.waivers.first_match(self.ops[j]) is None:
                return self.ops[j]
            j += 1
        return None

    def done(self) -> bool:
        return self.i >= len(self.ops)

    def _device(self, feed_responses: bool, async_interleave=None) -> ReplayDevice:
        resp = self.cap.responses if feed_responses else []
        dev = ReplayDevice(self.ops[self.i:], responses=resp, base=self.i, waivers=self.waivers)
        dev.async_interleave = async_interleave
        return dev

    def waive(self, waivers: WaiverSet, *, limit: int | None = None) -> int:
        """Consume consecutive SKIP-waived ops at the cursor, booking each by name. Returns
        the count, stopping at the first op no waiver covers (or after `limit`)."""
        n = 0
        while not self.done() and (limit is None or n < limit):
            op = self.ops[self.i]
            w = waivers.first_match(op)
            if w is None:
                break
            self.ledger.waive(w, op)
            self.i += 1
            n += 1
        return n

    def _drive(self, dev: ReplayDevice, label: str, exc: BaseException | None) -> None:
        """Book a finished handler's ops and advance the cursor. On a Divergence, leave the
        cursor on the diverging op (which `_next` advanced past but did not match)."""
        for w, op in dev.waived:
            self.ledger.waive(w, op)
        for w, _port in dev.extra:
            self.ledger.book_extra(w)
        self.ledger.credit(label, dev.matched)
        if isinstance(exc, Divergence):
            self.i += max(dev.i - 1, 0)
            self.ledger.frontier = self.peek()
            self.ledger.frontier_reason = str(exc)
        else:
            self.i += dev.i

    def run(self, fn, label: str, *, feed_responses: bool = False, async_interleave=None):
        """Drive a synchronous handler `fn(dev)` at the cursor. `async_interleave` drains
        spliced async ops. A Divergence stops the walk at the frontier."""
        dev = self._device(feed_responses, async_interleave)
        try:
            result = fn(dev)
        except BaseException as e:
            self._drive(dev, label, e)
            raise
        self._drive(dev, label, None)
        return result

    async def run_async(self, coro_fn, label: str, *, feed_responses: bool = True):
        """Async twin of `run` for handlers that await. Responses are served from the wire."""
        dev = self._device(feed_responses)
        try:
            result = await coro_fn(dev)
        except BaseException as e:
            self._drive(dev, label, e)
            raise
        self._drive(dev, label, None)
        return result

    def stack_trace(self, center: int, before: int = 10, after: int = 10) -> str:
        """The ops around `center` (the stop point), like a trace. `center` is arrowed."""
        lo, hi = max(0, center - before), min(len(self.ops), center + after + 1)
        lines = [f"    ... op {lo}..{hi - 1} of {len(self.ops)} (stopped at op {center}) ..."]
        for j in range(lo, hi):
            mark = " >>> " if j == center else "     "
            lines.append(f"{mark}op {j:<5} {self.ops[j].fmt()}")
        return "\n".join(lines)

    def report(self, title: str) -> int:
        """Print the ledger. Exit code: 0 true 100%, 1 frontier, 2 clean but waived."""
        L = self.ledger
        bar = "=" * 78
        print(f"\n{bar}\n{title}\n{bar}")
        print(f"device dev{self.cap.dev} · {L.total} host-to-device ops · "
              f"{len(self.cap.responses)} responses served as input\n")

        print(f"REPRODUCED by real driver code: {L.reproduced} ops ({L.coverage():.1f}%)")
        for label, n in L.by_handler.most_common():
            if n:
                print(f"    {label:.<44} {n}")

        if L.waived:
            print(f"\nWAIVED (named, not replayed against the port): {L.waived_count} ops "
                  f"({100.0 * L.waived_count / L.total:.1f}%)")
            for name, b in L.waived.items():
                total = len(b.ops) + b.n_extra
                where = f"frames {min(b.frames)}-{max(b.frames)}" if b.frames else ""
                if b.n_extra:
                    tag = f"{b.n_extra} port-extra" + (f" + {len(b.ops)} wire" if b.ops else "")
                    where = (where + "; " if where else "") + "port-emitted, no wire op"
                    print(f"    {name:.<44} {total} ops  ({tag}; {where})")
                else:
                    print(f"    {name:.<44} {total} ops  ({where})")
                print(f"        why: {b.why}")
        else:
            print("\nWAIVED: none.")

        if L.frontier is not None:
            f = L.frontier
            print(f"\nFRONTIER (first op nothing reproduces): op #{f.idx} = {f.fmt()}")
            print(f"    reason: {L.frontier_reason}")
            print("    port this next.\n")
            print(self.stack_trace(f.idx))
        elif not self.done():
            nxt = self.peek()
            print(f"\nUNWALKED TAIL: stopped at op #{nxt.idx} = {nxt.fmt()} "
                  f"({L.total - self.i} ops remain)\n")
            print(self.stack_trace(nxt.idx))

        unaccounted = L.total - L.reproduced - L.wire_waived
        extra_note = f" (+{L.extras_count} port-extra)" if L.extras_count else ""
        print(f"\n{'-' * 78}")
        print(f"COVERAGE: {L.reproduced}/{L.total} = {L.coverage():.1f}% replayed against "
              f"real driver code")
        print(f"          wire-waived {L.wire_waived} · unaccounted {unaccounted}{extra_note}")
        if L.frontier is None and self.done() and L.waived_count == 0:
            print("RESULT: PASS. Every recorded op reproduced by real driver code.")
            return 0
        if L.frontier is None and self.done():
            print(f"RESULT: CLEAN BUT WAIVED. No divergence, {L.waived_count} ops waived above. "
                  f"Not a 100% pass.")
            return 2
        print("RESULT: FRONTIER. The walk stopped; see the op above and its trace.")
        return 1
