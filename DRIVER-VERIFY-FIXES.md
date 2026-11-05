# Branch driver-verify-fixes (parked, reference only)

Parked 2026-11-04. Do not merge as a whole.

218 commits on top of master: driver changes made to match the Linux C, found by the replay framework in
`tests/verify_pcap/` (state and gaps: `tests/verify_pcap/README.md` on branch `verify-framework`).
Use it as a reference when an issue comes in: what changed when a port was moved toward Linux, and whether
that change would help.

**Untested on hardware**, except where noted below. Several changes removed hardware-found behaviour and
broke RX on hardware: see `docs/porting/HACKS.md`.

## Commits

| Split | Commits |
|---|---|
| `src/wifit3/chips/` only | 88 |
| `tests/` only | 86 |
| Both, or other files | 44 |

Files changed in `src/wifit3/chips/` per driver: rtl8821au_dkms 14, rtl8822bu_dkms 12, ar9271_v2 12,
rtl8812au_dkms 11, rtl8821cu_dkms 9, mt7921au 8, mt7925au 8, rtl8188eus_dkms 6, rt5572 6, rtl8814au_dkms 4,
rt5372 4, rt5370 4, rt3070 4, rtl8822cu 3.

One driver's changes: `git log master..driver-verify-fixes -- src/wifit3/chips/<driver>`

## Hardware results (2026-11-04, 1 card each)

| Change | Result |
|---|---|
| mt7921au `close()` releases the USB interface (ea93c8e8) | Works: same-process reconnect went from Errno 13 to working, 3/3 runs |
| mt7925au `close()` releases the USB interface (42b52aca) | Not tested |
| rtl8822bu_dkms `_heal_cold_synth` removed (261bf57c) | Regression: 3/9 warm connects deaf on ch1-14 until the first 5 GHz hop |
| rtl8822bu_dkms spur eliminator re-enabled (12276433) | Regression, not interleaved: ch153 2.5-15.2 bcn/s vs 13.6-20.9 before; ~30 ms more per hop on some channels |
| mt7921au FIX_RATE for 5 GHz unicast data removed (76bce75d) | Regression: 5 GHz unicast data inject 0/50 seen, 0 ACKs |

## Tests on this branch

- `uv run pytest tests/verify_pcap`: 124 passed, 4 skipped (captures not on this machine), 27 xfailed.
- `uv run pytest tests --ignore=tests/verify_pcap`: 4763 passed, 29 skipped, 10 xfailed.

Capture tests read the untracked `driver_captures/` directory; without it they skip.

## Picking a change up

One commit at a time, onto a branch from master. Keep it only with a hardware reason and a live A/B on the same
card (master vs the change, interleaved in one session), not because a replay divergence went away.
