# scripts/

Developer tooling for wifit3 chipset work: verifying driver ports offline against
recorded pcaps, and driving or diagnosing cards on real hardware. These import from
`wifit3`, but nothing in `src/` imports them, so the installed wheel stays product-only
and moving or removing a script here cannot break the app.

Run everything from the repo root, e.g. `uv run python scripts/porting/verify_pcap.py --list`.

```
scripts/
├─ porting/      Offline pcap verification. verify_pcap.py <chip> byte-diffs a driver's
│                bring-up against its recorded cold-boot capture (no hardware needed). Holds the
│                shared replay engines (rtw88 / rt2x00 / mt76usb) + pcap_slicer, peek_frame, usb_speed.
├─ chips/        One directory per chipset. Each has its verify_pcap.py recipe, test_hw.py live
│                bring-up, and any firmware/table extractors + per-chip debug probes.
├─ diag/         Hardware measurement + health harness: sweep.py soak, beacon_watch{,_usbcap}.py
│                RX A/B, driver_health + baselines.
├─ ack/          auto-ACK / ACK-retry bench probes. The model is written up in docs/ACKS.md.
├─ wps/          WPS PIN/PBC hardware ground-truth probes + a reliability lab.
├─ wep/          wep_lab.py: WEP TX-throughput tester.
├─ ui/           Rendering helpers: render_cardart.py (.ans card art -> PNG), wiffy_preview.py.
├─ generators/   Generators that (re)write source under src/ (e.g. the WPS OUI table).
└─ release.py    Bump __version__, commit, tag; pushing the vX.Y.Z tag builds + publishes.
```

One script lives *inside* the package instead of here: `src/wifit3/scripts/capture.py`, the
durable product tool that records the cold-boot pcaps + `main.log` the whole workflow consumes,
so it ships and versions with wifit3. The line is "durable product tool" (in `src/`) vs "dev
tooling" (here).
