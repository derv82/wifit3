# scripts/diag

Hardware measurement tooling: run one card, get numbers (unplug the rest). Feeds
`docs/SUPPORTED-HARDWARE.md`; the grading process that reads these numbers is
`docs/GRADING.md`. The vs-Linux baseline tooling now lives in `scripts/baseline/`.

```
scripts/diag/
├─ sweep.py                Main run: hop channels, count frames/BSSIDs per channel, then an N-min
│                          soak; writes a report .md + .csv to reports/. Drives probes/.
├─ probes/                 Sweep's measurements: baseline (per-channel yield), longrun (soak trend),
│                          parse_quality (OUI + beacon-channel sanity on delivered frames).
├─ report.py               Renders sweep's probe results into the .md + .csv.
├─ reports/                Sweep output, gitignored. Filename: <chip>_<YYYYmmdd>-<HHMMSS>.{md,csv}.
├─ soak_all.py             Runs sweep across several cards, one cold-boot at a time.
├─ beacon_watch.py         Live beacons/sec off the card: a quick RX pulse-check.
└─ beacon_watch_usbcap.py  Same count from a driver_captures/ capture (A/B vs the kernel).
```
