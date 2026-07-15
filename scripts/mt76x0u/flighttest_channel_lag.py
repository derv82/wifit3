"""flighttest_channel_lag.py — root-cause the 3-second UI freeze on
mt76x0u channel changes.

User report: pinning to one channel = smooth. Switching from that channel
to a different one = ~3 second UI freeze that persists in 3-second chunks
afterward. Default channel-hopping (all 22 channels) is "fine".

Hypotheses to test:
  H1) `set_channel` body itself blocks ~3s (synchronous MCU traffic in
      asyncio loop). Predicted: workload A reproduces.
  H2) RxDrainer drains a backlog of frames buffered during set_channel,
      blocking event loop AFTER set_channel returns. Predicted: event-loop
      monitor shows blocks AFTER set_channel completion.
  H3) stop_hopping waits for in-flight sync set_channel to finish. Predicted:
      workload B (UI sim) reproduces; workload A doesn't.
  H4) PyUSB executor thread pile-up during set_channel resolves all at once
      after, causing burst processing. Predicted: many close-spaced
      async_bulk_in completions immediately after set_channel.

Instrumentation:
  - Monkey-patch `MCUChannel.send_msg` to record per-command wall-clock time.
  - Monkey-patch `MT76x0UTransport.read32/write32/bulk_in/bulk_out` to log
    counts + total time per phase.
  - Background "event loop monitor" task that records any pause >100ms
    in scheduled 50ms wakeups — direct UI-freeze proxy.
  - Per-set_channel summary: total wall, phase breakdown, #MCU cmds,
    event-loop blocks observed during + 2s after.
  - RxDrainer rate sampled every 250ms.

Workloads:
  --workload bare   : just set_channel(1) -> 5s -> set_channel(6) -> 5s -> set_channel(1)
  --workload uisim  : start_hopping(all 2.4G) -> 5s -> stop+start[1] -> 5s ->
                       stop+start[6] (THE ENTER PRESS) -> 5s -> stop+start[1]
                       -> 5s -> stop. This is what the UI actually does.
  --workload both   : run bare then uisim back-to-back. (default)
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.mt76x0u.constants import USB_IDS_MT76X0U
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.chips.driver import DeviceID


# ---------------------------------------------------------------------------
# Telemetry buckets
# ---------------------------------------------------------------------------
class Telemetry:
    def __init__(self):
        # per-MCU send_msg latency list
        self.mcu_latencies_ms: list[float] = []
        # per-transport call counts
        self.transport_counts = collections.Counter()
        # event-loop blocks: list of (timestamp, gap_ms)
        self.loop_blocks: list[tuple[float, float]] = []
        # per-phase logs: list of (label, start_t, duration_ms, mcu_count_during,
        #                          loop_blocks_during)
        self.phases: list[dict] = []
        self._mcu_count_at_phase_start = 0
        self._loop_blocks_at_phase_start = 0
        # RxDrainer rate samples: list of (t, rx_count)
        self.rx_samples: list[tuple[float, int]] = []

    def phase_start(self, label: str) -> dict:
        return {
            "label": label,
            "t0": time.monotonic(),
            "mcu_count_at_start": len(self.mcu_latencies_ms),
            "loop_blocks_at_start": len(self.loop_blocks),
        }

    def phase_end(self, p: dict) -> None:
        p["duration_ms"] = (time.monotonic() - p["t0"]) * 1000
        p["mcu_count"] = len(self.mcu_latencies_ms) - p["mcu_count_at_start"]
        p["loop_blocks_count"] = len(self.loop_blocks) - p["loop_blocks_at_start"]
        p["loop_blocks_total_ms"] = sum(
            ms for _, ms in self.loop_blocks[p["loop_blocks_at_start"]:]
        )
        self.phases.append(p)


T = Telemetry()


# ---------------------------------------------------------------------------
# Monkey-patches
# ---------------------------------------------------------------------------
def install_mcu_instrumentation(driver: MT76x0UDriver) -> None:
    """Wrap MCUChannel.send_msg to record per-command wall-clock time."""
    mcu = driver.mcu
    original = mcu.send_msg

    def timed_send(*args, **kwargs):
        t0 = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            T.mcu_latencies_ms.append((time.monotonic() - t0) * 1000)

    mcu.send_msg = timed_send


def install_transport_instrumentation(driver: MT76x0UDriver) -> None:
    """Wrap transport.{read32,write32,bulk_in,bulk_out} to count calls."""
    t = driver.transport

    for name in ("read32", "write32", "bulk_in", "bulk_out"):
        original = getattr(t, name)

        def make_wrapper(orig, n):
            def wrapper(*a, **kw):
                T.transport_counts[n] += 1
                return orig(*a, **kw)
            return wrapper

        setattr(t, name, make_wrapper(original, name))


# ---------------------------------------------------------------------------
# Event-loop monitor
# ---------------------------------------------------------------------------
async def event_loop_monitor(stop: asyncio.Event, threshold_ms: float = 100) -> None:
    """Wake every 50ms, record any actual gap > threshold_ms."""
    interval = 0.05
    last = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = time.monotonic()
        gap_ms = (now - last - interval) * 1000
        if gap_ms > threshold_ms:
            T.loop_blocks.append((now, gap_ms))
        last = now


async def rx_drainer_sampler(driver: MT76x0UDriver, stop: asyncio.Event) -> None:
    """Sample RxDrainer.rx_count every 250ms."""
    while not stop.is_set():
        if driver._rx_drainer is not None:
            T.rx_samples.append(
                (time.monotonic(), driver._rx_drainer.rx_count)
            )
        await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------
async def workload_bare(driver: MT76x0UDriver) -> None:
    """Three direct set_channel calls with 5s sleep between."""
    print("\n=== Workload A: bare set_channel(1) -> 5s -> set_channel(6) -> 5s -> set_channel(1) ===\n")

    print("[settling 2s at startup channel]")
    await asyncio.sleep(2)

    for label, ch in [("A1: set_channel(1)", 1),
                      ("A2: set_channel(6)", 6),
                      ("A3: set_channel(1)", 1)]:
        print(f"\n--- {label} ---")
        p = T.phase_start(label)
        ok = await driver.set_channel(ch)
        T.phase_end(p)
        print(f"  ok={ok}  duration={p['duration_ms']:.0f}ms  "
              f"mcu_cmds={p['mcu_count']}  "
              f"loop_blocks={p['loop_blocks_count']}  "
              f"loop_blocked_total={p['loop_blocks_total_ms']:.0f}ms")

        # 5s post-window: still measure loop blocks (H2 hypothesis)
        print("  [observing 5s post-set_channel for delayed lag...]")
        post = T.phase_start(f"{label} POST-5s")
        await asyncio.sleep(5)
        T.phase_end(post)
        print(f"  POST: loop_blocks={post['loop_blocks_count']}  "
              f"loop_blocked_total={post['loop_blocks_total_ms']:.0f}ms")


async def workload_longhop(driver: MT76x0UDriver, duration: float,
                            hop_interval: float = 0.25) -> None:
    """Continuous channel hopping for N seconds. User reports: just leaving
    the UI hopping for 10-15s causes 3s lag to start ON ITS OWN. Something
    accumulates.

    Tracks set_channel duration AND event-loop blocks per 1s bucket so we
    can see WHEN they start appearing.
    """
    print(f"\n=== Workload C: continuous hop 1..13 for {duration}s "
          f"(interval={hop_interval}s) ===\n")

    state = {"running": True}
    channels = list(range(1, 14))
    hop_count = 0
    set_channel_durations: list[float] = []
    set_channel_timestamps: list[float] = []

    async def hop_loop():
        nonlocal hop_count
        cycle_idx = 0
        while state["running"]:
            ch = channels[cycle_idx % len(channels)]
            cycle_idx += 1
            t0 = time.monotonic()
            await driver.set_channel(ch)
            elapsed = (time.monotonic() - t0) * 1000
            set_channel_durations.append(elapsed)
            set_channel_timestamps.append(t0)
            hop_count += 1
            await asyncio.sleep(hop_interval)

    p = T.phase_start(f"C: longhop {duration}s")
    task = asyncio.create_task(hop_loop())
    await asyncio.sleep(duration)
    state["running"] = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    T.phase_end(p)

    print(f"\n[longhop done -- {hop_count} set_channel calls in {duration}s]")
    print(f"  total loop_blocks={p['loop_blocks_count']}  "
          f"total_blocked={p['loop_blocks_total_ms']:.0f}ms")

    if set_channel_durations:
        t_start = set_channel_timestamps[0]
        per_sec_max: dict[int, float] = {}
        per_sec_count: dict[int, int] = {}
        per_sec_total: dict[int, float] = {}
        for ts, dur in zip(set_channel_timestamps, set_channel_durations):
            bucket = int(ts - t_start)
            per_sec_max[bucket] = max(per_sec_max.get(bucket, 0), dur)
            per_sec_count[bucket] = per_sec_count.get(bucket, 0) + 1
            per_sec_total[bucket] = per_sec_total.get(bucket, 0) + dur
        print("\n  set_channel duration per 1s window:")
        print(f"  {'T+s':>4s}  {'n':>3s}  {'max_ms':>7s}  {'mean_ms':>8s}")
        for b in sorted(per_sec_count):
            mean = per_sec_total[b] / per_sec_count[b]
            print(f"  {b:>4d}  {per_sec_count[b]:>3d}  "
                  f"{per_sec_max[b]:>6.0f}   {mean:>7.1f}")

    if T.loop_blocks:
        t_start = T.loop_blocks[0][0]
        per_sec_blocks: dict[int, list[float]] = {}
        for ts, gap in T.loop_blocks:
            bucket = int(ts - t_start)
            per_sec_blocks.setdefault(bucket, []).append(gap)
        print("\n  event-loop blocks per 1s window:")
        print(f"  {'T+s':>4s}  {'#blocks':>7s}  {'max_ms':>7s}  {'total_ms':>9s}")
        for b in sorted(per_sec_blocks):
            blks = per_sec_blocks[b]
            print(f"  {b:>4d}  {len(blks):>7d}  {max(blks):>6.0f}   "
                  f"{sum(blks):>8.0f}")


async def workload_uisim(driver, iface_proxy) -> None:
    """Mimic the UI's channel-filter-dialog Enter-press behaviour.

    The UI doesn't call driver.set_channel directly — it calls
    iface.stop_hopping() + iface.start_hopping(channels=...). We have to
    reproduce that wrapper. Since we don't have a real WlanInterface, we
    simulate the hop-loop pattern directly using asyncio.create_task.
    """
    print("\n=== Workload B: UI sim — start_hopping -> pin to [1] -> switch to [6] (THE LAG REPRO) -> ... ===\n")

    # Hop loop state
    state = {"running": False, "task": None, "channels": None}

    async def hop_loop(channels: list[int], interval: float):
        cycle_idx = 0
        while state["running"]:
            ch = channels[cycle_idx % len(channels)]
            cycle_idx += 1
            await driver.set_channel(ch)
            await asyncio.sleep(interval)

    async def stop_hopping():
        state["running"] = False
        if state["task"]:
            state["task"].cancel()
            try:
                await state["task"]
            except asyncio.CancelledError:
                pass
            state["task"] = None

    async def start_hopping(channels: list[int], interval: float = 0.25):
        state["running"] = True
        state["channels"] = channels
        state["task"] = asyncio.create_task(hop_loop(channels, interval))

    # Step 1: start hopping all 2.4 GHz channels
    print("[B1: start_hopping channels 1-13, interval=0.25s]")
    p = T.phase_start("B1: start_hopping([1..13])")
    await start_hopping(list(range(1, 14)), interval=0.25)
    await asyncio.sleep(5)
    T.phase_end(p)
    print(f"  observed: mcu_cmds={p['mcu_count']}  loop_blocks={p['loop_blocks_count']}  "
          f"loop_blocked_total={p['loop_blocks_total_ms']:.0f}ms (over 5s)")

    # Step 2: pin to ch 1 — UI does stop_hopping + start_hopping([1])
    print("\n[B2: 'pin to channel 1' — stop_hopping + start_hopping([1])]")
    p = T.phase_start("B2: pin to [1]")
    await stop_hopping()
    await start_hopping([1], interval=0.25)
    await asyncio.sleep(5)
    T.phase_end(p)
    print(f"  observed: mcu_cmds={p['mcu_count']}  loop_blocks={p['loop_blocks_count']}  "
          f"loop_blocked_total={p['loop_blocks_total_ms']:.0f}ms (over 5s)")

    # Step 3: SWITCH to ch 6 — THE LAG TRIGGER per user
    print("\n[B3: 'switch from ch 1 to ch 6' — THIS IS WHERE USER SEES 3s LAG]")
    p = T.phase_start("B3: switch [1]->[6] (LAG TRIGGER)")
    await stop_hopping()
    await start_hopping([6], interval=0.25)
    # Measure JUST the stop+start handoff first
    handoff_ms = (time.monotonic() - p["t0"]) * 1000
    print(f"  stop+start handoff alone: {handoff_ms:.0f}ms")
    await asyncio.sleep(5)
    T.phase_end(p)
    print(f"  total observed: mcu_cmds={p['mcu_count']}  loop_blocks={p['loop_blocks_count']}  "
          f"loop_blocked_total={p['loop_blocks_total_ms']:.0f}ms (over 5s)")

    # Step 4: another switch
    print("\n[B4: 'switch from ch 6 to ch 11']")
    p = T.phase_start("B4: switch [6]->[11]")
    await stop_hopping()
    await start_hopping([11], interval=0.25)
    await asyncio.sleep(5)
    T.phase_end(p)
    print(f"  observed: mcu_cmds={p['mcu_count']}  loop_blocks={p['loop_blocks_count']}  "
          f"loop_blocked_total={p['loop_blocks_total_ms']:.0f}ms (over 5s)")

    await stop_hopping()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def print_report():
    print("\n" + "=" * 72)
    print("REPORT")
    print("=" * 72)

    print("\nTransport call totals:")
    for name, count in sorted(T.transport_counts.items()):
        print(f"  {name:>10s}  {count:>6d}")

    if T.mcu_latencies_ms:
        sorted_ms = sorted(T.mcu_latencies_ms)
        n = len(sorted_ms)
        print(f"\nMCU send_msg latency (n={n}):")
        print(f"  min={sorted_ms[0]:.1f}ms  "
              f"median={sorted_ms[n // 2]:.1f}ms  "
              f"p90={sorted_ms[int(n * 0.9)]:.1f}ms  "
              f"p99={sorted_ms[int(n * 0.99)]:.1f}ms  "
              f"max={sorted_ms[-1]:.1f}ms  "
              f"mean={sum(sorted_ms) / n:.1f}ms")

    print(f"\nEvent-loop blocks > 100ms detected: {len(T.loop_blocks)}")
    if T.loop_blocks:
        sorted_gaps = sorted(g for _, g in T.loop_blocks)
        print(f"  total blocked time: {sum(sorted_gaps):.0f}ms")
        print(f"  largest: {sorted_gaps[-1]:.0f}ms")
        print(f"  median: {sorted_gaps[len(sorted_gaps) // 2]:.0f}ms")
        # Show top-5 longest blocks with their timestamps
        top5 = sorted(T.loop_blocks, key=lambda x: -x[1])[:5]
        print("  top 5 longest blocks (timestamp, ms):")
        for t, ms in top5:
            print(f"    T={t:.3f}  {ms:.0f}ms")

    print("\nPhase summary:")
    print(f"  {'phase':<42s}  {'wall':>8s}  {'#mcu':>5s}  {'blocks':>6s}  {'blocked':>8s}")
    for p in T.phases:
        print(f"  {p['label']:<42s}  {p['duration_ms']:>7.0f}ms  "
              f"{p['mcu_count']:>5d}  {p['loop_blocks_count']:>6d}  "
              f"{p['loop_blocks_total_ms']:>7.0f}ms")

    if T.rx_samples:
        # Per-second rx rate
        print("\nRX rate (frames received per 1s window, snapshot every 250ms):")
        # Group into 1s buckets
        if len(T.rx_samples) >= 2:
            t0 = T.rx_samples[0][0]
            buckets = collections.defaultdict(int)
            prev_count = T.rx_samples[0][1]
            for t, c in T.rx_samples[1:]:
                bucket = int(t - t0)
                buckets[bucket] += (c - prev_count)
                prev_count = c
            for bucket in sorted(buckets):
                bar = "#" * min(60, buckets[bucket] // 5)
                print(f"  T+{bucket:>3d}s  {buckets[bucket]:>4d}  {bar}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main_async(args):
    backend = libusb_package.get_libusb1_backend()
    dev = None
    id_entry = None
    for vid, pid, desc in USB_IDS_MT76X0U:
        found = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if found is not None:
            dev = found
            id_entry = DeviceID(vid, pid, desc)
            break
    if dev is None:
        print("[FATAL] no MT76x0U device found")
        return 2

    print(f"[*] Found {id_entry.description} ({id_entry.vid:04x}:{id_entry.pid:04x})")
    driver = MT76x0UDriver.from_usb_device(dev, id_entry)

    print("[*] driver.connect() — warm or cold, force-resets either way")
    ok = await driver.connect()
    if not ok:
        print("[FATAL] connect() returned False — see logs")
        return 3

    # Install instrumentation AFTER connect (so we don't include init in MCU latency histo)
    install_mcu_instrumentation(driver)
    install_transport_instrumentation(driver)
    print("[*] instrumentation installed")

    if args.no_rx_drainer and driver._rx_drainer is not None:
        print("[*] stopping background RxDrainer (--no-rx-drainer set)")
        await driver._rx_drainer.stop()
        driver._rx_drainer = None

    # Start background monitors
    stop = asyncio.Event()
    monitor_task = asyncio.create_task(event_loop_monitor(stop))
    rx_sampler_task = asyncio.create_task(rx_drainer_sampler(driver, stop))

    try:
        if args.workload in ("bare", "both"):
            await workload_bare(driver)
        if args.workload in ("uisim", "both"):
            await workload_uisim(driver, None)
        if args.workload == "longhop":
            await workload_longhop(driver, args.longhop_seconds,
                                    hop_interval=args.hop_interval)
    finally:
        stop.set()
        await asyncio.gather(monitor_task, rx_sampler_task, return_exceptions=True)
        print_report()
        await driver.close()

    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", choices=["bare", "uisim", "longhop", "both"],
                        default="both",
                        help="Which scenario to run (default: both)")
    parser.add_argument("--longhop-seconds", type=float, default=30.0,
                        help="Duration for --workload longhop (default 30)")
    parser.add_argument("--hop-interval", type=float, default=0.25,
                        help="Sleep between hops (default 0.25s = UI default)")
    parser.add_argument("--no-rx-drainer", action="store_true",
                        help="Stop the background RxDrainer before workload "
                             "starts (tests RxDrainer/MCU USB contention hypothesis)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if not args.debug else logging.DEBUG,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
