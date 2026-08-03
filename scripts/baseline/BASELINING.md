# Baselining: wifit3 vs Linux

Compare our userland driver against the Linux/Kali stack on the same card, back-to-back, so a
difference is the driver or the RF, not the test tooling. Run on Kali. The grading that consumes
these numbers is `docs/GRADING.md`.

## Scripts
- `baseline_wifit3.py` bring up our driver, sweep the channels, write `wifit3-<chip>.json`.
- `baseline_linux.py` airmon-ng/iw to monitor + lock channel, `tcpdump -w` per channel
  (`--capture`), or read existing pcaps (`--pcap`); write `linux-<chip>.json`.
- `baseline_diff.py` the core both collectors import: both call `feed(ts, parsed, rssi, channel)`,
  so grouping is identical. Writes the JSON rollup, and `--diff wifit3-<chip>.json linux-<chip>.json`
  prints the comparison.

Both sides pass the raw 802.11 to the same `WlanFrameParser`; `baseline_linux.py` just skips the
pcap's radiotap header first (RSSI lives there). tcpdump, not airodump: airodump dedups beacons to
one per AP and drops radiotap. So a gap in the numbers is the driver or the RF, never a parser split.

Fixed filenames, no timestamps: re-running a card overwrites its file, so the diff never goes stale.
The overwritten run isn't lost. `to_json` first copies it into `history/` (gitignored, stamped with
that run's mtime), so a per-card history accrues.

Pin the reference AP by BSSID (supplied at runtime, kept out of git). If it isn't on its expected
channel, the run is invalid (the test router hops channels): reject it, don't score a zero.

## What it measures, per card
Each line reads `value | gap from Linux | gap from the best card so far`:
- Breadth: access points heard, 2.4 and 5 GHz separately.
- Beacon rate: beacons/sec from the reference AP.
- RSSI: per-BSSID against Linux. Same card, so a consistent gap is a decode bug.
- Channel tune: `N/N channels heard their own beacons | silent | cross-channel`.

Per-channel numbers stay in the JSON; the terminal shows the rollup. "Best card" is just the max
across the JSONs collected so far: no matrix or letter grades (that's GRADING.md).
