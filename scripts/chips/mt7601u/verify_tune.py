"""Verify MT7601U's channel tune against a recorded monitor-mode capture.

Delimits each tune on the MCU CMD_RANDOM_WRITE that carries freq_plan (the first
op __mt7601u_phy_set_channel issues), replays the port's set_channel against the
recorded op stream in strict order, and reports the first divergence.

Usage: uv run python scratch/verify_tune.py <pcap> [--tune N] [--all]
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from find_tunes import detect_card, parse_pcapng  # noqa: E402

OFF_TYPE, OFF_XFER, OFF_EP, OFF_DEV = 8, 9, 10, 11
OFF_LENCAP, OFF_SETUP, OFF_DATA = 36, 40, 64
SUBMIT = 0x53


class Divergence(AssertionError):
    pass


def _read_data(pkts, i: int, dev: int) -> bytes:
    """The 4 data bytes a read's COMPLETE carries.

    The COMPLETE is usually the next record, but usbmon interleaves URBs from other
    devices (the kernel's own control transfers land in the same stream), so scan
    forward for the first CTRL COMPLETE carrying 4 bytes for THIS device.
    """
    for q in pkts[i + 1:i + 24]:
        if len(q) < 40 or q[OFF_DEV] != dev or q[OFF_XFER] != 2:
            continue
        if q[OFF_TYPE] == SUBMIT:                # another submit: not our reply
            continue
        ln = struct.unpack_from("<I", q, OFF_LENCAP)[0]
        if ln == 4:
            return bytes(q[OFF_DATA:OFF_DATA + 4])
    return b""


def collect(pkts, dev):
    """Host ops in strict order; EP 0x85 MCU responses ride along as `resp`.

    The kernel's response URB is armed before the tune, so every CMD_DONE the
    driver awaited is already sitting in the capture in order; attaching it to
    the op stream lets the port consume the real seq-matched replies.
    """
    ops = []
    for i, p in enumerate(pkts):
        if len(p) < 48 or p[OFF_DEV] != dev:
            continue
        if p[OFF_XFER] == 2 and p[OFF_TYPE] == SUBMIT:
            bm, br, wv, wi, _wl = struct.unpack_from("<BBHHH", p, OFF_SETUP)
            if (bm & 0x60) != 0x40:
                continue
            data = _read_data(pkts, i, dev) if br == 0x07 else b""
            ops.append({"kind": {0x07: "read", 0x02: "write", 0x42: "fce"}[br],
                        "wi": wi, "wv": wv, "data": data, "payload": b"",
                        "resp": b""})
        elif p[OFF_XFER] == 3 and p[OFF_EP] == 0x08 and p[OFF_TYPE] == SUBMIT:
            ln = struct.unpack_from("<I", p, OFF_LENCAP)[0]
            ops.append({"kind": "bulk_out", "wi": None, "wv": None, "data": b"",
                        "payload": bytes(p[OFF_DATA:OFF_DATA + ln]), "resp": b""})
    return ops


class TuneReplay:
    """The port's transport + MCU surface, fed one tune's recorded ops."""

    def __init__(self, ops: list[dict], responses: list[bytes] | None = None):
        self.ops = ops
        # Index the captured CMD_DONEs by their own seq. A message only consumes a
        # response when it awaits one, so positional queuing would drain replies
        # belonging to fire-and-forget commands.
        from wifit3.chips.mt7601u import constants as C
        self._by_seq: dict[int, bytes] = {}
        for r in (responses or []):
            if len(r) >= 4:
                info = int.from_bytes(r[:4], "little")
                self._by_seq.setdefault(C._field_get(C.MT_RXD_CMD_INFO_CMD_SEQ, info), r)
        self._sent_seq = 0
        self.i = 0
        self.matched = 0
        self._respq: list[bytes] = []

    def _expect(self, kind, **fields):
        if self.i >= len(self.ops):
            raise Divergence(f"port issued {kind} but the tune is exhausted "
                             f"after {self.matched} ops")
        op = self.ops[self.i]
        if op["kind"] != kind:
            raise Divergence(
                f"op #{self.i} ({self.matched} matched): capture has "
                f"{op['kind']}({op['wi']:#06x})" if op["wi"] is not None else
                f"op #{self.i} ({self.matched} matched): capture has {op['kind']}")
        for key, want in fields.items():
            got = op[key]
            if got != want:
                raise Divergence(
                    f"op #{self.i} ({kind}) {key}: port={want!r} capture={got!r}")
        self.i += 1
        self.matched += 1
        return op

    def rr(self, offset):
        op = self._expect("read", wi=offset)
        return int.from_bytes(op["data"], "little") if len(op["data"]) == 4 else 0xFFFFFFFF

    def wr(self, offset, val):
        self._expect("write", wi=offset, wv=val & 0xFFFF)
        self._expect("write", wi=offset + 2, wv=(val >> 16) & 0xFFFF)

    def rmw(self, offset, mask, val):
        """Read-modify-write that always writes (usb.c:188)."""
        cur = self.rr(offset)
        val |= cur & ~mask & 0xFFFFFFFF
        self.wr(offset, val)
        return val

    def rmc(self, offset, mask, val):
        """Read-modify-write that skips an unchanged register (usb.c:198)."""
        cur = self.rr(offset)
        val |= cur & ~mask & 0xFFFFFFFF
        if cur != val:
            self.wr(offset, val)
        return val

    def bulk_out(self, data, timeout_ms=0):
        from wifit3.chips.mt7601u import constants as C
        op = self._expect("bulk_out")
        # MT_TXD_CMD_INFO_SEQ is a free-running session counter: its value depends
        # on how many MCU commands the driver issued before this tune, not on port
        # logic. Mask it so the gate tests the payload, not the counter's history.
        mask = C.MT_TXD_CMD_INFO_SEQ

        def norm(p: bytes) -> bytes:
            if len(p) < 4:
                return p
            info = int.from_bytes(p[:4], "little") & ~mask
            return info.to_bytes(4, "little") + p[4:]

        if norm(op["payload"]) != norm(data):
            raise Divergence(
                f"op #{self.i} (bulk_out): port sent {len(data)}B {data[:32].hex()}\n"
                f"           capture had {len(op['payload'])}B {op['payload'][:32].hex()}")
        # Remember the seq the driver put on the wire; the MCU echoes it back.
        self._sent_seq = C._field_get(
            C.MT_TXD_CMD_INFO_SEQ, int.from_bytes(data[:4], "little"))
        return op["resp"]

    # The firmware-download URB shares the inband endpoint.
    bulk_out_inband_fw = bulk_out

    def bulk_in_resp(self, buf, timeout_ms=0):
        """A captured CMD_DONE with its seq rewritten to the one we just sent.

        The seq is a free-running session counter, so the recording's value cannot
        match a replay that started mid-session; the MCU's echo is what matters.
        """
        from wifit3.chips.mt7601u import constants as C
        raw = self._by_seq.get(self._sent_seq, b"")
        if not raw:
            return b""
        info = int.from_bytes(raw[:4], "little")
        info = (info & ~C.MT_RXD_CMD_INFO_CMD_SEQ) | _field_prep_seq(self._sent_seq)
        return info.to_bytes(4, "little") + raw[4:]


def _field_prep_seq(seq: int) -> int:
    from wifit3.chips.mt7601u import constants as C
    return C._field_prep(C.MT_RXD_CMD_INFO_CMD_SEQ, seq)

    def _take(self):
        self.i += 1
        self.matched += 1
        return self.ops[self.i - 1]


def tune_bounds(ops: list[dict]) -> list[tuple[int, int]]:
    """(start, end) of each tune, delimited on the RF-memmap CMD_RANDOM_WRITE."""
    from wifit3.chips.mt7601u import constants as C

    def is_plan(op) -> bool:
        if op["kind"] != "bulk_out":
            return False
        p = op["payload"]
        if len(p) < 12:
            return False
        info = int.from_bytes(p[:4], "little")
        if C._field_get(C.MT_TXD_CMD_INFO_TYPE, info) != C.CMD_RANDOM_WRITE:
            return False
        ln = C._field_get(C.MT_TXD_INFO_LEN, info)
        body = p[4:4 + ln]
        pairs = [(int.from_bytes(body[j:j + 4], "little"),
                  int.from_bytes(body[j + 4:j + 8], "little"))
                 for j in range(0, len(body) - 7, 8)]
        return bool(pairs) and (pairs[0][0] & ~0xFFFF) == 0x80000000 \
            and (pairs[0][0] & 0xFFFF) == 17

    starts = [n for n, op in enumerate(ops) if is_plan(op)]
    return list(zip(starts, starts[1:] + [len(ops)]))


def main() -> int:
    pcap = sys.argv[1]
    only = None
    if "--tune" in sys.argv:
        only = int(sys.argv[sys.argv.index("--tune") + 1])
    do_all = "--all" in sys.argv

    pkts = parse_pcapng(pcap)
    dev = detect_card(pkts)
    ops = collect(pkts, dev)
    bounds = tune_bounds(ops)
    responses = [bytes(p[OFF_DATA:OFF_DATA + 16])
                 for p in pkts
                 if len(p) >= 64 and p[OFF_DEV] == dev
                 and p[OFF_XFER] == 3 and p[OFF_EP] == 0x85 and p[OFF_TYPE] != SUBMIT]
    print(f"MCU responses on EP 0x85: {len(responses)}")
    print(f"card dev={dev}: {len(ops)} ops, {len(bounds)} tunes")

    from wifit3.chips.mt7601u.mcu import MT7601UMcu
    from wifit3.chips.mt7601u.phy import MT7601UPhy
    from verify_mt7601u import StrictReplayTransport as ColdBootTp
    from verify_mt7601u import load_ops
    from wifit3.chips.mt7601u.eeprom import MT7601UEeprom

    # The tune consumes EEPROM-derived values (per-channel power, LNA gain), so
    # decode the real block from a cold-boot capture rather than guessing them.
    cold = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else None
    params = None
    if cold:
        cops = load_ops(cold, detect_card(parse_pcapng(cold)))
        ctp = ColdBootTp(cops)
        start = next(i for i, o in enumerate(cops)
                     if o["kind"] == "write" and o["wi"] == 0x0024) - 1
        ctp.i = start
        cee = MT7601UEeprom(ctp)
        try:
            params = cee.read()
        except Exception as e:
            print(f"  (EEPROM replay failed: {e})")
    if params is not None:
        print(f"EEPROM chan_pwr[0..5] = {params.chan_pwr[:6]} lna_gain={params.lna_gain}")

    targets = range(len(bounds)) if do_all else (
        [only] if only is not None else range(min(5, len(bounds))))

    passed = failed = 0
    for n in targets:
        a, b = bounds[n]
        tp = TuneReplay(ops[a:b], responses)
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        from wifit3.chips.mt7601u.eeprom import MT7601UEepromParams
        phy = MT7601UPhy(tp, mcu, params or MT7601UEepromParams())
        # Feed the EEPROM-derived values the tune consumes.
        try:
            phy.set_channel(_channel_for(ops[a], bounds), 0)
            passed += 1
            print(f"  tune {n:3d}: MATCH, {tp.matched}/{b - a} ops")
        except Divergence as e:
            failed += 1
            print(f"  tune {n:3d}: DIVERGED after {tp.matched} ops\n      {e}")
        except Exception as e:
            failed += 1
            print(f"  tune {n:3d}: port raised {type(e).__name__}: {e}")
    print(f"\n{passed} matched, {failed} diverged")
    return 0 if failed == 0 else 1


def _channel_for(plan_op, bounds):
    """Map a tune's freq_plan row back to its 1..14 channel."""
    from wifit3.chips.mt7601u.phy import FREQ_PLAN
    p = plan_op["payload"]
    body = p[4:]
    vals = []
    for j in range(0, min(len(body) - 7, 32), 8):
        vals.append(int.from_bytes(body[j + 4:j + 8], "little") & 0xFF)
    row = tuple(vals[:4])
    for idx, plan in enumerate(FREQ_PLAN):
        if plan == row:
            return idx + 1
    return 1


if __name__ == "__main__":
    sys.exit(main())