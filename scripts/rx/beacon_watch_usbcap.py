"""Beacon extraction from a usbmon RE capture: the cross-chip RX-health check.

`capture.py`'s `capture-N.pcap` is a usbmon dump of the *kernel* driver. During
its fixed-channel-1 monitor segment, every received 802.11 frame crosses the USB
bus as bulk-IN payload, wrapped in that chip's RX descriptor. The 802.11 header
survives verbatim inside the wrapping, so we recover beacons by scanning the raw
`usb.capdata` byte stream for the beacon signature: **no per-chip descriptor
decode**, so this works on any chipset's capture. This is the *only* way to get a
true per-second beacon rate out of these captures: airodump's own `.cap`
deduplicates beacons to one-per-AP, and `capture-N.pcap` is USB, not over-air.

    # primary: pin one BSSID, get its true per-second beacon series + % loss
    uv run python scripts/rx/beacon_watch_usbcap.py CAP.pcap --bssid 11:22:33:44:55:66

    # discovery: no BSSID -> report the best-heard AP (a frequency floor drops
    #            the descriptor-noise singletons; only needed without --bssid)
    uv run python scripts/rx/beacon_watch_usbcap.py CAP.pcap

The signature is `8000 <dur> ffffffffffff <SA> <BSSID>` (beacon frame-control,
broadcast DA, transmitter, BSSID), matched in lowercase no-colon hex. Pinning a
`--bssid` makes the BSSID match exact (false-positive odds ~2^-48), so the floor
is bypassed entirely. Renders identically to `beacon_watch.py` (shared
`summarize`) so a usbmon baseline and our live driver compare directly. Writes
nothing to disk; BSSIDs are runtime args only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from beacon_watch import summarize  # noqa: E402  (shared histogram renderer)

# One beacon per 102.4 ms beacon interval (100 TU), the single-AP wire ceiling.
BEACON_CEILING = 9.77

# 8000 = beacon frame-control; <dur dur>; ffffffffffff = broadcast DA (Addr1);
# <SA 6B = Addr2>; capture group = BSSID (Addr3, 6B). No-colon lowercase hex.
_SIG = re.compile("8000[0-9a-f]{4}ffffffffffff[0-9a-f]{12}([0-9a-f]{12})")


def _norm_bssid(b: str) -> str:
    return b.replace(":", "").replace("-", "").lower()


def _fmt_bssid(hx: str) -> str:
    return ":".join(hx[i:i + 2] for i in range(0, 12, 2))


def _tshark_bulk_in(pcap: str):
    """Yield (time_relative, endpoint, capdata_hex) for every bulk-IN transfer."""
    cmd = [
        "tshark", "-r", pcap,
        "-Y", "usb.transfer_type==0x03 && usb.endpoint_address>=0x80",
        "-T", "fields", "-e", "frame.time_relative",
        "-e", "usb.endpoint_address", "-e", "usb.capdata",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        print("[-] tshark not found. Install wireshark/tshark.", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        print(f"[-] tshark failed: {e.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        t_str, ep, hexdata = parts
        if not hexdata:
            continue
        try:
            yield float(t_str), ep, hexdata.lower()
        except ValueError:
            continue


def _fixed_window(pcap: str):
    """(start, stop) of the FIXED-CH1 segment in pcap-relative seconds, or None.

    `capture.py` logs `[<epoch>] [FIXED-CH1] start/stopped` to the sibling
    `<stem>_logs/main.log`; clipping to it measures the 15 s the card actually
    sat on channel 1, so the reception % isn't diluted by bring-up/idle.
    """
    p = Path(pcap)
    log = p.parent / f"{p.stem}_logs" / "main.log"
    if not log.exists():
        return None
    start = stop = None
    for line in log.read_text(errors="replace").splitlines():
        if "[FIXED-CH1]" not in line:
            continue
        m = re.match(r"\[(\d+\.\d+)\]", line)
        if not m:
            continue
        if "start" in line:
            start = float(m.group(1))
        elif "stop" in line:
            stop = float(m.group(1))
    if start is None or stop is None:
        return None
    out = subprocess.run(
        ["tshark", "-r", pcap, "-c", "1", "-T", "fields", "-e", "frame.time_epoch"],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        t0 = float(out)  # epoch of frame 1, to rebase absolute -> relative
    except ValueError:
        return None
    return (start - t0, stop - t0)


def extract_events(pcap: str, *, endpoint: str | None):
    """Scan bulk-IN capdata for beacon signatures.

    Returns (events, ep_hits, endpoint): events is [(t, bssid_hex)] for the
    chosen endpoint; ep_hits maps endpoint -> match count. When no endpoint is
    given, auto-picks the one with the most beacon hits (airmon re-enumerates the
    card into monitor mode, so the RX endpoint isn't fixed across captures).
    """
    rows = list(_tshark_bulk_in(pcap))
    ep_hits: dict[str, int] = {}
    for _t, ep, hexdata in rows:
        n = len(_SIG.findall(hexdata))
        if n:
            ep_hits[ep] = ep_hits.get(ep, 0) + n
    if endpoint is None and ep_hits:
        endpoint = max(ep_hits, key=ep_hits.get)
    events: list[tuple[float, str]] = []
    for t, ep, hexdata in rows:
        if endpoint and ep != endpoint:
            continue
        for bssid in _SIG.findall(hexdata):
            events.append((t, bssid))
    return events, ep_hits, endpoint


def main() -> int:
    p = argparse.ArgumentParser(
        description="Beacons/sec for one BSSID (or the best-heard) recovered from a "
                    "usbmon capture. Cross-chip; no descriptor decode. No files written.",
    )
    p.add_argument("pcap", help="usbmon capture (capture-N.pcap)")
    p.add_argument("--bssid", default=None,
                   help="Pin one BSSID (primary use). Exact match, no frequency floor.")
    p.add_argument("--endpoint", default=None,
                   help="Bulk-IN endpoint (e.g. 0x81). Default: auto-detect busiest.")
    p.add_argument("--min-beacons", type=int, default=20,
                   help="Discovery only (no --bssid): drop BSSIDs seen < N times as "
                        "descriptor noise. Default 20.")
    p.add_argument("--full", action="store_true",
                   help="Scan the whole capture instead of clipping to the FIXED-CH1 "
                        "segment (reception %% is diluted by bring-up/idle then).")
    args = p.parse_args()

    events, ep_hits, ep = extract_events(args.pcap, endpoint=args.endpoint)
    if not events:
        print("No beacon signatures found in any bulk-IN transfer.")
        print(f"  (endpoints with hits: {ep_hits or 'none: wrong pcap, or RX not captured'})")
        return 1
    print(f"[*] bulk-IN endpoint {ep}  (hits per endpoint: {ep_hits})", file=sys.stderr)

    window = None if args.full else _fixed_window(args.pcap)
    if window:
        lo, hi = window
        events = [(t, b) for t, b in events if lo <= t <= hi]
        print(f"[*] clipped to FIXED-CH1 window ({hi - lo:.0f}s on ch1)", file=sys.stderr)
        if not events:
            print("No beacons inside the FIXED-CH1 window. Try --full.")
            return 1
    elif not args.full:
        print("[*] no _logs/main.log FIXED-CH1 window, scanning whole capture "
              "(reception % diluted by bring-up/idle; use --full to silence).",
              file=sys.stderr)

    target = None
    if args.bssid:
        target = _norm_bssid(args.bssid)
        events = [(t, b) for t, b in events if b == target]
        if not events:
            print(f"[!] BSSID {args.bssid} produced 0 beacons on endpoint {ep}.")
            return 1
    else:
        counts = Counter(b for _, b in events)
        keep = {b for b, n in counts.items() if n >= args.min_beacons}
        events = [(t, b) for t, b in events if b in keep]
        print(f"[*] discovery: {len(keep)} BSSIDs >= {args.min_beacons} beacons kept, "
              f"{len(counts) - len(keep)} below floor dropped as noise", file=sys.stderr)
        if not events:
            print(f"No BSSID reached the {args.min_beacons}-beacon floor. "
                  "Lower --min-beacons or pin --bssid.")
            return 1

    # Colon-format for summarize's labels/ranking; rebase time to first beacon.
    events = [(t, _fmt_bssid(b)) for t, b in events]
    t0 = min(t for t, _ in events)
    events = [(t - t0, b) for t, b in events]
    n_secs = int(max(t for t, _ in events)) + 1
    summarize(events, n_secs, _fmt_bssid(target) if target else None,
              source_label=Path(args.pcap).name, expected_per_sec=BEACON_CEILING)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        sys.exit(130)
