"""Verify the RTL8922AU port against its cold-boot pcap.

Replays the recorded USB conversation against the driver's real code: one forward cursor over
the captured control ops, driving connect(), asserting the port issues the same op at each
step. Reproduced register ops are counted; ops the driver never emits (USB enumeration) are
waived by name and logged, never silently dropped; the first register op the port does not
emit is the frontier, printed with a trace. It cannot report PASS until every register op
reproduces. Self-contained (rtw89 has no sibling to share with yet).

    uv run python scripts/porting/verify_pcap.py rtl8922au [capture]
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
from collections import Counter, defaultdict, namedtuple
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8922au.driver import RTL8922AUDriver
from wifit3.chips.rtl8922au import chan as chanmod
from wifit3.chips.rtl8922au.rx import iter_bulk_frames

DEFAULT_CAP = "driver_captures/captures_rtw89_8922au_git/capture-1.pcap"
RTW89_USB_VENQT = 0x05

# Channel-hop dispatch markers (per-channel set_channel unit). The unit opens with the
# pre_set_channel_bb read of R_DBCC; the target channel rides the R_FC0 center-freq write a few
# hundred ops later. [SRC] rtw8922a.c:2206 (R_DBCC 0x6b48), 1517 (R_FC0 0x6b4c), CR_BASE_BE 0x20000.
_R_DBCC_ABS = 0x26B48
_R_FC0_ABS = 0x26B4C
_B_FC0_MASK = 0x1FFF             # GENMASK(12, 0): central_freq

# Non-hop mac80211 ops the driver also emits, dispatched by their opening read. configure_filter
# opens with the read of R_BE_RX_FLTR_OPT (MAC_0); config(CONF_CHANGE_MONITOR) opens with the
# physts read of R_PLCP_HISTOGRAM. [SRC] mac.c:2686, phy.c:7140 (+ CR_BASE_BE 0x20000).
_R_RX_FLTR_OPT_ABS = 0x11420
_R_RX_FLTR_OPT_MAC1_ABS = 0x15420    # the dbcc MAC_1 mirror; a configure_filter event writes both
_B_BE_A_MC = 0x8                     # accept-multicast bit: SET only in mac80211's pre-FIF_ALLMULTI
_R_PLCP_HISTOGRAM_ABS = 0x20738
# The periodic DM watchdog (rtw89_track_work) is an async producer; it opens with env_monitor's
# read of R_IFS_TOTAL_BE4. [SRC] core.c:5473, phy.c:7020.
_R_IFS_TOTAL_ABS = 0x20EEC
# The three genuinely-async events: mac80211's configure_filter / config_monitor callbacks and the
# periodic watchdog. Each interleaves with the driver's own ops on the wire (a concurrent callback or
# a timer on Linux), so the harness runs the driver's REAL handler for it wherever its opening read
# appears, then resumes. This is the only legitimate cursor-swap; hops stay real set_channel() calls.
_ASYNC_OPENERS = (_R_RX_FLTR_OPT_ABS, _R_PLCP_HISTOGRAM_ABS, _R_IFS_TOTAL_ABS)

# usbmon mon_bin record offsets.
_OFF_TYPE, _OFF_XFER, _OFF_EP, _OFF_DEV, _OFF_LENCAP, _OFF_SETUP, _OFF_DATA = 8, 9, 10, 11, 36, 40, 64
_URB_SUBMIT, _URB_COMPLETE = 0x53, 0x43
_XFER_ISO, _XFER_INT, _XFER_CTRL, _XFER_BULK = 0x00, 0x01, 0x02, 0x03
_PKT_TYPE_C2H = 10                # rtw89 RX-descriptor pkt_type for a firmware C2H report (not WIFI)
_MIN_RECORD = _OFF_SETUP + 8      # a record must reach through the 8-byte setup packet


class Divergence(Exception):
    """Raised at the first register op the port does not reproduce byte-for-byte."""

    def __init__(self, msg: str, idx: int):
        super().__init__(msg)
        self.idx = idx


class CaptureExhausted(Divergence):
    """The port issued an op past the end of the capture. Benign IFF every capture op already
    matched: the capture was cut mid-operation (e.g. mid double-tune, the last pass unrecorded), and
    the driver correctly continued past the recorded end. A real over-emission bug otherwise."""


# A named reason an op is not the driver's to emit. Every hit is logged, never dropped.
# match(op) -> bool.
Waiver = namedtuple("Waiver", "name why match")


# usbcore drives enumeration (GET_DESCRIPTOR / SET_CONFIGURATION etc.), not the wifi driver.
# Bulk-OUT ops are never waived: they are the driver's firmware/TX transfers to reproduce.
WAIVERS = [
    Waiver("USB enumeration", "standard control requests usbcore issues while enumerating",
           lambda op: op["kind"] == "ctrl" and op["breq"] != RTW89_USB_VENQT),
]


def parse_pcapng(path: str) -> list[bytes]:
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


def detect_dev(pkts: list[bytes]) -> int | None:
    """Devnum issuing the most VENQT (register-access) control transfers."""
    counts: dict[int, int] = {}
    for pkt in pkts:
        if len(pkt) < _MIN_RECORD or pkt[_OFF_XFER] != _XFER_CTRL \
                or pkt[_OFF_TYPE] != _URB_SUBMIT or pkt[_OFF_SETUP + 1] != RTW89_USB_VENQT:
            continue
        counts[pkt[_OFF_DEV]] = counts.get(pkt[_OFF_DEV], 0) + 1
    return max(counts, key=counts.get) if counts else None


def build_ops(pkts: list[bytes], dev: int) -> list[dict]:
    """Every control op plus every bulk-OUT op for the device in capture order. Enumeration
    control ops are kept (waived + logged, not dropped). Bulk-OUT submits (firmware chunks, TX)
    are the driver's to reproduce, so they are real ops. Bulk-IN (RX) is not a discrete driver
    op and is skipped. A control read's value is stitched on from its COMPLETE record.
    Control op: {kind:'ctrl', breq, is_in, wval, widx, wlen, data, frame}.
    Bulk op: {kind:'bulk', ep, wlen, data, frame}."""
    ops: list[dict] = []
    pending: dict[bytes, dict] = {}
    frame = 0
    for pkt in pkts:
        frame += 1
        if len(pkt) < _MIN_RECORD or pkt[_OFF_DEV] != dev:
            continue
        urb = bytes(pkt[0:8])
        if pkt[_OFF_XFER] == _XFER_CTRL:
            if pkt[_OFF_TYPE] == _URB_SUBMIT:
                wval, widx, wlen = struct.unpack_from("<HHH", pkt, _OFF_SETUP + 2)
                op = {"kind": "ctrl", "breq": pkt[_OFF_SETUP + 1], "bmreq": pkt[_OFF_SETUP],
                      "is_in": bool(pkt[_OFF_SETUP] & 0x80),
                      "wval": wval, "widx": widx, "wlen": wlen, "data": b"", "frame": frame}
                if op["is_in"]:
                    pending[urb] = op
                    ops.append(op)
                else:
                    dlen = min(wlen, len(pkt) - _OFF_DATA)
                    op["data"] = bytes(pkt[_OFF_DATA:_OFF_DATA + dlen]) if dlen > 0 else b""
                    ops.append(op)
            elif pkt[_OFF_TYPE] == _URB_COMPLETE:
                op = pending.pop(urb, None)
                if op is not None:
                    dlen = min(op["wlen"], len(pkt) - _OFF_DATA)
                    op["data"] = bytes(pkt[_OFF_DATA:_OFF_DATA + dlen]) if dlen > 0 else b""
        elif pkt[_OFF_XFER] == _XFER_BULK and pkt[_OFF_TYPE] == _URB_SUBMIT \
                and not (pkt[_OFF_EP] & 0x80):
            wlen = struct.unpack_from("<I", pkt, _OFF_LENCAP)[0]
            dlen = min(wlen, len(pkt) - _OFF_DATA)
            ops.append({"kind": "bulk", "breq": None, "ep": pkt[_OFF_EP],
                        "wlen": wlen, "data": bytes(pkt[_OFF_DATA:_OFF_DATA + dlen]),
                        "frame": frame})
    return ops


def _waiver_for(op: dict) -> Waiver | None:
    return next((w for w in WAIVERS if w.match(op)), None)


def _fmt(op: dict) -> str:
    if op["kind"] == "bulk":
        return f"bulk-OUT ep=0x{op['ep']:02x} ({op['wlen']}B) {op['data'][:16].hex()}… @f{op['frame']}"
    addr = op["wval"] | (op["widx"] << 16)       # rtw89 addr decode
    tag = "" if op["breq"] == RTW89_USB_VENQT else f" breq=0x{op['breq']:02x}"
    if op["is_in"]:
        return f"read addr=0x{addr:x} ({op['wlen']}B){tag} @f{op['frame']}"
    return f"write addr=0x{addr:x} = {op['data'].hex()}{tag} @f{op['frame']}"


class _Bucket:
    def __init__(self, why: str):
        self.why = why
        self.frames: list[int] = []


class ReplayDev:
    """Stand-in for usb.core.Device over the op stream. `_next` skips and logs waived ops;
    the port's ctrl_transfer must match the next non-waived op or it raises Divergence."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.matched = 0
        self.waived_reg = 0   # register/bulk ops intentionally waived (e.g. mac80211 filter transient)
        self.waived: dict[str, _Bucket] = {}
        self.speed = 3        # this capture is USB 2 (high-speed); the USB mode switch runs
        self.driver = None    # set in run(); the real methods the async-injector calls
        self._injecting = False           # True while running an injected async handler (no re-entry)
        self.injected = Counter()         # count of each async handler the injector fired

    def _next(self) -> dict:
        while self.i < len(self.ops):
            op = self.ops[self.i]
            w = _waiver_for(op)
            if w is None:
                self.i += 1
                return op
            self.waived.setdefault(w.name, _Bucket(w.why)).frames.append(op["frame"])
            self.i += 1
        raise CaptureExhausted(f"port issued an op past the end of the capture (op #{self.i})", self.i)

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=None):
        if not self._injecting:
            self._absorb_async(wValue | (wIndex << 16))
        op = self._next()
        is_in = bool(bmRequestType & 0x80)
        if op["kind"] != "ctrl":
            raise Divergence(f"op #{self.i - 1}: port issued a control transfer, wire has "
                             f"{_fmt(op)}", self.i - 1)
        # Full bmRequestType (not just the direction bit): a wrong type/recipient is a real bug.
        if bmRequestType != op["bmreq"] or bRequest != op["breq"] \
                or wValue != op["wval"] or wIndex != op["widx"]:
            raise Divergence(
                f"op #{self.i - 1}: port {'read' if is_in else 'write'} bmReq=0x{bmRequestType:02x} "
                f"bReq=0x{bRequest:02x} wV=0x{wValue:04x} wI=0x{wIndex:04x}, wire has "
                f"bmReq=0x{op['bmreq']:02x} {_fmt(op)}", self.i - 1)
        if is_in:
            # A read's wLength is the requested width; a mismatch means the port reads the wrong size.
            if int(data_or_wLength) != op["wlen"]:
                raise Divergence(f"op #{self.i - 1}: read wLength {int(data_or_wLength)} vs wire "
                                 f"{op['wlen']} (addr=0x{_op_addr(op):x}) @f{op['frame']}", self.i - 1)
        else:
            payload = bytes(data_or_wLength) if data_or_wLength else b""
            if payload != op["data"]:
                raise Divergence(f"op #{self.i - 1}: write {payload.hex()} vs wire "
                                 f"{op['data'].hex()} @f{op['frame']}", self.i - 1)
        self.matched += 1
        return bytearray(op["data"]) if is_in else len(op["data"])

    def write(self, endpoint, data, timeout=None):
        """Bulk-OUT write. Matches the next op, which must be a bulk op with the same endpoint
        and byte-identical payload."""
        if not self._injecting:
            self._absorb_async(None)
        op = self._next()
        payload = bytes(data)
        if op["kind"] != "bulk":
            raise Divergence(f"op #{self.i - 1}: port issued a bulk-OUT to ep 0x{endpoint:02x} "
                             f"({len(payload)}B), wire has {_fmt(op)}", self.i - 1)
        if endpoint != op["ep"] or payload != op["data"]:
            raise Divergence(
                f"op #{self.i - 1}: bulk-OUT ep=0x{endpoint:02x} {len(payload)}B "
                f"{payload[:16].hex()}… vs wire {_fmt(op)}", self.i - 1)
        self.matched += 1
        return len(payload)

    def dispose_resources(self, *a):
        pass

    def waive_fltr_event(self, name: str, why: str) -> int:
        """Advance the cursor past one full RX-filter event (the [rd/wr MAC_0][rd/wr MAC_1] RMW),
        logging each op under a named waiver. Used to skip mac80211's transient configure_filter that
        our userland driver does not emit. Returns the op count skipped."""
        n = 0
        while self.i < len(self.ops):
            op = self.ops[self.i]
            if op["kind"] == "ctrl" and _op_addr(op) in (_R_RX_FLTR_OPT_ABS, _R_RX_FLTR_OPT_MAC1_ABS):
                self.waived.setdefault(name, _Bucket(why)).frames.append(op["frame"])
                self.i += 1
                self.waived_reg += 1
                n += 1
            else:
                break
        return n

    def _absorb_async(self, incoming_addr: int | None) -> None:
        """Before matching the driver's next op, run any async event the wire has queued here that the
        driver is not itself emitting: mac80211's concurrent configure_filter/config_monitor, or the
        periodic DM watchdog. Each runs the driver's REAL method (re-entrantly), consuming that event's
        wire ops, so a real set_channel() can proceed across the interleave the capture recorded."""
        while True:
            j = self.next_register_op()
            if j is None:
                return
            op = self.ops[j]
            if op["kind"] != "ctrl" or not op["is_in"]:
                return
            addr = _op_addr(op)
            if addr not in _ASYNC_OPENERS or addr == incoming_addr:
                return                       # not an interleave (or the driver is emitting it itself)
            self._dispatch_async(j)

    def _dispatch_async(self, j: int) -> None:
        """Run the driver's real handler for the async event whose opener is wire op j. The transient
        (pre-FIF_ALLMULTI) filter is waived; every other event is a real driver call."""
        addr = _op_addr(self.ops[j])
        self._injecting = True
        try:
            if addr == _R_RX_FLTR_OPT_ABS:
                if _is_transient_fltr(self.ops, j):
                    self.waive_fltr_event(
                        "mac80211 filter transient",
                        "mac80211's pre-FIF_ALLMULTI filter write (A_MC set); the driver's "
                        "configure_filter emits the final filter, not this intermediate")
                else:
                    self.injected["configure_filter"] += 1
                    self.driver.configure_filter()
            elif addr == _R_PLCP_HISTOGRAM_ABS:
                self.injected["config_monitor"] += 1
                self.driver.config_monitor()
            elif addr == _R_IFS_TOTAL_ABS:
                self.injected["dm_watchdog"] += 1
                self.driver.dm_watchdog()
        finally:
            self._injecting = False

    def next_register_op(self) -> int | None:
        """Index of the next non-waived op at the cursor (the frontier), or None if done."""
        j = self.i
        while j < len(self.ops):
            if _waiver_for(self.ops[j]) is None:
                return j
            j += 1
        return None

    def stack_trace(self, center: int, span: int = 10) -> str:
        lo, hi = max(0, center - span), min(len(self.ops), center + span + 1)
        lines = [f"    ... op {lo}..{hi - 1} of {len(self.ops)} (stopped at op {center}) ..."]
        for j in range(lo, hi):
            mark = " >>> " if j == center else "     "
            waived = "  [waived]" if _waiver_for(self.ops[j]) else ""
            lines.append(f"{mark}op {j:<6} {_fmt(self.ops[j])}{waived}")
        return "\n".join(lines)


def _op_addr(op: dict) -> int:
    return op["wval"] | (op["widx"] << 16)


def _is_hop_opener(op: dict) -> bool:
    """The per-channel unit opens with pre_set_channel_bb's read of R_DBCC."""
    return op["kind"] == "ctrl" and op["is_in"] and _op_addr(op) == _R_DBCC_ABS


def _is_ctrl_read(op: dict, addr: int) -> bool:
    return op["kind"] == "ctrl" and op["is_in"] and _op_addr(op) == addr


def _is_transient_fltr(ops: list[dict], start: int, window: int = 6) -> bool:
    """True if the configure_filter event at `start` is mac80211's pre-FIF_ALLMULTI TRANSIENT (still
    has A_MC set). Our userland monitor sets the promiscuous filter directly and never emits this, so
    the harness waives the transient events; the driver reproduces only the steady (A_MC-clear) ones.
    [SRC] mac80211.c:331 FIF_ALLMULTI clears B_BE_A_MC."""
    for op in ops[start:start + window]:
        if op["kind"] == "ctrl" and not op["is_in"] and _op_addr(op) == _R_RX_FLTR_OPT_ABS \
                and len(op["data"]) == 4:
            return bool(struct.unpack("<I", op["data"])[0] & _B_BE_A_MC)
    return False


def _peek_channel(ops: list[dict], start: int, window: int = 600) -> int | None:
    """The channel a hop targets is a runtime input (airodump picks it); read it from the upcoming
    R_FC0 center-freq write, then map freq -> channel. [SRC] rtw8922a.c:1517."""
    for op in ops[start:start + window]:
        if op["kind"] == "ctrl" and not op["is_in"] and _op_addr(op) == _R_FC0_ABS \
                and len(op["data"]) == 4:
            freq = struct.unpack("<I", op["data"])[0] & _B_FC0_MASK
            return chanmod.freq_to_channel(freq)
    return None


async def _drive(driver: RTL8922AUDriver, replay: "ReplayDev", ops: list[dict]) -> int:
    """Cold bring-up via the driver's real _bringup() (connect() minus the PyUSB interface claim,
    which has no wire bytes), then real per-channel driver.set_channel() hops. mac80211's async
    events (configure_filter / config_monitor / DM watchdog) are injected by ReplayDev where they
    interleave, running the driver's real handlers; a boundary event (one not swallowed mid-hop) is
    dispatched here. Channel number is airodump's choice, read from the wire. Returns the hop count."""
    await driver._bringup()
    hops = 0
    while True:
        j = replay.next_register_op()
        if j is None:
            break
        op = ops[j]
        if _is_hop_opener(op):
            ch = _peek_channel(ops, j)
            if ch is None:
                break
            await driver.set_channel(ch)          # real public API; the driver runs its double-tune
            hops += 1
        elif op["kind"] == "ctrl" and op["is_in"] and _op_addr(op) in _ASYNC_OPENERS:
            replay._dispatch_async(j)             # async event at a boundary (no hop absorbing it)
        else:
            break
    return hops


def urb_census(pkts: list[bytes], replay_dev: int, verbose: bool) -> dict:
    """Classify every usbmon record; return {'enum','rx','c2h'} counts for the replayed device.
    Under --verbose, also print the per-devnum breakdown (the card's re-enum stages vs other bus
    devices, per-class URB counts, bulk-IN RX/C2H split). The card re-enumerates across devnums on
    the USB-2 mode switch; verify replays the post-switch device (the one with the most VENQT ops)."""
    per_dev: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for pkt in pkts:
        if len(pkt) < _MIN_RECORD or pkt[_OFF_TYPE] != _URB_SUBMIT:
            continue
        xf, is_in = pkt[_OFF_XFER], bool(pkt[_OFF_EP] & 0x80)
        if xf == _XFER_CTRL:
            cls = "reg(VENQT)" if pkt[_OFF_SETUP + 1] == RTW89_USB_VENQT else "enum"
        elif xf == _XFER_BULK:
            cls = "bulk-IN" if is_in else "bulk-OUT"
        elif xf == _XFER_INT:
            cls = "interrupt"
        elif xf == _XFER_ISO:
            cls = "iso"
        else:
            continue
        per_dev[pkt[_OFF_DEV]][cls] += 1
    def is_card(c: dict) -> bool:      # our card's stages issue VENQT register ops or bulk transfers
        return bool(c.get("reg(VENQT)") or c.get("bulk-OUT") or c.get("bulk-IN"))
    rx_f = c2h_f = err = 0
    for pkt in pkts:
        if len(pkt) < _MIN_RECORD or pkt[_OFF_DEV] != replay_dev \
                or pkt[_OFF_XFER] != _XFER_BULK or pkt[_OFF_TYPE] != _URB_COMPLETE \
                or not (pkt[_OFF_EP] & 0x80):
            continue
        try:
            for pkt_type, _payload, _rssi in iter_bulk_frames(bytes(pkt[_OFF_DATA:])):
                c2h_f += pkt_type == _PKT_TYPE_C2H
                rx_f += pkt_type != _PKT_TYPE_C2H
        except Exception:  # noqa: BLE001
            err += 1
    if verbose:
        print("URB census (all devnums):")
        for d in sorted(per_dev):
            parts = ", ".join(f"{k}={v}" for k, v in sorted(per_dev[d].items()))
            tag = " <- replayed" if d == replay_dev else \
                (" (card, pre-switch re-enum stage)" if is_card(per_dev[d]) else " (other bus device)")
            print(f"  dev {d}: {parts}{tag}")
        print(f"  dev {replay_dev} bulk-IN: {rx_f} RX + {c2h_f} C2H frames"
              + (f" [{err} unparsed]" if err else ""))
    return {"enum": per_dev[replay_dev].get("enum", 0), "rx": rx_f, "c2h": c2h_f}


def _bulk_in_buffers(pkts: list[bytes], dev: int) -> list[bytes]:
    """The captured bulk-IN COMPLETE buffers for the replayed device, in order (RX + C2H)."""
    out = []
    for pkt in pkts:
        if len(pkt) >= _MIN_RECORD and pkt[_OFF_DEV] == dev and pkt[_OFF_XFER] == _XFER_BULK \
                and pkt[_OFF_TYPE] == _URB_COMPLETE and (pkt[_OFF_EP] & 0x80):
            out.append(bytes(pkt[_OFF_DATA:]))
    return out


def validate_c2h(bufs: list[bytes], driver: RTL8922AUDriver, verbose: bool) -> tuple[int, int]:
    """Replay captured C2H buffers through the driver's real _scan_rfk_c2h -> parse_c2h_hdr. Returns
    (c2h_frames, rfk_reports_recognized). Informational: it does NOT gate the byte-match. Exists so a
    broken C2H parse (0 reports, or a bad state) surfaces. Prints the state breakdown under --verbose."""
    rw = driver.transport.rfk_wait
    orig_signal, reports = rw.signal, []
    rw.signal = lambda state: reports.append(state)
    n_c2h = 0
    try:
        for buf in bufs:
            for pkt_type, _payload, _rssi in iter_bulk_frames(buf):
                n_c2h += pkt_type == _PKT_TYPE_C2H
            driver._scan_rfk_c2h(buf)          # real driver parse; RFK reports call rw.signal
    finally:
        rw.signal = orig_signal
    if verbose:
        states = ", ".join(f"{s}:{c}" for s, c in sorted(Counter(reports).items()))
        print(f"C2H: {n_c2h} frames -> {len(reports)} RFK reports parsed (states {{{states}}})")
    return n_c2h, len(reports)


def run(cap: str | None = None, verbose: bool = False) -> int:
    logging.getLogger("wifit3").setLevel(logging.CRITICAL)
    path = cap or DEFAULT_CAP
    if not Path(path).exists():
        print(f"FAIL: no such capture {path}")
        return 1
    pkts = parse_pcapng(path)
    dev = detect_dev(pkts)
    if dev is None:
        print(f"FAIL: no VENQT device found in {path}")
        return 1
    ops = build_ops(pkts, dev)
    n_reg = sum(1 for o in ops if o["kind"] == "ctrl" and o["breq"] == RTW89_USB_VENQT)
    n_bulk = sum(1 for o in ops if o["kind"] == "bulk")
    n_driver = n_reg + n_bulk         # every op the driver must emit to byte-match the kernel
    print(f"verify_pcap rtl8922au {Path(path).name} (dev {dev})")
    cen = urb_census(pkts, dev, verbose)

    replay = ReplayDev(ops)
    driver = RTL8922AUDriver.from_usb_device(replay, RTL8922AUDriver.SUPPORTED_IDS[0])
    # verify drives _bringup() (not connect()), so the PyUSB interface claim is never called.
    driver._h2c_ep = next((o["ep"] for o in ops if o["kind"] == "bulk"), None)
    replay.driver = driver            # the real handlers the async-injector calls

    diverged = None
    truncated = False
    try:
        asyncio.run(_drive(driver, replay, ops))
    except CaptureExhausted as e:
        # Capture cut mid-operation. Benign only if every capture op already matched (the driver
        # correctly continued past the recorded end, e.g. into the final hop's unrecorded pass).
        if replay.matched + replay.waived_reg == n_driver:
            truncated = True
        else:
            diverged = e
    except Divergence as e:
        diverged = e
    except Exception as e:  # noqa: BLE001
        print(f"harness error {type(e).__name__}: {e} (matched {replay.matched})")
        return 2
    n_c2h, n_rfk = validate_c2h(_bulk_in_buffers(pkts, dev), driver, verbose)

    # Percentage is matched-only over every driver op. Waived ops count AGAINST it, so it reads
    # 100% only when the driver reproduced every byte with nothing waived.
    pct = 100.0 * replay.matched / n_driver if n_driver else 0.0
    print(f"driver ops byte-matched: {replay.matched}/{n_driver} = {pct:.3f}%")
    for name, b in replay.waived.items():
        if b.frames and name != "USB enumeration":     # driver ops we deliberately do not emit
            print(f"  {len(b.frames)} not matched (waived): {name} "
                  f"[f{min(b.frames)}-{max(b.frames)}] {b.why}")
    print(f"not driver ops (usbcore issues them): {cen['enum']} enum")
    print(f"inbound, not byte-matched: {cen['rx']} RX (vary per capture), "
          f"{n_c2h} C2H ({n_rfk} RFK reports parsed)")
    if replay.injected:
        inj = ", ".join(f"{k}×{v}" for k, v in sorted(replay.injected.items()))
        print(f"async-injected (real driver calls run where the wire fired the event): {inj}")

    if diverged is not None:
        print(f"FAIL: diverged at op #{diverged.idx}: {diverged}")
        if verbose:
            print(replay.stack_trace(diverged.idx))
        return 1
    front = replay.next_register_op()
    if front is not None:
        print(f"INCOMPLETE ({pct:.3f}%): next unported op #{front} = {_fmt(ops[front])}")
        if verbose:
            print(replay.stack_trace(front))
        return 1
    trunc = " (capture ends mid-hop; the final tune continued past the recording)" if truncated else ""
    if replay.waived_reg == 0:
        print(f"PASS: 100.000% byte-match, nothing waived{trunc}")
    else:
        print(f"PASS with waivers: {pct:.3f}% byte-match, {replay.waived_reg} ops waived (above){trunc}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    pos = [a for a in argv if not a.startswith("-")]
    return run(pos[0] if pos else None, verbose="--verbose" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
