# scripts/diag

Hardware measurement tooling: run one card, get numbers (unplug the rest). Feeds
`docs/SUPPORTED-HARDWARE.md`; the grading process that reads these numbers is
`docs/verification-methodology.md`. Reports and rollups carry live BSSIDs, so they stay gitignored.

Two families, one shared helper:

```
scripts/diag/
│  # vs-Linux baselining: does our driver hear what the kernel does?
│  # run our driver and the kernel driver over the same channels, back-to-back, then diff.
├─ baseline_linux.py       Kernel side: airmon-ng + iw + tcpdump per channel -> linux-<chip>.json.
├─ baseline_wifit3.py      Our side: bring up our driver, dwell per channel -> wifit3-<chip>.json.
├─ baseline_diff.py        Core both collectors import (feed/rollup); `--diff a.json b.json` compares.
├─ BASELINING.md           How to run those three, and what each number means.
│
│  # vs-other-cards health + soak: is this card healthy, and does it stay healthy?
├─ sweep.py                Main run: hop channels, count frames/BSSIDs per channel, then an N-min
│                          soak; writes a report .md + .csv to reports/. Drives probes/.
├─ probes/                 Sweep's measurements: baseline (per-channel yield), longrun (soak trend),
│                          parse_quality (OUI + beacon-channel sanity on delivered frames).
├─ report.py               Renders sweep's probe results into the .md + .csv.
├─ reports/                Sweep output, gitignored. Filename: <chip>_<YYYYmmdd>-<HHMMSS>.{md,csv}.
├─ soak_all.py             Runs sweep across several cards, one cold-boot at a time.
├─ beacon_watch.py         Live beacons/sec off the card: a quick RX pulse-check.
├─ beacon_watch_usbcap.py  Same count from a driver_captures/ capture (A/B vs the kernel).
│
└─ _diaglib.py             Shared: card selection (--card / --instance) + reference-AP loading.
```
