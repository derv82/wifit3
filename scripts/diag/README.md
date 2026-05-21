# scripts/diag — chipset diagnostic harness

Driver-agnostic probes that drive `WlanInterface` and dump a Markdown
report + CSV per run. Reports land in `scripts/diag/reports/`
(gitignored). One card at a time — unplug the rest.

## Current state (today)

`sweep.py` is a thin CLI over `probes.ALL_PROBES`. Each probe owns
its own CLI flags, verdict line, report section, and CSV section.
One report + CSV per run lands in `scripts/diag/reports/`.

```
scripts/diag/
  sweep.py            ← thin CLI (discover → connect → run probes → render)
  probes/
    base.py           ← Probe protocol + shared helpers
    baseline.py       ← yield-per-channel probe (active)
    longrun.py        ← multi-minute hop probe (active)
    parse_quality.py  ← BSSID OUI + beacon-channel sanity (passive)
  report.py           ← markdown + CSV renderer (walks each probe)
  reports/            ← gitignored output
```

Diag is dev tooling — it lives under `scripts/` deliberately so it
doesn't get bundled into the shipped `wifit3` package. The agent +
senior lead are the only intended runners; end users never call this.

```powershell
# Defaults: every enabled probe runs in registry order
uv run python scripts/diag/sweep.py

# Discover what's registered
uv run python scripts/diag/sweep.py --list-probes

# Skip-flags are auto-generated per probe
uv run python scripts/diag/sweep.py --skip-baseline --longrun-min 30
uv run python scripts/diag/sweep.py --skip-longrun
uv run python scripts/diag/sweep.py --skip-parse-quality

# Scale every duration arg by a single factor — "longevity" mode
uv run python scripts/diag/sweep.py --duration-multiplier 3

# Per-probe flags
uv run python scripts/diag/sweep.py --dwell-sec 5
uv run python scripts/diag/sweep.py --channels 1,6,11,36,149
uv run python scripts/diag/sweep.py --death-timeout-sec 30
```

### What the probes measure

**baseline** (active) — for each `driver.SUPPORTED_CHANNELS` channel,
tune, sleep 250ms (AGC), dwell `--dwell-sec`, count:
- raw RX frames in the dwell window (any frame, parsed or not)
- BSSIDs whose beacon advertises this channel and whose `last_seen`
  falls inside the window
- mean RSSI across those BSSIDs

Silent channels → broken tune (channel-set, AGC, PLL, EFUSE).

**longrun** (active) — start normal hopping for `--longrun-min`
minutes, snapshot every second, bucket into `--bucket-sec` windows
(default 60s). Each bucket reports median "active BSSIDs" (whose
`last_seen` falls inside the rolling bucket window), split into 2.4 vs
5 GHz. Monotonic decline → "BSSIDs disappear after hopping" bug.

Trend ratio (`median(last-3) / median(first-3)`) only considers
buckets that saw at least `bucket_sec / 2` snapshots, so the partial
teardown bucket (death-detect or Ctrl-C) doesn't skew it. Full table
is still rendered, partial buckets get a `partial` marker.

**Death detect** — if no frames arrive for `--death-timeout-sec`
consecutive seconds (after at least that long of runtime), the
long-run exits early and the verdict flags `DEATH DETECTED`. Caught
RTL8812AU's t≈27s full-RX wedge on 2026-05-20.

**parse-quality** (passive) — piggybacks on whatever active probes
ran. For every frame the iface delivers (i.e. post-driver-parse), it
inspects:
- **OUI sanity** on the BSSID — rejects multicast, all-zero, all-FF,
  all-same-byte. Catches bit-flip BSSIDs from split-MPDU decoder bugs
  and RX-DMA underruns.
- **Beacon channel consistency** — beacon's DS Param IE primary
  channel vs the chip's currently-tuned channel. Mismatches indicate
  loose tune (RF hearing an adjacent channel) or misordered RX queue.

Verdict WARNs above 1% garbage OUIs or 20% beacon-channel mismatch.
First 5 garbage BSSIDs surface in the detail section + CSV.

### Verdict flags

- **Silent channels** — baseline tune succeeded but zero frames
- **Tune failures** — `set_channel()` returned False
- **Trend ratio** — see longrun; WARN if < 0.5 over trend-eligible buckets
- **Death detected** — long-run cut short
- **OUI garbage** — parse-quality, WARN > 1% of frames
- **Beacon channel mismatch** — parse-quality, WARN > 20% of beacons

### Known limitations

- Frame counter is raw bulk-IN deliveries. parse-quality only sees
  frames the driver+parser already accepted — pre-parse-failure rate
  needs a per-driver raw tap (Phase 1.5).
- RSSI mean is reported verbatim from the driver — there is no
  cross-card calibration check, so impossible values (RTL8822BU +11
  dBm, RTL8188EUS -98 dBm on strong APs) flow straight through.
- No cross-run / cross-card comparison — every report is a single
  card's data, and `scripts/diag/reports/*.md` is unstructured text.

## Where this is going

The harness was started small and accreted features. Goal is a
proper diagnostics suite — modular probes, one entry point, a story
for cross-card comparison. Phases below are deliberately scoped; we
are NOT designing the full DB schema upfront.

### Phase 0 — refactor into a real probe registry — DONE 2026-05-21

Modular probes under `scripts/diag/probes/`, each one a `Probe`
protocol implementation. `sweep.py` is now a thin CLI: discover →
connect → attach → run → finalize → render.

Earlier plans had this living under `src/wifit3/diag/`. That was wrong
— diag is dev tooling, doesn't ship in the runtime package, and
matches the project's existing convention (`scripts/<chipset>/`).

CLI additions:
- `--list-probes` — print probe names and exit
- `--skip-<name>` — auto-generated per probe
- `--duration-multiplier N` — single flag every probe respects

Bundled in: partial-teardown bucket bias fix (longrun trend now
excludes any bucket with fewer than `bucket_sec / 2` snapshots).
`SUPPORTED_CHANNELS` per-chip fix landed separately in `3f4dd00`.

### Phase 1 — parse-quality probe — DONE 2026-05-21

`probes/parse_quality.py`. Passive — hooks `iface.register_rx_callback`,
inspects the driver+parser output for OUI sanity + beacon channel
consistency. See "What the probes measure" above for the verdict
thresholds.

**Hard scoping note**: the iface only delivers post-parse frames to
its rx callbacks, so this probe sees a filtered view. Pre-parse
failure rate isn't measurable here — that's Phase 1.5.

### Phase 1.5 — pre-parse tap + FCS validation (designed, not built)

Two upgrades that both need driver-level changes:

1. **Pre-parse tap** — every driver's RX loop calls a "raw mpdu
   delivered" hook BEFORE `WlanFrameParser.parse_80211_frame`. Lets
   parse-quality measure parse-failure rate as a percentage of
   delivered MPDUs (the metric we actually want — "card delivers
   garbage that still counts as frames"). Implementation: add
   `register_raw_rx_callback` on `WlanInterface`; each driver fires
   it for every mpdu in its `iter_bulk_frames` loop, regardless of
   parse outcome.

2. **FCS validation** — per-driver `STRIPS_FCS: bool` capability +
   CRC-32 check inside parse-quality when the driver doesn't strip
   it. Catches descriptor-decoder off-by-one errors that leave the
   MPDU otherwise plausible but with a bad trailing CRC.

Worth doing once one of: (a) a card surfaces a "looks fine but isn't"
parse failure we can't see today, (b) parse-quality verdict is too
noisy on healthy cards because of beacon-channel-mismatch hopping
artifacts.

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
