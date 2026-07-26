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

DEFAULT_CAP = "usb_dumps_new2/captures_rtw89_8922au_git/capture-1.pcap"
RTW89_USB_VENQT = 0x05

# usbmon mon_bin record offsets.
_OFF_TYPE, _OFF_XFER, _OFF_DEV, _OFF_LENCAP, _OFF_SETUP, _OFF_DATA = 8, 9, 11, 36, 40, 64
_URB_SUBMIT, _URB_COMPLETE, _XFER_CTRL = 0x53, 0x43, 0x02
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
WAIVERS = [
    Waiver("USB enumeration", "standard control requests usbcore issues while enumerating",
           lambda op: op["breq"] != RTW89_USB_VENQT),
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
    """Every control op for the device in capture order (enumeration included, so it is
    waived and logged rather than dropped). A read's value is stitched on from its COMPLETE
    record. Each op: {breq, is_in, wval, widx, wlen, data, frame}."""
    ops: list[dict] = []
    pending: dict[bytes, dict] = {}
    frame = 0
    for pkt in pkts:
        frame += 1
        if len(pkt) < _MIN_RECORD or pkt[_OFF_DEV] != dev or pkt[_OFF_XFER] != _XFER_CTRL:
            continue
        urb = bytes(pkt[0:8])
        if pkt[_OFF_TYPE] == _URB_SUBMIT:
            wval, widx, wlen = struct.unpack_from("<HHH", pkt, _OFF_SETUP + 2)
            op = {"breq": pkt[_OFF_SETUP + 1], "is_in": bool(pkt[_OFF_SETUP] & 0x80),
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
    return ops


def _waiver_for(op: dict) -> Waiver | None:
    return next((w for w in WAIVERS if w.match(op)), None)


def _fmt(op: dict) -> str:
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
    n_reg = sum(1 for o in ops if o["breq"] == RTW89_USB_VENQT)
    print(f"verify_pcap rtl8922au: {Path(path).name}, dev{dev}, {len(ops)} control ops "
          f"({n_reg} register / VENQT)")

    replay = ReplayDev(ops)
    driver = RTL8922AUDriver.from_usb_device(replay, RTL8922AUDriver.SUPPORTED_IDS[0])
    driver._claim_vendor_interface = lambda: 0        # no live USB to claim

    diverged = None
    try:
        asyncio.run(driver.connect())
    except Divergence as e:
        diverged = e
    except Exception as e:  # noqa: BLE001
        print(f"  harness error {type(e).__name__}: {e} (reproduced {replay.matched})")
        return 2

    print(f"\nREPRODUCED (real driver code): {replay.matched}/{n_reg} register ops")
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
    if front is None and replay.matched == n_reg:
        print("\nRESULT: PASS. Every register op reproduced by real driver code.")
        return 0
    if front is not None:
        print(f"\nFRONTIER: op #{front} = {_fmt(ops[front])}. Port this next.")
        print(replay.stack_trace(front))
    print(f"\nRESULT: INCOMPLETE. {replay.matched}/{n_reg} register ops reproduced.")
    return 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
