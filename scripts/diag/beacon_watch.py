"""Beacon-rate watch — the quick "did my change help or hurt?" RX check.

Two modes:

  * **Live** (default): bring up the detected card, sit on one channel for a
    fixed window, and tally beacons in 1-second buckets — measures *our*
    userland driver.
  * **Offline** (``--pcap FILE``): read an over-the-air capture (e.g. the
    fixed-channel airodump pcap that ``capture.py`` drops on the Kali box)
    and build the same histogram via tshark — measures whatever driver made
    the pcap. Live and offline render identically, so a kernel-driver
    baseline and our driver compare directly.

Either way the headline is **one BSSID's** per-second histogram: whichever
AP was heard best (most beacons), unless you pin one with ``--bssid``. That
answers the portable question — "what's the highest beacons/sec this card
can hear here?" — instead of an inflated all-APs sum.

    uv run python scripts/diag/beacon_watch.py
    uv run python scripts/diag/beacon_watch.py --bssid 11:22:33:44:55:66
    uv run python scripts/diag/beacon_watch.py --pcap captures_x/capture-1_logs/airodump-fixed-ch1.cap

Writes nothing to disk. BSSIDs are runtime args only — never hardcode one.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402
from wifit3.dot11.parser import WlanFrameParser  # noqa: E402

_BAR_MAX = 40  # cap the bar so a busy second can't wrap the terminal


class BeaconCollector:
    """RX callback: record (monotonic_ts, bssid) for every beacon frame."""

    def __init__(self) -> None:
        self.events: list[tuple[float, str]] = []

    def __call__(self, raw: bytes, rssi: int, ts: float) -> None:
        try:
            parsed = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:
            return
        if not parsed or parsed.type != "beacon":
            return
        bssid = (parsed.bssid or "").lower()
        if bssid:
            self.events.append((time.monotonic(), bssid))


def extract_events_from_pcap(pcap_path: str) -> list[tuple[float, str]]:
    """tshark -> [(time_relative, bssid)] for every beacon in the pcap.

    ``frame.time_relative`` is seconds from the capture's first frame — i.e.
    from when the card started sitting on the channel — so a slow RX ramp
    shows up as leading low/empty buckets, exactly like live mode.
    """
    cmd = [
        "tshark", "-r", pcap_path,
        "-Y", "wlan.fc.type_subtype == 0x08",
        "-T", "fields", "-e", "frame.time_relative", "-e", "wlan.bssid",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        print("[-] tshark not found — install wireshark/tshark.", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        print(f"[-] tshark failed: {e.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    events: list[tuple[float, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        t_str, bssid = parts
        bssid = bssid.strip().lower()
        if not bssid:
            continue
        try:
            events.append((float(t_str), bssid))
        except ValueError:
            continue
    return events


def summarize(events, n_secs, target_bssid, *, source_label, expected_per_sec=None) -> None:
    """Render one BSSID's per-second histogram + stats + a per-BSSID ranking.

    ``events`` are (t_seconds_from_start, bssid). The reported BSSID is
    ``target_bssid`` if given, else the one with the most beacons.
    """
    if not events:
        print("No beacons captured.")
        return
    counts = Counter(b for _, b in events)
    if target_bssid:
        chosen = target_bssid.lower()
        how = "specified"
    else:
        chosen = counts.most_common(1)[0][0]
        how = "best-seen"

    buckets = [0] * max(1, n_secs)
    for t, b in events:
        if b != chosen:
            continue
        idx = int(t)
        if 0 <= idx < len(buckets):
            buckets[idx] += 1

    print(f"\n[*] {source_label}: BSSID {chosen} ({how})")
    for sec, n in enumerate(buckets, 1):
        bar = "#" * min(n, _BAR_MAX) + ("+" if n > _BAR_MAX else "")
        print(f"  sec {sec:>3}: {n:>3} /s  {bar}")
    print("-" * 52)
    total = sum(buckets)
    mean = total / len(buckets)
    median = statistics.median(buckets)
    stdev = statistics.pstdev(buckets) if len(buckets) > 1 else 0.0
    zero_secs = sum(1 for b in buckets if b == 0)
    print(f"  total={total}  mean={mean:.1f}  median={median:g}  "
          f"min={min(buckets)}  max={max(buckets)}  stdev={stdev:.1f}  "
          f"zero-seconds={zero_secs}")
    if expected_per_sec:
        # A beacon every 102.4 ms (~9.77/s) is the wire ceiling for one AP;
        # mean/ceiling is how much of the AP's beacon stream this card caught.
        recv = 100.0 * mean / expected_per_sec
        print(f"  reception: {recv:.0f}% of {expected_per_sec:g}/s "
              f"({max(0.0, 100 - recv):.0f}% beacon loss)")

    print("\nBeacons by BSSID this window (top 10):")
    for bssid, n in counts.most_common(10):
        mark = "  <- reported" if bssid == chosen else ""
        print(f"  {bssid}  {n}{mark}")
    if target_bssid and chosen not in counts:
        print(f"\n[!] Target {chosen} produced 0 beacons (see list above).")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Beacons/sec histogram for the best-heard (or a chosen) BSSID. "
                    "No files written.",
    )
    p.add_argument("--pcap", type=str, default=None,
                   help="Offline mode: read beacons from this capture via tshark "
                        "instead of bringing up the card.")
    p.add_argument("--channel", type=int, default=1,
                   help="(live mode) Channel to tune to (default: 1).")
    p.add_argument("--duration", type=float, default=15.0,
                   help="(live mode) Watch window in seconds (default: 15).")
    p.add_argument("--bssid", type=str, default=None,
                   help="Report this BSSID instead of the best-heard one.")
    p.add_argument("--no-dig", action="store_true",
                   help="(live mode) Disable the driver's DIG/AGC watchdog before connect "
                        "(no-op for drivers without one) — isolates its RX effect.")
    p.add_argument("--debug", action="store_true", help="DEBUG logging (live mode).")
    return p


def run_pcap(args) -> int:
    events = extract_events_from_pcap(args.pcap)
    n_secs = int(max((t for t, _ in events), default=0)) + 1
    summarize(events, n_secs, args.bssid, source_label=Path(args.pcap).name)
    return 0


async def run_live(args) -> int:
    print("[*] Discovering interfaces...", file=sys.stderr)
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] No supported devices found.", file=sys.stderr)
        return 1
    if len(ifaces) > 1:
        print(f"[!] {len(ifaces)} interfaces — using {ifaces[0].name}. "
              "Unplug others to test in isolation.", file=sys.stderr)
    iface = ifaces[0]

    def _progress(pct: float, msg: str) -> None:
        print(f"  [{int(pct * 100):3d}%] {msg}", file=sys.stderr)

    if getattr(args, "no_dig", False):
        if hasattr(iface.driver, "enable_dig"):
            iface.driver.enable_dig = False
            print("[*] DIG/AGC watchdog DISABLED for this run.", file=sys.stderr)
        else:
            print(f"[!] --no-dig ignored: {type(iface.driver).__name__} has no DIG watchdog.",
                  file=sys.stderr)

    print(f"[*] Bringing up {iface.name} ({iface.description})...", file=sys.stderr)
    try:
        ok = await iface.connect(progress_cb=_progress)
    except Exception as e:  # noqa: BLE001 — bring-up can raise on USB error
        print(f"[-] Bring-up failed: {e}", file=sys.stderr)
        await mgr.close_all()
        return 1
    if not ok:
        print("[-] Bring-up returned False.", file=sys.stderr)
        await mgr.close_all()
        return 1

    collector = BeaconCollector()
    iface.register_rx_callback(collector)
    print(f"\n[*] {iface.name}: watching CH{args.channel} for {args.duration:g}s",
          file=sys.stderr)
    if not await iface.set_channel(args.channel):
        print(f"[-] set_channel({args.channel}) failed.", file=sys.stderr)
        await mgr.close_all()
        return 1

    start = time.monotonic()
    n_secs = max(1, round(args.duration))
    try:
        for sec in range(1, n_secs + 1):
            gap = (start + sec) - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            print(f"\r  watching... {sec}/{n_secs}s  {len(collector.events)} beacons",
                  end="", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    print("", file=sys.stderr)
    await mgr.close_all()

    events = [(t - start, b) for t, b in collector.events if t >= start]
    summarize(events, n_secs, args.bssid, source_label=iface.name)
    return 0


if __name__ == "__main__":
    _args = _build_parser().parse_args()
    if _args.debug:
        import logging
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        # The per-frame [RXFRAME] trace floods at DEBUG (hundreds/s). Pin it to INFO so
        # the DIG/DM watchdog + power-track trace stays readable; everything else stays
        # at DEBUG.
        logging.getLogger("wifit3.wlan.interface").setLevel(logging.INFO)
    try:
        if _args.pcap:
            _rc = run_pcap(_args)
        else:
            _rc = asyncio.run(run_live(_args))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        _rc = 130
    sys.exit(_rc)
