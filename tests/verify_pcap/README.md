# verify_pcap framework (parked)

Parked 2026-11-04. Work in progress: it does not replace `scripts/porting/verify_pcap.py`, and it may never do so.

Goal: replay a Linux usbmon capture against the real Python driver on a fake USB device, with no driver logic in
the test, so it can replace the per-chip `scripts/porting/` replay scripts.

## State on this branch (master's drivers)

The framework was built against the driver changes on branch `driver-verify-fixes`, not master's drivers.
On master it does not collect cleanly, and no driver capture passes.

`uv run pytest tests/verify_pcap --continue-on-collection-errors`: 38 failed, 60 passed, 3 skipped, 25 xfailed,
7 collection errors. All 60 passes are the framework's own unit tests and `tests/scripts/test_clean_pcap.py`.

| Driver | Result |
|---|---|
| rt3070, rt5370, rt5372, rt5572 | Collection error: `HRTIMER_TASKS` names `_tx_sta_fifo_timeout`, which exists only on `driver-verify-fixes` |
| mt7921au, mt7925au, `test_mediatek_connac_decoder.py` | Collection error: `constants.EP_OUT_AC_BE` exists only on `driver-verify-fixes` |
| ar9271_v2 | 5 failed, 5 xfail |
| rtl8188eus_dkms, rtl8812au_dkms, rtl8814au_dkms, rtl8821au_dkms, rtl8822bu_dkms, rtl8822cu | All failed |
| rtl8821cu_dkms | 3 failed, 1 xfail |
| rt2800usb | All xfail |
| rt2500usb, rtl8187 | All xfail: decoders added, not yet made to pass any existing capture |
| rtl8192fu | Skipped: captures not on this machine |
| `test_rt2800_decoder.py` | 6 of 12 failed |

## Captures

Every test points into the untracked `driver_captures/` directory at the repo root. No one else has it, so on CI
or a fresh clone every capture test skips. Committing cleaned captures under `tests/verify_pcap/<driver>/assets/`
(`src/wifit3/scripts/clean_pcap.py`) was planned and not started.

## Drivers with no test yet

Rough size per driver, measured against the existing decoders (95-265 lines each). None of this has been checked:

| Driver | Decoder | Estimate |
|---|---|---|
| rtl8812au, rtl8821au, rtl8822bu, rtw88_8814au (mainline-derived) | Probably `realtek_decoder.py` | Small: a test file each |
| rtl8188eus (rtl8xxxu) | Probably `realtek_decoder.py` | Small to medium |
| mt7601u, mt76x0u, mt76x2u | New mt76 USB vendor-request decoder | Medium: comparable to `rt2800_decoder.py` |
| rtl8922au | New rtw89 decoder | Large |

## Known gaps

- Tests depend on driver internals (`HRTIMER_TASKS`, constants), the same coupling the old scripts had.
- Timing: the replay models the Linux timer wheel, jiffies, hrtimer latency and races (`virtual_clock_loop.py`,
  `capture_replay.py`). That makes it brittle (tolerances, jitter) and makes it miss order errors at the same time.
- Order is not enforced while a driver timer task is mid-tick. A request that differs from the cursor matches any
  later identical op, with no distance bound (`capture_replay.py` `match_request`, pass-over).
- xfail is non-strict and not pinned to an op: an earlier divergence in an xfailed capture still reports xfail.
- A missing capture is a skip, not a failure.
- Nothing `close()` sends is compared (`driver_closing`).
- Bulk-IN RX is ignored; the driver's RX parser is never exercised (the old tool does feed RX through it).
- About 4,400 lines, against about 2,000 for `scripts/porting/`.

## Open question

Finished (every decoder, committed captures), it would probably cover more than the old scripts. It also adds a
lot of configuration (waivers, probe replies, hrtimer task lists) and assumptions about device state.
Linux equivalence is a porting aid, not a measure of a good port: ports diverge from the Linux C on purpose
(see `docs/porting/HACKS.md` on `driver-verify-fixes`). Driver changes need a hardware reason, not a replay divergence.

## Related branch

`driver-verify-fixes`: the same framework plus the driver changes it was built against, all untested on
hardware. See `DRIVER-VERIFY-FIXES.md` on that branch.
