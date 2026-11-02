"""TX lab: per-card numbers for injected frames, as seen by a monitor-mode sniffer.

The card under test (--test-card) injects; the sniffer (--sniffer-card) captures. On one channel:
  capture    the sniffer's beacons/s from --ref-bssid (a 100 TU AP sends 9.8/s)
  retries    100 broadcast send_no_wait (expect 1 copy each) and 100 unicast to an address nobody
             ACKs (expect the HW retry limit): copies per frame, retry bit, sequence numbers
  latency    inject call -> sniffer bulk read, and the sniffer's RX queue (bulk read -> callback)
  serial     send_no_wait awaited back-to-back for 5 s: calls/s vs landed/s
  flood      500 unawaited send_no_wait tasks (FakeAP._tx style): landed, out-of-order, drain time
  beacon     FakeAP's own beacon loop for 30 s: gaps at the inject call vs at the sniffer
  responder  the sniffer sends probe requests; FakeAP on the test card ACKs + answers: latency

Timestamps are time.perf_counter(). The script wraps the sniffer transport's ``_read_once`` (stamps
each bulk read on the reader thread) and ``_dispatch`` (hands that stamp to the RX callbacks); src/
is untouched. The sniffer's own USB delay is inside every latency; it is the same sniffer for every
card, so compare cards, not absolutes.

Output prefixes: [+] found/ok   [*] step   [#] a measured number   [-] a problem.
The reference BSSID is passed at runtime; nothing real is hardcoded.
"""
import argparse
import asyncio
import itertools
import logging
import math
import statistics
import struct
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows console is cp1252
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wifit3.campaigns.eviltwin.fake_ap import FakeAP
from wifit3.device.manager import wlan_ifaces
from wifit3.dot11.mac import mac_header
from wifit3.dot11.probe import probe_req, probe_resp

MARK_SRC = "02:7a:7a:00:00:01"    # the test card injects marker frames as this (Addr2)
TWIN_BSSID = "02:7a:7a:00:00:02"  # FakeAP's BSSID in the beacon + responder tests
PROBE_SRC = "02:b0:b0:00:00:00"   # the sniffer's probe requests come from this, last byte = probe index
BOGUS = "02:00:00:00:00:01"       # unicast, nobody owns it: never ACKed, so retried to the limit
LAB_SSID = "wifit3-tx-lab"
BROADCAST = b"\xff" * 6
MARKER = b"TXLAB"
COUNT = 100
INTERVAL = 0.02
SETTLE = 2.0
CAPTURE_SECONDS = 10.0
SERIAL_SECONDS = 5.0
FLOOD = 500
BEACON_SECONDS = 30.0
BEACON_TARGET_MS = 102.4
PROBES = 50
PROBE_GAP = 0.2
HEIGHT = 7                        # histogram rows
COLUMNS = 16                      # histogram columns; the last one also holds everything above


def mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def marker_frame(dst: bytes, n: int) -> bytes:
    """Vendor-specific action frame (category 127) carrying MARKER + counter ``n``."""
    return (mac_header(b"\xd0\x00", dst, mac_bytes(MARK_SRC), mac_bytes(MARK_SRC))
            + b"\x7f\x00\x00\x00" + MARKER + struct.pack("<I", n))


def lab_beacon(channel: int) -> bytes:
    resp = probe_resp(mac_bytes(TWIN_BSSID), LAB_SSID, channel)
    return b"\x80\x00" + resp[2:4] + BROADCAST + resp[10:]


def percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def median_p95_ms(xs: list[float]) -> str:
    """'median / p95' of seconds, in ms: the summary-row form."""
    if not xs:
        return "-"
    return f"{statistics.median(xs) * 1000:.1f} / {percentile(xs, 0.95) * 1000:.1f}"


def gaps_ms(times: list[float]) -> list[float]:
    return [(b - a) * 1000 for a, b in zip(times, times[1:])]


def histogram(title: str, values: list[float], lo: float, step: float) -> list[str]:
    """Bar chart, COLUMNS columns ``step`` wide from ``lo``. Values below ``lo`` land in the first
    column, above the range in the last. Any nonzero column shows at least one row."""
    counts = [0] * COLUMNS
    for v in values:
        counts[min(COLUMNS - 1, max(0, int((v - lo) // step)))] += 1
    top = max(counts) or 1
    bars = [math.ceil(c / top * HEIGHT) if c else 0 for c in counts]
    yw = len(str(top))
    lines = [title]
    for row in range(HEIGHT, 0, -1):
        y = str(top).rjust(yw) if row == HEIGHT else " " * yw
        lines.append(f"{y} |" + "".join("##" if b >= row else "  " for b in bars))
    lines.append(" " * yw + " +" + "--" * COLUMNS)
    left, right = f"{lo:g}", f"{lo + step * (COLUMNS - 1):g}+"
    lines.append(" " * (yw + 2) + left + right.rjust(2 * COLUMNS - len(left)))
    return lines


def print_side_by_side(*blocks: list[str]) -> None:
    widths = [max(len(line) for line in b) for b in blocks]
    for row in itertools.zip_longest(*blocks, fillvalue=""):
        print("    " + "      ".join(line.ljust(w) for line, w in zip(row, widths)).rstrip())


@dataclass
class Copy:
    read_time: float
    dispatch_time: float
    seq: int
    retry: bool


class SnifferTap:
    """Sniffer RX sink. Must be built before the sniffer connects: the RX reader thread captures
    the transport's ``_read_once`` / ``_dispatch`` when it starts."""

    def __init__(self, transport, ref_bssid: bytes) -> None:
        self.ref_bssid = ref_bssid
        self.ref_beacons: list[float] = []
        self.lab_beacons: list[float] = []
        self.probe_resps: dict[bytes, list[float]] = defaultdict(list)   # addr1 -> read times
        self.copies: dict[tuple[bytes, int], list[Copy]] = defaultdict(list)   # (addr1, n)
        self._read_times: dict[int, float] = {}
        self._read_time = 0.0
        read_once, dispatch = transport._read_once, transport._dispatch

        def timed_read_once():
            buf = read_once()
            if buf:
                self._read_times[id(buf)] = time.perf_counter()
            return buf

        def timed_dispatch(buf):
            self._read_time = self._read_times.pop(id(buf), time.perf_counter())
            dispatch(buf)

        transport._read_once, transport._dispatch = timed_read_once, timed_dispatch

    def __call__(self, pkt) -> None:
        raw, t = pkt.raw, self._read_time
        if len(raw) < 24:
            return
        ta = raw[10:16]
        if raw[0] == 0x80:
            if ta == self.ref_bssid:
                self.ref_beacons.append(t)
            elif ta == mac_bytes(TWIN_BSSID):
                self.lab_beacons.append(t)
        elif raw[0] == 0x50 and ta == mac_bytes(TWIN_BSSID):
            self.probe_resps[bytes(raw[4:10])].append(t)
        elif raw[0] == 0xD0 and ta == mac_bytes(MARK_SRC) and raw[28:33] == MARKER:
            n = struct.unpack_from("<I", raw, 33)[0]
            seq = int.from_bytes(raw[22:24], "little") >> 4
            self.copies[(bytes(raw[4:10]), n)].append(
                Copy(t, time.perf_counter(), seq, bool(raw[1] & 0x08)))


class Lab:
    def __init__(self, test, sniffer, tap: SnifferTap, channel: int) -> None:
        self.test, self.sniffer, self.tap, self.channel = test, sniffer, tap, channel
        self.next_n = 0
        self.row: dict[str, str] = {}

    async def send(self, frame: bytes) -> bool:
        try:
            return await self.test.send_no_wait(frame)
        except Exception as e:                    # noqa: BLE001
            print(f"[-] send_no_wait raised: {e!r}")
            return False

    def copies_of(self, dst: bytes, ns) -> dict[int, list[Copy]]:
        return {n: self.tap.copies[(dst, n)] for n in ns if (dst, n) in self.tap.copies}

    async def capture(self) -> None:
        print("[*] capture: the sniffer's view of the reference AP")
        self.tap.ref_beacons.clear()
        await asyncio.sleep(CAPTURE_SECONDS)
        rate = len(self.tap.ref_beacons) / CAPTURE_SECONDS
        print(f"[#] {rate:.1f} beacons/s heard (the AP sends 9.8/s)")
        self.row["ref b/s"] = f"{rate:.1f}"

    async def paced(self, dst: bytes) -> tuple[dict[int, float], int]:
        calls, false = {}, 0
        for n in range(self.next_n, self.next_n + COUNT):
            calls[n] = time.perf_counter()
            false += not await self.send(marker_frame(dst, n))
            await asyncio.sleep(INTERVAL)
        self.next_n += COUNT
        await asyncio.sleep(SETTLE)
        return calls, false

    async def retries_and_latency(self) -> None:
        print(f"[*] retries: {COUNT} broadcast, then {COUNT} unicast to an address nobody ACKs")
        bcast_calls, bcast_false = await self.paced(BROADCAST)
        bogus = mac_bytes(BOGUS)
        uni_calls, uni_false = await self.paced(bogus)
        bcast = self.copies_of(BROADCAST, bcast_calls)
        uni = self.copies_of(bogus, uni_calls)
        bcast_copies = [len(c) for c in bcast.values()]
        uni_copies = [len(c) for c in uni.values()]
        retry_bits = sum(cp.retry for c in bcast.values() for cp in c)
        steps = [bcast[n + 1][0].seq - bcast[n][0].seq for n in bcast if n + 1 in bcast]
        plus_one = sum(s % 4096 == 1 for s in steps)
        uni_median = f"{statistics.median(uni_copies):g}" if uni_copies else "-"
        if bcast_false or uni_false:
            print(f"[-] send_no_wait returned False {bcast_false + uni_false}x")
        print(f"[#] broadcast: {len(bcast)}/{COUNT} landed, most copies of one frame "
              f"{max(bcast_copies, default=0)} (expect 1), retry bit on {retry_bits} copies")
        print(f"[#] broadcast: sequence number +1 between consecutive frames {plus_one}/{len(steps)}")
        print(f"[#] unicast:   {len(uni)}/{COUNT} landed, median {uni_median} copies per frame "
              f"(the HW retry limit, counting the first send)")
        print_side_by_side(histogram("copies per frame: broadcast", bcast_copies, 1, 1),
                           histogram("copies per frame: unicast to nobody", uni_copies, 1, 1))
        print()

        latency = [c[0].read_time - bcast_calls[n] for n, c in bcast.items()]
        rx_queue = [cp.dispatch_time - cp.read_time for c in bcast.values() for cp in c]
        print("[*] latency (broadcast frames above)")
        if not latency:
            print("[-] no broadcast frame landed")
            return
        print(f"[#] inject call -> on air at the sniffer: median {statistics.median(latency) * 1000:.1f} ms, "
              f"95% under {percentile(latency, 0.95) * 1000:.1f} ms")
        print(f"[#] sniffer RX queue (bulk read -> callback): median {statistics.median(rx_queue) * 1000:.1f} ms, "
              f"95% under {percentile(rx_queue, 0.95) * 1000:.1f} ms")
        print_side_by_side(histogram("inject -> on air (ms)", [x * 1000 for x in latency], 0, 2),
                           histogram("sniffer RX queue (ms)", [x * 1000 for x in rx_queue], 0, 10))
        self.row["bcast landed"] = f"{len(bcast)}/{COUNT}"
        self.row["bcast copies max"] = str(max(bcast_copies, default=0))
        self.row["retry bit"] = str(retry_bits)
        self.row["seq +1"] = f"{plus_one}/{len(steps)}"
        self.row["unicast copies"] = uni_median
        self.row["latency ms"] = median_p95_ms(latency)
        self.row["RX queue ms"] = median_p95_ms(rx_queue)

    async def serial(self) -> None:
        print(f"[*] serial: send_no_wait awaited back-to-back for {SERIAL_SECONDS:.0f} s")
        n0, calls, end = self.next_n, 0, time.perf_counter() + SERIAL_SECONDS
        while time.perf_counter() < end:
            await self.send(marker_frame(BROADCAST, self.next_n))
            self.next_n += 1
            calls += 1
            await asyncio.sleep(0)               # let the sniffer's dispatch run
        await asyncio.sleep(SETTLE)
        landed = len(self.copies_of(BROADCAST, range(n0, self.next_n)))
        print(f"[#] {calls / SERIAL_SECONDS:.0f} frames/s sent, {landed / SERIAL_SECONDS:.0f} frames/s "
              f"heard ({landed}/{calls})")
        self.row["serial sent/heard per s"] = f"{calls / SERIAL_SECONDS:.0f} / {landed / SERIAL_SECONDS:.0f}"

    async def flood(self) -> None:
        print(f"[*] flood: {FLOOD} unawaited send_no_wait tasks at once")
        ns = range(self.next_n, self.next_n + FLOOD)
        self.next_n += FLOOD
        t0 = time.perf_counter()
        tasks = [asyncio.create_task(self.send(marker_frame(BROADCAST, n))) for n in ns]
        await asyncio.gather(*tasks)
        returned = time.perf_counter() - t0
        await asyncio.sleep(SETTLE)
        seen = self.copies_of(BROADCAST, ns)
        order = [n for n, _ in sorted(seen.items(), key=lambda kv: kv[1][0].read_time)]
        inversions = sum(b < a for a, b in zip(order, order[1:]))
        last = max((c[0].read_time for c in seen.values()), default=t0) - t0
        print(f"[#] {len(seen)}/{FLOOD} heard, {inversions} out of order")
        print(f"[#] every task returned after {returned:.2f} s; last frame on air {last:.2f} s")
        self.row["flood heard"] = f"{len(seen)}/{FLOOD}"
        self.row["flood out of order"] = str(inversions)
        self.row["flood drain s"] = f"{last:.2f}"

    async def beacon(self) -> None:
        print(f"[*] beacon: FakeAP's beacon loop for {BEACON_SECONDS:.0f} s "
              f"(target gap {BEACON_TARGET_MS} ms)")
        calls: list[float] = []
        send_no_wait = self.test.send_no_wait

        async def timed_send_no_wait(frame: bytes) -> bool:
            calls.append(time.perf_counter())
            return await send_no_wait(frame)

        self.test.send_no_wait = timed_send_no_wait
        fake = FakeAP(self.test, mac_bytes(TWIN_BSSID), LAB_SSID, self.channel, lab_beacon(self.channel))
        self.tap.lab_beacons.clear()
        try:
            await fake.start()
            await asyncio.sleep(BEACON_SECONDS)
            await fake.stop()
        finally:
            del self.test.send_no_wait
        await asyncio.sleep(SETTLE)
        heard = self.tap.lab_beacons
        call_gaps, heard_gaps = gaps_ms(calls), gaps_ms(heard)
        if len(call_gaps) < 2 or len(heard_gaps) < 2:
            print(f"[-] too few beacons: {len(calls)} sent, {len(heard)} heard")
            return
        print(f"[#] {len(heard)}/{len(calls)} heard; {len(calls) / BEACON_SECONDS:.2f} beacons/s sent "
              f"(target {1000 / BEACON_TARGET_MS:.2f})")
        print(f"[#] gap at the inject call: median {statistics.median(call_gaps):.1f} ms, "
              f"95% within {percentile([abs(g - statistics.median(call_gaps)) for g in call_gaps], 0.95):.1f} ms")
        print(f"[#] gap at the sniffer:     median {statistics.median(heard_gaps):.1f} ms, "
              f"95% within {percentile([abs(g - statistics.median(heard_gaps)) for g in heard_gaps], 0.95):.1f} ms "
              f"(a missed beacon shows as a ~205 ms gap)")
        print_side_by_side(histogram("gap at inject call (ms)", call_gaps, 80, 5),
                           histogram("gap at sniffer (ms)", heard_gaps, 80, 5))
        self.row["beacons heard"] = f"{len(heard)}/{len(calls)}"
        self.row["beacons/s sent"] = f"{len(calls) / BEACON_SECONDS:.2f}"
        self.row["beacon gap ms (call)"] = f"{statistics.median(call_gaps):.1f}"
        self.row["beacon gap ms (sniffer)"] = f"{statistics.median(heard_gaps):.1f}"

    async def responder(self) -> None:
        print(f"[*] responder: {PROBES} probe requests from the sniffer to FakeAP on the test card")
        fake = FakeAP(self.test, mac_bytes(TWIN_BSSID), LAB_SSID, self.channel,
                      lab_beacon(self.channel), rx_source=self.test)
        calls: dict[bytes, float] = {}
        await self.sniffer.enable_rx_acks()       # tallies ACKs to each probe's Addr2: active monitor
        await fake.start()
        try:
            for i in range(PROBES):
                src = mac_bytes(PROBE_SRC)[:5] + bytes([i])
                await self.sniffer.set_fake_mac(src, TWIN_BSSID)   # ACK the response, like a phone
                calls[src] = time.perf_counter()
                await self.sniffer.send_no_wait(probe_req(mac_bytes(TWIN_BSSID), src, LAB_SSID,
                                                          self.channel))
                await asyncio.sleep(PROBE_GAP)
        finally:
            await fake.stop()
            await self.sniffer.clear_fake_mac()
        await asyncio.sleep(SETTLE)
        latency = [self.tap.probe_resps[src][0] - t for src, t in calls.items()
                   if self.tap.probe_resps.get(src)]
        acked = sum(self.sniffer.acks_seen(src) > 0 for src in calls)
        await self.sniffer.disable_rx_acks()
        print(f"[#] {acked}/{PROBES} ACKed by the test card's hardware (active monitor)")
        print(f"[#] {len(latency)}/{PROBES} answered with a probe response; FakeAP heard "
              f"{fake.stats.probes_direct} requests (more than {PROBES} = the sniffer resent)")
        if latency:
            print(f"[#] request -> response: median {statistics.median(latency) * 1000:.0f} ms, "
                  f"95% under {percentile(latency, 0.95) * 1000:.0f} ms")
            print_side_by_side(histogram("request -> response (ms)", [x * 1000 for x in latency], 0, 20))
        self.row["probe ACKed"] = f"{acked}/{PROBES}"
        self.row["probe answered"] = f"{len(latency)}/{PROBES}"
        self.row["probe resp ms"] = median_p95_ms(latency)


def pick(ifaces, want: str, channel: int, exclude=None):
    return next((i for i in ifaces if i is not exclude and want.lower() in (i.description or "").lower()
                 and channel in i.driver.SUPPORTED_CHANNELS), None)


async def main(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.ERROR)
    ifaces = wlan_ifaces()
    test = pick(ifaces, a.test_card, a.channel)
    sniffer = pick(ifaces, a.sniffer_card, a.channel, exclude=test)
    if test is None or sniffer is None:
        print(f"[-] need a '{a.test_card}' and a '{a.sniffer_card}' card on channel {a.channel}. plugged in:")
        for i in ifaces:
            print(f"      {i.description}  (ch {i.driver.SUPPORTED_CHANNELS[:4]}...)")
        return
    print(f"[+] test:    {test.description}")
    print(f"[+] sniffer: {sniffer.description}")
    tap = SnifferTap(sniffer.driver.transport, mac_bytes(a.ref_bssid))

    print("[*] connecting...")
    # Sequential, sniffer first: two bring-ups at once collide on USB (see scripts/ack/tx_retries.py).
    snf_ok = await sniffer.connect()
    test_ok = await test.connect()
    if not (snf_ok and test_ok):
        print(f"[-] connect failed (test={test_ok} sniffer={snf_ok})")
        return
    await sniffer.set_channel(a.channel)
    await test.set_channel(a.channel)
    sniffer.register_rx_callback(tap)
    print()

    lab = Lab(test, sniffer, tap, a.channel)
    try:
        for step in (lab.capture, lab.retries_and_latency, lab.serial, lab.flood, lab.beacon,
                     lab.responder):
            await step()
            print()
    finally:
        await asyncio.gather(test.close(), sniffer.close(), return_exceptions=True)
    print("| card | ch | " + " | ".join(lab.row) + " |")
    print(f"| {test.description} | {a.channel} | " + " | ".join(lab.row.values()) + " |")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="TX lab: per-card injection numbers seen by a sniffer")
    p.add_argument("--test-card", required=True, help="substring of the card under test, e.g. 7601")
    p.add_argument("--sniffer-card", default="7925", help="substring of the sniffer card")
    p.add_argument("--ref-bssid", required=True, help="a 100 TU AP on --channel, for the capture check")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--debug", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
