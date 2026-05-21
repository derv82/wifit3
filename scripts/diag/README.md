# scripts/diag — chipset diagnostic harness

Driver-agnostic probes that drive `WlanInterface` and dump a Markdown
report + CSV per run. Reports land in `scripts/diag/reports/`
(gitignored). One card at a time — unplug the rest.

## Current state (today)

Single script `sweep.py` runs two probes in sequence and writes one
report + CSV per run.

```powershell
# Defaults: 10s/channel baseline + 10-min long-run hopping
uv run python scripts/diag/sweep.py

# Skip-flags follow the pattern we want to keep
uv run python scripts/diag/sweep.py --skip-baseline --longrun-min 30
uv run python scripts/diag/sweep.py --skip-longrun

# Useful flags
uv run python scripts/diag/sweep.py --dwell-sec 5
uv run python scripts/diag/sweep.py --channels 1,6,11,36,149
uv run python scripts/diag/sweep.py --death-timeout-sec 30
```

### What the probes measure

**Yield baseline** — for each `driver.SUPPORTED_CHANNELS` channel,
tune, sleep 250ms (AGC), dwell `--dwell-sec`, count:
- raw RX frames in the dwell window (any frame, parsed or not)
- BSSIDs whose beacon advertises this channel and whose `last_seen`
  falls inside the window
- mean RSSI across those BSSIDs

Silent channels → broken tune (channel-set, AGC, PLL, EFUSE).

**Long-run degradation** — start normal hopping for `--longrun-min`
minutes, snapshot every second, bucket into `--bucket-sec` windows
(default 60s). Each bucket reports median "active BSSIDs" (whose
`last_seen` falls inside the rolling bucket window), split into 2.4 vs
5 GHz. Monotonic decline → "BSSIDs disappear after hopping" bug.

**Death detect** — if no frames arrive for `--death-timeout-sec`
consecutive seconds (after at least that long of runtime), the
long-run exits early and the verdict flags `DEATH DETECTED`. Caught
RTL8812AU's t≈27s full-RX wedge on 2026-05-20.

### Verdict flags

- **Silent channels** — baseline tune succeeded but zero frames
- **Tune failures** — `set_channel()` returned False
- **Trend ratio** — `median(last-3 buckets) / median(first-3)`; WARN
  if < 0.5
- **Death detected** — long-run cut short

### Known limitations

- Trend ratio picks up the partial teardown bucket (the 1-snapshot
  bucket past `total_sec`), inflating apparent degradation.
- Frame counter is raw bulk-IN deliveries — does not distinguish
  parse-success vs parse-failure, so it can't catch "card delivers
  garbage that still counts as frames".
- RSSI mean is reported verbatim from the driver — there is no
  cross-card calibration check, so impossible values (RTL8822BU +11
  dBm, RTL8188EUS -98 dBm on strong APs) flow straight through.
- No cross-run / cross-card comparison — every report is a single
  card's data, and `scripts/diag/reports/*.md` is unstructured text.
- `SUPPORTED_CHANNELS` is per-driver, not per-chip — 2.4-only chips
  like RT5372 (PAU05) get 5G channels flagged "tune failed" even
  though the runtime is correctly rejecting them.

## Where this is going

The harness was started small and accreted features. Goal is a
proper diagnostics suite — modular probes, one entry point, a story
for cross-card comparison. Phases below are deliberately scoped; we
are NOT designing the full DB schema upfront.

### Phase 0 — refactor into a real subpackage (no behavior change)

Move the inline `sweep.py` into a proper Python subpackage:

```
src/wifit3/diag/
  probes/
    base.py            ← Probe protocol (name, run(iface, args), record())
    yield_channel.py   ← current baseline probe
    longrun.py         ← current long-run probe
  report.py            ← markdown renderer (reads probe results)
scripts/diag/sweep.py  ← thin CLI: arg parse, probe dispatch, report write
```

CLI gains `--list-probes` and `--skip-<name>` for every registered
probe. `--duration-multiplier N` is a single flag every probe respects
— "longevity" becomes "run the existing probes longer", per your idea.

**Exit criteria:** running `sweep.py` on the RT3572 produces a report
that matches the pre-refactor output byte-for-byte (modulo timestamp).

**Quick wins to bundle:**
- Fix the partial-teardown-bucket bias in the trend ratio (exclude
  buckets with fewer than `bucket_sec / 2` snapshots).
- Fix `SUPPORTED_CHANNELS` over-declaration in `chips/rt2800usb/` so
  RT5372 doesn't flag 5G as "tune failed".

### Phase 1 — parse-quality probe

Hook the `WlanFrameParser` output into the diag rx callback. Per
frame, record:
- did it parse to a valid frame?
- did the BSSID OUI match a known IEEE OUI (cheap sanity check)?
- was the FCS valid? (per-driver; some chips strip it, some don't)

New probe `parse_quality.py` aggregates these into per-run stats and
verdict flags. Surfaces the "garbage BSSIDs / bit-flip frames"
concern directly. Same shape as the existing two probes — runs by
default, opt out via `--skip-parse-quality`.

**Exit criteria:** a card known to be healthy reports >99% parse
success; a card simulating corruption (rare) is flagged.

### Phase 2+ (NOT designed yet, just flagged)

Cross-card comparison needs **per-BSSID data** persisted across runs:
- "Which channel did each card observe BSSID `xx:xx:xx:xx:xx:xx` on?"
  (frequency-drift detector — the original-spec ask)
- "What RSSI range did each card report for a given BSSID?"
  (calibration disagreement detector)
- "Which cards see BSSIDs no other card ever sees?" (likely garbage)

Open questions to decide *before* implementing:
1. **Storage** — SQLite (queryable, schema migrations) vs TOML/JSON
   (human-readable, no queries). Lean SQLite but not committed.
2. **Aggregation window** — last N runs? Time-bounded ("last 24h of
   testing")? Session-tagged? Random snapshots from months-old runs
   would be misleading.
3. **UX** — Canned subcommands (`sweep.py compare --bssid-channels`)
   so the user never writes SQL/queries. What's the right set of
   canned queries?
4. **Schema migration** — additive `ALTER TABLE` only, or rebuild
   the DB from raw CSVs when fields change?

These get nailed down once Phase 0 + Phase 1 have shipped and we
actually need them. Don't pre-commit.

## Per-chipset workflow (current)

1. Unplug everything else.
2. Plug in ONE card.
3. `uv run python scripts/diag/sweep.py` (or with flags above).
4. Paste the `scripts/diag/reports/<chipset>_<ts>.md` to the agent.

If a card wedges mid-run, Ctrl-C, replug, re-run. The driver's warm-
reattach path picks up the leftover state where it can.
