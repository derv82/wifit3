# Card health check — wifit3 vs Linux

Two scripts compare our userland driver against the Linux/Kali stack on the **same card,
back-to-back**, so a difference is the driver or the RF — never the test tooling. Run on Kali.

## Scripts
- `baseline-wifit3.py` — bring up our driver, sweep the channels, write `wifit3-<chip>.json`.
- `baseline-linux.py` — `airmon-ng`/`iw` to monitor + lock channel, `tcpdump -w` per channel
  (`--capture`) or read existing pcaps (`--pcap`); write `linux-<chip>.json`.
- `driver_health.py` — shared core: both collectors call `feed(ts, parsed, rssi, channel)`, so
  grouping is identical. It writes the JSON rollup and prints the diff.

Fixed filenames, no timestamps — re-running a card overwrites its file, so the diff never goes
stale. The overwritten run isn't lost: `to_json` first copies it into `history/` (gitignored,
stamped with that run's mtime), so driver-health history accrues over time for every card.

## Rules
- **One parser, both sides.** `WlanFrameParser.parse_80211_frame()` takes raw bytes.
  `baseline-linux.py` reads the pcap itself: skip the radiotap header (RSSI is in it), hand the
  802.11 to the same parser. No tshark — same parser both sides means a gap is the driver/RF.
- **`tcpdump`, not airodump** — airodump dedups beacons to one per AP and drops radiotap.
- **Reference AP pinned by BSSID.** If it isn't on the expected channel, that run is invalid
  (don't score a zero) — the test router hops channels.
- The comparison **prints to the terminal** in plain sentences — a pure function of the two
  JSONs, so it's recomputable on demand and never stored (can't go stale). The two JSONs are
  the only thing written.

## What it measures, per card
Each line reads `value | gap from Linux | gap from the best card so far`:
- **Breadth** — access points heard, 2.4 and 5 GHz separately.
- **Beacon rate** — beacons/sec from the reference AP.
- **RSSI** — per-BSSID against Linux. Same card, so a consistent gap is a decode bug.
- **Channel tune** — `N/N channels heard their own beacons | silent | cross-channel`.
- **Injection** — IVs/sec (WEP replay) and deauth landed. You run these (aireplay).

Per-channel numbers stay in the JSON; the terminal shows the rollup. "Best card" is just the max
across the JSONs collected so far — no matrix, no letter grades.
