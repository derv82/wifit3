"""Acceptance gate: replay-diff the rtl8188ftv_dkms driver against vendor cold-boot captures.

    uv run python scripts/chips/rtl8188ftv_dkms/verify_pcap.py <capture.pcap> [<capture.pcap> ...]

One cursor per capture, from the card's first vendor op to its last, fail-closed. Bring-up is
``Rtl8188ftvDkmsDriver._bring_up`` (what ``connect()`` runs after claiming USB) against
``ReplayTransport``. After it, each burst at the cursor goes to the driver: ``set_channel`` (an
iw/airodump hop, channel read from the wire), ``dm.watchdog_tick`` (the body of
``_watchdog_loop``) or ``_inject_frame`` (a bulk-OUT). A whole watchdog tick spliced between two
ops of another handler is drained there. A tick racing a channel switch op-by-op is checked as
two in-order subsequences that together consume the region. Any op no handler claims fails.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8188ftv_dkms import dm, tx
from wifit3.chips.rtl8188ftv_dkms.driver import Rtl8188ftvDkmsDriver

# dm.watchdog_tick head: misc.check_rxfifo_full (0x667/0x664) + false_alarm_stats hold (0xC00/0xD00).
TICK_HEAD = [("R", 0x667, 1), ("W", 0x667, 1), ("R", 0x664, 2),
             ("R", 0xC00, 4), ("W", 0xC00, 4), ("R", 0xD00, 4), ("W", 0xD00, 4)]
# chan.sw_chnl head: rf.set_rf_reg(0x18, 0x3FF) serial read (0x824 RMW, 0x820 readback).
SWITCH_HEAD = [("R", 0x824, 4), ("W", 0x824, 4), ("R", 0x824, 4),
               ("W", 0x824, 4), ("W", 0x824, 4), ("R", 0x820, 4)]
SWITCH_RF18_WRITE_OFFSET = 7    # the RF 0x18 LSSI write, then spur_calibration's 0xC40 RMW
REG_LSSI_A = 0x0840
REG_SPUR_C40 = 0x0C40
RF_CHNLBW = 0x18


class HandlerFailed(Exception):
    """A driver handler returned False instead of raising."""


class CaptureDevice:
    """Stands in for ``usb.core.Device``: no descriptors, so ``tx.bulk_out_size`` uses its
    ``USB_BULK_SIZE`` (high-speed) fallback."""

    def __iter__(self):
        return iter(())


def _at(ops: list[dict], i: int, seq: list[tuple]) -> bool:
    return (0 <= i and i + len(seq) <= len(ops)
            and all(ops[i + k]["kind"] == kind and ops[i + k].get("addr") == addr
                    and ops[i + k].get("width") == width
                    for k, (kind, addr, width) in enumerate(seq)))


def _rf18_channel(op: dict) -> int | None:
    """Channel carried by an LSSI write of RF 0x18 (``(reg << 20) | data``, channel in [7:0])."""
    if op["kind"] != "W" or op.get("addr") != REG_LSSI_A or op.get("width") != 4:
        return None
    if (op["value"] >> 20) & 0xFF != RF_CHNLBW:
        return None
    channel = op["value"] & 0xFF
    return channel if 1 <= channel <= 14 else None


def _switch_channel_at(ops: list[dict], i: int) -> int | None:
    if not _at(ops, i, SWITCH_HEAD):
        return None
    k = i + SWITCH_RF18_WRITE_OFFSET
    if k + 2 >= len(ops):
        return None
    channel = _rf18_channel(ops[k])
    if channel is None:
        return None
    if ops[k + 1]["kind"] != "R" or ops[k + 1].get("addr") != REG_SPUR_C40:
        return None
    if ops[k + 2]["kind"] != "W" or ops[k + 2].get("addr") != REG_SPUR_C40:
        return None
    return channel


def _next_opener(ops: list[dict], start: int) -> int | None:
    for j in range(start, len(ops)):
        if ops[j]["kind"] == "B" or _at(ops, j, TICK_HEAD) or _switch_channel_at(ops, j):
            return j
    return None


def _race_channel(region: list[dict]) -> int | None:
    """The one channel switched in a raced region: an RF 0x18 write followed by a 0xC40 read and
    write before the next RF 0x18 write. None unless exactly one such switch is in the region."""
    writes = [(k, _rf18_channel(o)) for k, o in enumerate(region) if _rf18_channel(o)]
    switches = []
    for n, (k, channel) in enumerate(writes):
        bound = writes[n + 1][0] if n + 1 < len(writes) else len(region)
        read = next((j for j in range(k + 1, bound)
                     if region[j]["kind"] == "R" and region[j].get("addr") == REG_SPUR_C40), None)
        if read is None:
            continue
        if any(region[j]["kind"] == "W" and region[j].get("addr") == REG_SPUR_C40
               for j in range(read + 1, bound)):
            switches.append(channel)
    return switches[0] if len(switches) == 1 else None


class SubsequenceTransport:
    """One thread of a raced region: each op takes the next matching recorded op after the
    previous one it took, so that thread's order holds; ops it passes over stay for the other."""

    def __init__(self, ops: list[dict], base: int):
        self.ops = ops
        self.base = base
        self.pos = 0
        self.taken: set[int] = set()

    def _take(self, kind: str, addr: int, width: int, value: int | None = None) -> int:
        for j in range(self.pos, len(self.ops)):
            op = self.ops[j]
            if (op["kind"] == kind and op.get("addr") == addr and op.get("width") == width
                    and (value is None or op["value"] == value)):
                self.taken.add(j)
                self.pos = j + 1
                return op["value"]
        want = f"{kind} 0x{addr:04x}/{width}" + ("" if value is None else f"=0x{value:x}")
        raise rp.Divergence(f"raced region op#{self.base}-{self.base + len(self.ops) - 1}: "
                            f"no {want} after op#{self.base + self.pos}")

    def remainder(self) -> list[dict]:
        return [op for j, op in enumerate(self.ops) if j not in self.taken]

    def read8(self, a):
        return self._take("R", a, 1)

    def read16(self, a):
        return self._take("R", a, 2)

    def read32(self, a):
        return self._take("R", a, 4)

    def write8(self, a, v):
        self._take("W", a, 1, v & 0xFF)

    def write16(self, a, v):
        self._take("W", a, 2, v & 0xFFFF)

    def write32(self, a, v):
        self._take("W", a, 4, v & 0xFFFFFFFF)


def _ignore_progress(pct: float, msg: str) -> None:
    pass


class Walk:
    """One cursor over the capture, driving one ``Rtl8188ftvDkmsDriver`` whose transport is
    swapped for a ``ReplayTransport`` at the cursor before each handler."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.driver = Rtl8188ftvDkmsDriver(CaptureDevice())
        self.draining = False
        self.spliced_ticks = 0

    def _drain_ticks(self, t: rp.ReplayTransport, addr: int) -> None:
        if self.draining:
            return
        while (t.i < len(t.ops) and t.ops[t.i].get("addr") != addr
               and _at(t.ops, t.i, TICK_HEAD)):
            self.draining = True
            try:
                dm.watchdog_tick(t, self.driver.hal)
            finally:
                self.draining = False
            self.spliced_ticks += 1

    async def run(self, handler, label: str) -> None:
        t = rp.ReplayTransport(self.ops[self.i:])
        t.interleave = lambda addr: self._drain_ticks(t, addr)
        self.driver.transport = t
        try:
            ok = await handler()
        except rp.Divergence:
            self.i += max(t.i - 1, 0)
            raise
        except BaseException:
            self.i += t.i
            raise
        self.i += t.i
        if ok is False:
            raise HandlerFailed(f"{label} returned False")

    async def tick(self) -> None:
        dm.watchdog_tick(self.driver.transport, self.driver.hal)

    async def run_race(self, end: int, channel: int) -> None:
        switch_t = SubsequenceTransport(self.ops[self.i:end], self.i)
        self.driver.transport = switch_t
        if not await self.driver.set_channel(channel):
            raise HandlerFailed(f"set_channel({channel}) returned False")
        rest = switch_t.remainder()
        tick_t = rp.ReplayTransport(rest)
        self.driver.transport = tick_t
        dm.watchdog_tick(tick_t, self.driver.hal)
        if tick_t.i != len(rest):
            raise rp.Divergence(f"raced region op#{self.i}-{end - 1}: switch + tick leave "
                                f"{len(rest) - tick_t.i} ops unclaimed")
        self.i = end


def _first_divergence(e: BaseException) -> BaseException:
    """A Divergence raised in a finally-block hides the first one; report the first."""
    while isinstance(e.__context__, rp.Divergence):
        e = e.__context__
    return e


def _fmt(op: dict) -> str:
    return f"{rp.ReplayTransport._fmt(op)} (frame {op['frame']})"


async def _walk(w: Walk) -> dict:
    counts = {"hops": 0, "ticks": 0, "races": 0, "injects": 0}
    await w.run(lambda: w.driver._bring_up(_ignore_progress), "_bring_up")
    counts["bring_up_ops"] = w.i
    while w.i < len(w.ops):
        op = w.ops[w.i]
        if op["kind"] == "B":
            desc = op["data"][:tx.TXDESC_SIZE]
            frame = op["data"][tx.TXDESC_SIZE + tx.PACKET_OFFSET_SZ * tx.desc_pkt_offset(desc):]
            await w.run(lambda f=frame: w.driver._inject_frame(f), "_inject_frame")
            counts["injects"] += 1
            continue
        if _at(w.ops, w.i, TICK_HEAD):
            await w.run(w.tick, "watchdog_tick")
            counts["ticks"] += 1
            continue
        channel = _switch_channel_at(w.ops, w.i)
        if channel is not None:
            await w.run(lambda c=channel: w.driver.set_channel(c), f"set_channel({channel})")
            counts["hops"] += 1
            continue
        end = _next_opener(w.ops, w.i + 1)
        if end is None:
            end = len(w.ops)
        channel = _race_channel(w.ops[w.i:end])
        if channel is None:
            break
        await w.run_race(end, channel)
        counts["races"] += 1
    return counts


def run_capture(pcap: Path) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no settle delays
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    coverage_ok = rp.audit_coverage(pcap, dev)
    print(f"  coverage audit: {'no blind spot' if coverage_ok else 'BLIND SPOT (fails the run)'}")
    ops = rp.extract_ops(pcap, dev)
    print(f"{pcap.name}: card=dev{dev}, {len(ops)} vendor ops, walk from op#0")
    if not ops:
        print("FAIL: no vendor ops")
        return 1

    w = Walk(ops)
    try:
        counts = asyncio.run(_walk(w))
    except (rp.Divergence, HandlerFailed) as e:
        first = _first_divergence(e)
        at = f" at {_fmt(w.ops[w.i])}" if w.i < len(w.ops) else ""
        print(f"\nFAIL after {w.i} matched ops{at}:\n  {type(first).__name__}: {first}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) after {w.i} ops: {type(e).__name__}: {e}")
        return 2

    print(f"  bring-up: driver._bring_up reproduced {counts['bring_up_ops']} ops")
    print(f"  operational: {counts['hops']} set_channel + {counts['ticks']} watchdog ticks "
          f"({w.spliced_ticks} more spliced into other handlers) + {counts['races']} raced "
          f"set_channel x tick + {counts['injects']} _inject_frame")
    if w.i < len(ops):
        n = len(ops) - w.i
        print(f"\nFAIL: {n} ops UNVERIFIED, op#{w.i}-{len(ops) - 1} (frames "
              f"{ops[w.i]['frame']}-{ops[-1]['frame']}); first: {_fmt(ops[w.i])}. "
              f"No driver handler opens there (a teardown or a path the driver lacks).")
        return 1
    if not coverage_ok:
        print("\nFAIL: every vendor op matched, but the card used a channel the replay "
              "does not reproduce (coverage audit above)")
        return 1
    print(f"\nPASS: {w.i} of {len(ops)} ops reproduced by the driver.")
    return 0


def run(capture: str | None = None) -> int:
    """Registry entry (scripts/porting/verify_pcap.py)."""
    if capture is None:
        print(__doc__)
        return 2
    return run_capture(Path(capture))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Replay-diff the rtl8188ftv_dkms driver against vendor captures.")
    parser.add_argument("pcaps", nargs="+", type=Path, help="vendor-driver USB capture(s)")
    args = parser.parse_args(argv)
    results = [run_capture(p) for p in args.pcaps]
    return max(results)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
