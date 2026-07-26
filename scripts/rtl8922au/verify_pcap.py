"""Verify the RTL8922AU port against its cold-boot pcap.

Replays the recorded USB register conversation against the driver's real code: one forward
cursor over the captured vendor control ops, driving connect(), asserting the port issues the
same op at each step. Reproduced ops are counted; the first op the port does not emit is the
frontier, the next thing to port. Self-contained (rtw89 has no sibling to share with yet).

    uv run python scripts/verify_pcap.py rtl8922au [capture]
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
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
    """Raised at the first op the port does not reproduce byte-for-byte."""


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
    """The device's VENQT register ops in capture order. A read's value is stitched on from
    its COMPLETE record. Each op: {is_in, wval, widx, wlen, data, frame}."""
    ops: list[dict] = []
    pending: dict[bytes, dict] = {}
    frame = 0
    for pkt in pkts:
        frame += 1
        if len(pkt) < _MIN_RECORD or pkt[_OFF_DEV] != dev or pkt[_OFF_XFER] != _XFER_CTRL:
            continue
        urb = bytes(pkt[0:8])
        if pkt[_OFF_TYPE] == _URB_SUBMIT:
            if pkt[_OFF_SETUP + 1] != RTW89_USB_VENQT:
                continue
            wval, widx, wlen = struct.unpack_from("<HHH", pkt, _OFF_SETUP + 2)
            op = {"is_in": bool(pkt[_OFF_SETUP] & 0x80), "wval": wval, "widx": widx,
                  "wlen": wlen, "data": b"", "frame": frame}
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


def _fmt(op: dict) -> str:
    addr = op["wval"] | (op["widx"] << 16)       # rtw89 addr decode
    if op["is_in"]:
        return f"read addr=0x{addr:x} ({op['wlen']}B) @f{op['frame']}"
    return f"write addr=0x{addr:x} = {op['data'].hex()} @f{op['frame']}"


class ReplayDev:
    """Stand-in for usb.core.Device over the op stream. Each ctrl_transfer must match the op
    at the cursor; a read returns the recorded value, a mismatch raises Divergence."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.matched = 0

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=None):
        if self.i >= len(self.ops):
            raise Divergence(f"port issued an op past the end of the capture (op #{self.i})")
        op = self.ops[self.i]
        is_in = bool(bmRequestType & 0x80)
        if bRequest != RTW89_USB_VENQT or is_in != op["is_in"] \
                or wValue != op["wval"] or wIndex != op["widx"]:
            raise Divergence(
                f"op #{self.i}: port {'read' if is_in else 'write'} wV=0x{wValue:04x} "
                f"wI=0x{wIndex:04x}, wire has {_fmt(op)}")
        if not is_in:
            payload = bytes(data_or_wLength) if data_or_wLength else b""
            if payload != op["data"]:
                raise Divergence(f"op #{self.i}: write value {payload.hex()} vs wire "
                                 f"{op['data'].hex()} @f{op['frame']}")
        self.i += 1
        self.matched += 1
        return bytearray(op["data"]) if is_in else len(op["data"])

    def dispose_resources(self, *a):
        pass


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
    print(f"verify_pcap rtl8922au: {Path(path).name}, dev{dev}, {len(ops)} VENQT ops")

    replay = ReplayDev(ops)
    driver = RTL8922AUDriver.from_usb_device(replay, RTL8922AUDriver.SUPPORTED_IDS[0])
    driver._claim_vendor_interface = lambda: 0        # no live USB to claim

    try:
        asyncio.run(driver.connect())
    except Divergence as e:
        print(f"  reproduced {replay.matched} ops; DIVERGENCE {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"  reproduced {replay.matched} ops; harness error {type(e).__name__}: {e}")
        return 2

    if replay.matched == len(ops):
        print(f"  PASS: reproduced all {len(ops)} ops")
        return 0
    front = ops[replay.matched]
    print(f"  reproduced {replay.matched}/{len(ops)} ops; frontier at op #{replay.matched} = "
          f"{_fmt(front)}")
    print("  port this next.")
    return 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
