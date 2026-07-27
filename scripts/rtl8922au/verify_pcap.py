"""Verify the RTL8922AU port against its cold-boot pcap.

Replays the recorded USB conversation against the driver's real code: one forward cursor over
the captured control ops, driving connect(), asserting the port issues the same op at each
step. Reproduced register ops are counted; ops the driver never emits (USB enumeration) are
waived by name and logged, never silently dropped; the first register op the port does not
emit is the frontier, printed with a trace. It cannot report PASS until every register op
reproduces. Self-contained (rtw89 has no sibling to share with yet).

    uv run python scripts/verify_pcap.py rtl8922au [capture]
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
from collections import namedtuple
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8922au.driver import RTL8922AUDriver  # noqa: E402
from wifit3.chips.rtl8922au import chan as chanmod  # noqa: E402

DEFAULT_CAP = "usb_dumps_new2/captures_rtw89_8922au_git/capture-1.pcap"
RTW89_USB_VENQT = 0x05

# Channel-hop dispatch markers (per-channel set_channel unit). The unit opens with the
# pre_set_channel_bb read of R_DBCC; the target channel rides the R_FC0 center-freq write a few
# hundred ops later. [SRC] rtw8922a.c:2206 (R_DBCC 0x6b48), 1517 (R_FC0 0x6b4c), CR_BASE_BE 0x20000.
_R_DBCC_ABS = 0x26B48
_R_FC0_ABS = 0x26B4C
_B_FC0_MASK = 0x1FFF             # GENMASK(12, 0): central_freq

# usbmon mon_bin record offsets.
_OFF_TYPE, _OFF_XFER, _OFF_EP, _OFF_DEV, _OFF_LENCAP, _OFF_SETUP, _OFF_DATA = 8, 9, 10, 11, 36, 40, 64
_URB_SUBMIT, _URB_COMPLETE, _XFER_CTRL, _XFER_BULK = 0x53, 0x43, 0x02, 0x03
_MIN_RECORD = _OFF_SETUP + 8      # a record must reach through the 8-byte setup packet


class Divergence(Exception):
    """Raised at the first register op the port does not reproduce byte-for-byte."""

    def __init__(self, msg: str, idx: int):
        super().__init__(msg)
        self.idx = idx


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
                op = {"kind": "ctrl", "breq": pkt[_OFF_SETUP + 1],
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
        self.waived: dict[str, _Bucket] = {}
        self.speed = 3        # this capture is USB 2 (high-speed); the USB mode switch runs

    def _next(self) -> dict:
        while self.i < len(self.ops):
            op = self.ops[self.i]
            w = _waiver_for(op)
            if w is None:
                self.i += 1
                return op
            self.waived.setdefault(w.name, _Bucket(w.why)).frames.append(op["frame"])
            self.i += 1
        raise Divergence(f"port issued an op past the end of the capture (op #{self.i})", self.i)

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=None):
        op = self._next()
        is_in = bool(bmRequestType & 0x80)
        if op["kind"] != "ctrl":
            raise Divergence(f"op #{self.i - 1}: port issued a control transfer, wire has "
                             f"{_fmt(op)}", self.i - 1)
        if bRequest != op["breq"] or is_in != op["is_in"] \
                or wValue != op["wval"] or wIndex != op["widx"]:
            raise Divergence(
                f"op #{self.i - 1}: port {'read' if is_in else 'write'} wV=0x{wValue:04x} "
                f"wI=0x{wIndex:04x}, wire has {_fmt(op)}", self.i - 1)
        if not is_in:
            payload = bytes(data_or_wLength) if data_or_wLength else b""
            if payload != op["data"]:
                raise Divergence(f"op #{self.i - 1}: write {payload.hex()} vs wire "
                                 f"{op['data'].hex()} @f{op['frame']}", self.i - 1)
        self.matched += 1
        return bytearray(op["data"]) if is_in else len(op["data"])

    def write(self, endpoint, data, timeout=None):
        """Bulk-OUT write. Matches the next op, which must be a bulk op with the same endpoint
        and byte-identical payload."""
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
    """Cold bring-up via connect(), then dispatch each per-channel set_channel unit to
    driver.set_channel(peeked channel). Returns the hop count. Divergence propagates."""
    await driver.connect()
    hops = 0
    while True:
        j = replay.next_register_op()
        if j is None or not _is_hop_opener(ops[j]):
            break
        ch = _peek_channel(ops, j)
        if ch is None:
            break
        await driver.set_channel(ch)
        hops += 1
    return hops


def run(cap: str | None = None) -> int:
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
    n_driver = n_reg + n_bulk         # ops the driver must reproduce (register + bulk-OUT)
    print(f"verify_pcap rtl8922au: {Path(path).name}, dev{dev}, {len(ops)} ops "
          f"({n_reg} register / VENQT, {n_bulk} bulk-OUT)")

    replay = ReplayDev(ops)
    driver = RTL8922AUDriver.from_usb_device(replay, RTL8922AUDriver.SUPPORTED_IDS[0])
    driver._claim_vendor_interface = lambda: 0        # no live USB to claim
    # The bulk-OUT endpoint is discovered from the interface descriptor on real hardware; here it
    # is taken from the capture's first bulk-OUT op (the H2C/fwdl channel).
    driver._h2c_ep = next((o["ep"] for o in ops if o["kind"] == "bulk"), None)

    diverged = None
    hops = 0
    try:
        hops = asyncio.run(_drive(driver, replay, ops))
    except Divergence as e:
        diverged = e
    except Exception as e:  # noqa: BLE001
        print(f"  harness error {type(e).__name__}: {e} (reproduced {replay.matched})")
        return 2
    if hops:
        print(f"CHANNEL HOPS: {hops} set_channel unit(s) driven via driver.set_channel().")

    print(f"\nREPRODUCED (real driver code): {replay.matched}/{n_driver} driver ops "
          f"({n_reg} register + {n_bulk} bulk-OUT)")
    if replay.waived:
        for name, b in replay.waived.items():
            fr = f"frames {min(b.frames)}-{max(b.frames)}" if b.frames else ""
            print(f"WAIVED  {name}: {len(b.frames)} ops ({fr}); {b.why}")
    else:
        print("WAIVED: none")

    if diverged is not None:
        print(f"\nDIVERGENCE: {diverged}")
        print(replay.stack_trace(diverged.idx))
        return 1
    front = replay.next_register_op()
    if front is None and replay.matched == n_driver:
        print("\nRESULT: PASS. Every register + bulk-OUT op reproduced by real driver code.")
        return 0
    if front is not None:
        print(f"\nFRONTIER: op #{front} = {_fmt(ops[front])}. Port this next.")
        print(replay.stack_trace(front))
    print(f"\nRESULT: INCOMPLETE. {replay.matched}/{n_driver} driver ops reproduced.")
    return 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
