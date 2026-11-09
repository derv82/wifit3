"""Beacon trend: one card under test against reference cards, all recording the same seconds.

Every run starts with BRINGUP_DWELL_SECONDS on --channel right after connect. Then each cycle every card
hops like the scanner for SCAN_MINUTES (APs and beacons heard per minute),
then all cards dwell on --channel for DWELL_MINUTES. Per dwell: --bssid's beacons/s per minute
for each card, and a per-AP table (each card's top 10 by beacons/s, worst gap vs the references
first). An AP is "near" when every reference hears at least half of the beacons its interval
promises. The first --card is the card under test. Ends with zoomed per-minute histograms.

    uv run python scripts/rx/beacon_trend.py --card 8822CU 7925 8821AU --bssid 11:22:33:44:55:66

Text goes to stdout; --json also writes the per-second series. BSSIDs are runtime args only.
"""
from __future__ import annotations

import argparse
import json
import asyncio
import statistics
import struct
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py

from dev import select_device

from wifit3.chips.driver import Driver
from wifit3.device.manager import wlan_ifaces, wlan_close

SCAN_MINUTES = 2
DWELL_MINUTES = 10
BRINGUP_DWELL_SECONDS = 60
HOP_INTERVAL = 0.25          # the scanner's hop interval (ui/screens/scanner.py)
DEFAULT_BEACON_INTERVAL_TU = 100
TOP_APS_PER_CARD = 10
NEAR_FRACTION = 0.5
_BAR_WIDTH = 40


@dataclass
class CardTally:
    beacons: Counter = field(default_factory=Counter)          # second -> beacons, all APs
    target_beacons: Counter = field(default_factory=Counter)   # second -> --bssid beacons
    aps_by_minute: dict = field(default_factory=lambda: defaultdict(set))
    ap_beacons: Counter = field(default_factory=Counter)       # bssid -> beacons
    ap_ssid: dict = field(default_factory=dict)
    ap_interval_tu: dict = field(default_factory=dict)


@dataclass
class Phase:
    name: str
    cycle: int
    is_scan: bool
    seconds: int
    labels: list[str]
    start: float = 0.0
    tallies: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tallies = {label: CardTally() for label in self.labels}


class PhaseRecorder:
    """Tallies every card's beacons into the phase being recorded."""

    def __init__(self, target_bssid: str) -> None:
        self.target_bssid = target_bssid.lower()
        self.phase: Phase | None = None

    def callback_for(self, label: str):
        def on_packet(pkt) -> None:
            self.add(label, pkt)
        return on_packet

    def add(self, label: str, pkt) -> None:
        phase = self.phase
        if phase is None or not pkt or pkt.type != "beacon":
            return
        second = int(time.monotonic() - phase.start)
        bssid = (pkt.bssid or "").lower()
        tally = phase.tallies[label]
        tally.beacons[second] += 1
        tally.aps_by_minute[second // 60].add(bssid)
        tally.ap_beacons[bssid] += 1
        if pkt.ssid:
            tally.ap_ssid[bssid] = pkt.ssid
        if len(pkt.raw) >= 34:
            tally.ap_interval_tu[bssid] = struct.unpack_from("<H", pkt.raw, 32)[0]
        if bssid == self.target_bssid:
            tally.target_beacons[second] += 1

    async def record(self, phase: Phase) -> Phase:
        phase.start = time.monotonic()
        self.phase = phase
        await asyncio.sleep(phase.seconds)
        self.phase = None
        return phase


def minute_buckets(counter: Counter, minute: int) -> list[int]:
    return [counter[second] for second in range(minute * 60, minute * 60 + 60)]


def target_minute_stats(tally: CardTally, minute: int) -> tuple[float, int, int]:
    """(mean, min, zero-seconds) of --bssid's beacons/s over one minute."""
    buckets = minute_buckets(tally.target_beacons, minute)
    return statistics.mean(buckets), min(buckets), buckets.count(0)


def print_scan(phase: Phase) -> None:
    print(f"\n[*] {phase.name}: APs heard / beacons")
    print("  minute  " + "  ".join(f"{label:>14}" for label in phase.labels))
    for minute in range(phase.seconds // 60):
        cells = []
        for label in phase.labels:
            tally = phase.tallies[label]
            cells.append(f"{len(tally.aps_by_minute[minute]):>5} / {sum(minute_buckets(tally.beacons, minute)):>6}")
        print(f"  {minute + 1:>6}  " + "  ".join(f"{cell:>14}" for cell in cells))
    cells = []
    for label in phase.labels:
        tally = phase.tallies[label]
        cells.append(f"{len(tally.ap_beacons):>5} / {sum(tally.ap_beacons.values()):>6}")
    print("   total  " + "  ".join(f"{cell:>14}" for cell in cells))


def print_dwell_target(phase: Phase, target_bssid: str) -> None:
    print(f"\n[*] {phase.name}: {target_bssid} beacons/s per minute (mean  min  zero-s)")
    print("  minute  " + "  ".join(f"{label:>14}" for label in phase.labels))
    for minute in range(phase.seconds // 60):
        cells = []
        for label in phase.labels:
            mean, low, zero_secs = target_minute_stats(phase.tallies[label], minute)
            cells.append(f"{mean:>4.1f} {low:>3} {zero_secs:>3}")
        print(f"  {minute + 1:>6}  " + "  ".join(f"{cell:>14}" for cell in cells))


def expected_per_sec(phase: Phase, bssid: str) -> float:
    interval_tu = DEFAULT_BEACON_INTERVAL_TU
    for tally in phase.tallies.values():
        if tally.ap_interval_tu.get(bssid):
            interval_tu = tally.ap_interval_tu[bssid]
            break
    return 1.0 / (interval_tu * 1.024e-3)


def ap_rows(phase: Phase) -> list[dict]:
    """Each card's top APs by beacons/s, worst gap (test minus reference mean) first."""
    test_label, reference_labels = phase.labels[0], phase.labels[1:]
    chosen: set[str] = set()
    for label in phase.labels:
        chosen.update(bssid for bssid, _ in phase.tallies[label].ap_beacons.most_common(TOP_APS_PER_CARD))
    rows = []
    for bssid in chosen:
        rates = {label: phase.tallies[label].ap_beacons[bssid] / phase.seconds for label in phase.labels}
        expected = expected_per_sec(phase, bssid)
        if reference_labels:
            reference_mean = statistics.mean(rates[label] for label in reference_labels)
            near = all(rates[label] >= NEAR_FRACTION * expected for label in reference_labels)
        else:
            reference_mean = rates[test_label]
            near = rates[test_label] >= NEAR_FRACTION * expected
        ssid = next((t.ap_ssid[bssid] for t in phase.tallies.values() if t.ap_ssid.get(bssid)), "")
        rows.append({"gap": rates[test_label] - reference_mean, "bssid": bssid, "ssid": ssid,
                     "near": near, "expected_per_sec": expected, "per_sec": rates})
    rows.sort(key=lambda row: row["gap"])
    return rows


def print_ap_table(phase: Phase) -> None:
    print(f"\n[*] {phase.name}: beacons/s per AP (top {TOP_APS_PER_CARD} of each card; "
          f"gap = {phase.labels[0]} - reference mean)")
    print(f"  {'ssid':<16} {'bssid':>5}  dist  exp/s  " + "  ".join(f"{label:>10}" for label in phase.labels) + "    gap")
    for row in ap_rows(phase):
        cells = "  ".join(f"{row['per_sec'][label]:>10.2f}" for label in phase.labels)
        print(f"  {row['ssid'][:16]:<16} {row['bssid'][-5:]:>5}  {'near' if row['near'] else 'far':<4}  "
              f"{row['expected_per_sec']:>5.2f}  {cells}  {row['gap']:>+5.2f}")


def write_json(path: Path, phases: list[Phase], cards, labels: list[str], args) -> None:
    """Per-second series for plot_trend.py; times are seconds from the first phase's start."""
    run_start = phases[0].start if phases else 0.0
    doc = {
        "target_bssid": args.bssid.lower(),
        "channel": args.channel,
        "cards": [{"label": label, "description": iface.description} for iface, label in zip(cards, labels)],
        "phases": [],
    }
    for phase in phases:
        entry = {
            "name": phase.name, "cycle": phase.cycle, "is_scan": phase.is_scan,
            "start_s": round(phase.start - run_start, 3), "seconds": phase.seconds,
            "cards": {},
        }
        for label in labels:
            tally = phase.tallies[label]
            entry["cards"][label] = {
                "target_beacons_per_sec": [tally.target_beacons[s] for s in range(phase.seconds)],
                "beacons_per_sec": [tally.beacons[s] for s in range(phase.seconds)],
                "aps_per_minute": [len(tally.aps_by_minute[m]) for m in range(phase.seconds // 60)],
            }
        if not phase.is_scan:
            entry["ap_table"] = ap_rows(phase)
        doc["phases"].append(entry)
    path.write_text(json.dumps(doc, indent=1))
    print(f"[+] wrote {path}", file=sys.stderr)


def print_zoomed_histograms(title: str, rows_by_label: dict[str, list[tuple[str, float]]]) -> None:
    """One chart per card; all charts share one scale, zoomed to the values' min..max."""
    values = [value for rows in rows_by_label.values() for _, value in rows]
    if not values:
        return
    low, high = min(values), max(values)
    span = high - low
    for label, rows in rows_by_label.items():
        print(f"\n[#] {title}: {label}  (bars span {low:g} .. {high:g})")
        for row_label, value in rows:
            if span:
                width = 1 + round((value - low) / span * (_BAR_WIDTH - 1))
            else:
                width = _BAR_WIDTH
            print(f"  {row_label}  {value:>6.1f}  {'#' * width}")


def print_trends(phases: list[Phase]) -> None:
    if not phases:
        return
    labels = phases[0].labels
    target_rows = {label: [] for label in labels}
    ap_rows = {label: [] for label in labels}
    for phase in phases:
        for minute in range(phase.seconds // 60):
            row_label = f"c{phase.cycle} {'scan ' if phase.is_scan else 'dwell'} m{minute + 1:02}"
            for label in labels:
                tally = phase.tallies[label]
                if phase.is_scan:
                    ap_rows[label].append((row_label, len(tally.aps_by_minute[minute])))
                else:
                    target_rows[label].append((row_label, target_minute_stats(tally, minute)[0]))
    print_zoomed_histograms("dwell: --bssid mean beacons/s per minute", target_rows)
    print_zoomed_histograms("scan: APs heard per minute", ap_rows)


async def run(args) -> int:
    print("[*] Discovering interfaces...", file=sys.stderr)
    ifaces = wlan_ifaces()
    if not ifaces:
        print("[-] No supported devices found.", file=sys.stderr)
        return 1
    cards = []
    for card in args.card:
        iface = select_device([i for i in ifaces if i not in cards], card)
        if iface is None:
            await wlan_close(ifaces)
            return 1
        cards.append(iface)

    def _progress(pct: float, msg: str) -> None:
        print(f"  [{int(pct * 100):3d}%] {msg}", file=sys.stderr)

    # Sequential: two bring-ups at once collide on USB (see scripts/ack/tx_retries.py).
    for iface in cards:
        print(f"[*] Bringing up {iface.name} ({iface.description})...", file=sys.stderr)
        try:
            if not await iface.connect(progress_cb=_progress):
                print(f"[-] Bring-up returned False: {iface.description}", file=sys.stderr)
                await wlan_close(ifaces)
                return 1
        except Exception as e:  # noqa: BLE001, USB bring-up can raise
            print(f"[-] Bring-up failed: {iface.description}: {e}", file=sys.stderr)
            await wlan_close(ifaces)
            return 1

    labels = []
    for iface in cards:
        label = iface.chipset or iface.name
        if label in labels:
            label = f"{label}#{labels.count(label) + 1}"
        labels.append(label)
    recorder = PhaseRecorder(args.bssid)
    for iface, label in zip(cards, labels):
        iface.register_rx_callback(recorder.callback_for(label))
    print(f"[*] test card {labels[0]} ({cards[0].description}); references: "
          + (", ".join(f"{label} ({iface.description})" for iface, label in zip(cards[1:], labels[1:])) or "none"))
    print(f"[*] {BRINGUP_DWELL_SECONDS} s dwell after bring-up, then {args.cycles} cycle(s) of "
          f"{SCAN_MINUTES} min scan + {DWELL_MINUTES} min dwell "
          f"on ch{args.channel}, target {args.bssid}")
    phases: list[Phase] = []

    async def tune_all() -> bool:
        tuned = True
        for iface, label in zip(cards, labels):
            if not await iface.set_channel(args.channel):
                print(f"[-] {label}: set_channel({args.channel}) failed", file=sys.stderr)
                tuned = False
        return tuned

    try:
        print(f"[*] bring-up dwell: {BRINGUP_DWELL_SECONDS} s on ch{args.channel}", file=sys.stderr)
        if not await tune_all():
            return 1
        phases.append(await recorder.record(Phase(f"cycle 0 dwell ch{args.channel} after bring-up", 0,
                                                  False, BRINGUP_DWELL_SECONDS, labels)))
        print_dwell_target(phases[-1], args.bssid)
        print_ap_table(phases[-1])
        for cycle in range(1, args.cycles + 1):
            print(f"[*] cycle {cycle}: scanning", file=sys.stderr)
            for iface in cards:
                hop_channels = [c for c in iface.supported_channels if c not in Driver.CHANNELS_DFS]
                await iface.start_hopping(channels=hop_channels, interval=HOP_INTERVAL)
            phases.append(await recorder.record(Phase(f"cycle {cycle} scan", cycle, True,
                                                      SCAN_MINUTES * 60, labels)))
            for iface in cards:
                await iface.stop_hopping()
            print_scan(phases[-1])
            print(f"[*] cycle {cycle}: dwelling on ch{args.channel}", file=sys.stderr)
            if not await tune_all():
                break
            phases.append(await recorder.record(Phase(f"cycle {cycle} dwell ch{args.channel}", cycle,
                                                      False, DWELL_MINUTES * 60, labels)))
            print_dwell_target(phases[-1], args.bssid)
            print_ap_table(phases[-1])
            sys.stdout.flush()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[!] Interrupted.", file=sys.stderr)
    finally:
        await wlan_close(ifaces)
    print_trends(phases)
    if args.json:
        write_json(Path(args.json), phases, cards, labels, args)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="RX over time for a card under test against reference "
                                            "cards: hopping scans alternated with a fixed-channel dwell.")
    p.add_argument("--card", nargs="+", default=[""],
                   help="adapter substrings; the first is the card under test, the rest are references.")
    p.add_argument("--bssid", required=True, help="The AP to follow minute by minute during the dwell.")
    p.add_argument("--channel", type=int, default=1, help="Dwell channel (default: 1).")
    p.add_argument("--cycles", type=int, default=2, help="Scan + dwell cycles after the bring-up dwell (default: 2; 0 = bring-up dwell only).")
    p.add_argument("--json", default=None, help="Also write the per-second series here (for plot_trend.py).")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(message)s")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
