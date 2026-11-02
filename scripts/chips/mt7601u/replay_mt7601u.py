"""Single-cursor usbmon replay engine for MT7601U (scratch harness for the port).

Replays the recorded kernel bring-up against the port's transport and reports the
first op that diverges. Each op is one of:

  read  (offset, expected_len)          br07 vendor IN
  write (offset, value)                 br02 vendor OUT, low half
  writeh(offset, value)                 br02 vendor OUT, high half (offset+2)
  fce   (offset, value)                 br42 vendor OUT, low half
  fceh  (offset, value)                 br42 vendor OUT, high half
  bulk_out (data)                       bulk OUT on the inband endpoint
  bulk_in  (data)                       bulk IN data the kernel received

The port under test is a `MT7601UTransport`-shaped object: rr / wr / fce_wr /
bulk_out / bulk_in_resp.
"""
from __future__ import annotations

import struct
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

OFF_TYPE, OFF_XFER, OFF_EP, OFF_DEV = 8, 9, 10, 11
OFF_LENCAP, OFF_SETUP, OFF_DATA = 36, 40, 64
SUBMIT = 0x53
XFER_BULK = 0x03
EP_INBAND_OUT = 0x08


def _mask_seq(payload: bytes) -> bytes:
    """Zero the MCU command sequence in a bulk-OUT payload.

    The seq is a free-running session counter: its value depends on how many MCU
    commands the driver issued before this one, not on port logic. A replay that
    starts mid-session can never match a recording's counter, so comparing it would
    report a divergence that is not a port defect. Everything else is compared
    byte-for-byte.
    """
    from wifit3.chips.mt7601u.constants import MT_TXD_CMD_INFO_SEQ

    if len(payload) < 4:
        return payload
    info = int.from_bytes(payload[:4], "little") & ~MT_TXD_CMD_INFO_SEQ
    return info.to_bytes(4, "little") + payload[4:]


class Divergence(AssertionError):
    pass


def parse_pcapng(path: str) -> list[bytes]:
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def detect_card(pkts: list[bytes]) -> int | None:
    """The card is the device issuing the most vendor 0x07/0x42 requests."""
    counts: Counter = Counter()
    for pkt in pkts:
        if len(pkt) < 48 or pkt[OFF_XFER] != 2 or pkt[OFF_TYPE] != SUBMIT:
            continue
        bm, br = pkt[OFF_SETUP], pkt[OFF_SETUP + 1]
        if (bm & 0x60) == 0x40 and br in (0x07, 0x42):
            counts[pkt[OFF_DEV]] += 1
    return counts.most_common(1)[0][0] if counts else None


def extract_ops(pkts: list[bytes], dev: int) -> list[tuple]:
    """The ordered op list the kernel issued, collapsing URB submit/complete pairs."""
    ops: list[tuple] = []
    for pkt in pkts:
        if len(pkt) < 48 or pkt[OFF_DEV] != dev:
            continue
        xfer, utype, ep = pkt[OFF_XFER], pkt[OFF_TYPE], pkt[OFF_EP]
        if xfer == 2 and utype == SUBMIT:
            bm, br, wv, wi, _wl = struct.unpack_from("<BBHHH", pkt, OFF_SETUP)
            if (bm & 0x60) != 0x40:
                continue                       # standard / class: enumeration, not ours
            if br == 0x07:
                ops.append(("read", wi))
            elif br in (0x02, 0x42):
                kind = "write" if br == 0x02 else "fce"
                ops.append((kind, wi, wv))
        elif xfer == XFER_BULK and ep == EP_INBAND_OUT and utype == SUBMIT:
            lencap = struct.unpack_from("<I", pkt, OFF_LENCAP)[0]
            ops.append(("bulk_out", pkt[OFF_DATA:OFF_DATA + lencap]))
    return ops


class ReplayTransport:
    """Stands in for the port's transport, fed recorded reads and checking writes."""

    def __init__(self, read_values: dict[int, list[int]], inband_data: dict[int, bytes]):
        self.reads = {off: list(vals) for off, vals in read_values.items()}
        self.bulk_ins = dict(inband_data)
        self.emitted: list[tuple] = []
        self.cursor = 0
        self.ops: list[tuple] = []

    def feed(self, ops: list[tuple]) -> None:
        self.ops = ops

    def _next(self, kind: str, *args):
        while self.cursor < len(self.ops):
            op = self.ops[self.cursor]
            if op[0] == kind:
                self.cursor += 1
                return op
            self.cursor += 1
        raise Divergence(f"port issued {kind} but the capture has none left")

    def rr(self, offset: int) -> int:
        self._next("read", offset)
        vals = self.reads.get(offset)
        if not vals:
            raise Divergence(f"no recorded read value for offset {offset:#06x}")
        return vals.pop(0) if len(vals) > 1 else vals[0]

    def wr(self, offset: int, val: int) -> None:
        lo = self._next("write", offset, val & 0xFFFF)
        if lo[2] != (val & 0xFFFF):
            raise Divergence(f"write low {offset:#06x}: port sent {val & 0xFFFF:#06x}, "
                             f"capture had {lo[2]:#06x} (full val {val:#010x})")
        hi = self._next("write", offset + 2, (val >> 16) & 0xFFFF)
        if hi[2] != ((val >> 16) & 0xFFFF):
            raise Divergence(f"write high {offset + 2:#06x}: port sent {(val >> 16) & 0xFFFF:#06x}, "
                             f"capture had {hi[2]:#06x} (full val {val:#010x})")

    def fce_wr(self, offset: int, val: int) -> None:
        for off, half in ((offset, val & 0xFFFF), (offset + 2, (val >> 16) & 0xFFFF)):
            op = self._next("fce", off, half)
            if op[2] != half:
                raise Divergence(f"fce {off:#06x}: port sent {half:#06x}, capture had {op[2]:#06x}")

    def bulk_out(self, data: bytes, timeout_ms: int = 0) -> int:
        op = self._next("bulk_out")
        if _mask_seq(data) != _mask_seq(op[1]):
            raise Divergence(f"bulk_out {len(data)}B differs from capture {len(op[1])}B\n"
                             f"  port:    {data[:48].hex()}\n"
                             f"  capture: {op[1][:48].hex()}")
        return len(data)


def main() -> int:
    pcap = sys.argv[1]
    pkts = parse_pcapng(pcap)
    dev = detect_card(pkts)
    ops = extract_ops(pkts, dev)
    print(f"card dev={dev}, {len(ops)} replayable ops")
    kinds = Counter(op[0] for op in ops)
    print(" ", dict(kinds))
    return 0


if __name__ == "__main__":
    sys.exit(main())