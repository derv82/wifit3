# Focus View Redesign — Spec (LOCKED, pre-implementation)

Status: design locked via brainstorm + a throwaway top-row mockup
(`scratch_topbar_mockup.py`, button style + widths confirmed on hardware).
This doc is the reference; we execute in the chunks at the bottom.

## Problems being solved

1. **SECURITY / CAPTURE width** — currently `1fr`, so they float too wide on a
   widescreen and clip (WEP key) on a normal terminal. → fixed widths.
2. **CLIENTS is a space hog** — eats horizontal (empty right half) and vertical
   room, starving the EVENT LOG. → relocate, shrink, give LOG the tall column.
3. **No position jitter** — text must not reflow when values change (the thing
   that killed the centered-block experiment).

## Layout

```
┌TARGET INFO─┐┌(no title)──┐┌CAPTURE────┐     top row, fixed widths, top-aligned
│ ‹SSID chip›││ Replay Chop││ Beacons   │     content. ATTACKS has NO title
│ BSSID      ││ Save   Frag││ Power     │     (buttons self-label; Encryption
│ Ch 6 🔒    ││            ││ IVs       │     line already states the family).
│ Last bcn   ││            ││ Replay    │
└────────────┘└────────────┘└───────────┘
┌SECURITY──────────┐┌EVENT LOG───────────────────┐
│ Encryption …     ││ ChopChopping packet …       │
│ WPS · PMF        ││  ├─► …                      │   left column: fixed width
│ SAE groups       ││  └─✓ …                      │   (~34). LOG: 1fr (the one
├CLIENTS (3)───────┤│ …                           │   stretchy element), full
│ MAC      PWR PKTS││ …                           │   height of lower section.
│ ‹row highlight = ││ …                           │
│  selection›      ││ …                           │
├DEAUTH────────────┤│ …                           │
│ [Selected][Bcast]││ …                           │
└──────────────────┘└─────────────────────────────┘
```

Conceptual split that drives the placement:
- **AP-targeted attacks** (Replay/Chop/Save/Frag, PMKID/SAE/WPA↓) → ATTACKS,
  next to TARGET ("here's the target, here are your options").
- **Client-targeted action** (DEAUTH) → under CLIENTS ("pick who, deauth").
- **Feedback** (CAPTURE live stats + LOG event stream) → right side, same column
  family, so what you read and what you watch are together.

## Panel specs

| Panel | Width | Height | Content / alignment |
|---|---|---|---|
| TARGET INFO | 30 (≥28 floor: BSSID is 24) | top-row height | title + 4 lines, **top-aligned** |
| ATTACKS | 26 | top-row height (~8) | **no title**, 2×2 fat buttons |
| CAPTURE | 30 | top-row height | title + 4 lines, **top-aligned** |
| SECURITY | ~34 (left col) | auto (≤ title+5) | condensed (below) |
| CLIENTS | left col | `1fr` (gets the slack) | DataTable, scrolls |
| DEAUTH | left col | auto | 2 buttons, one horizontal row |
| EVENT LOG | `1fr` | full lower height | the stretchy panel |

Top row height ≈ **8** (2 fat button rows of 3 + border). TARGET/CAPTURE are
~7, so they top-align with one trailing blank — fine, no centering wrapper.

## Hard rules

- **Fixed-width, left-aligned panels.** No `1fr` on info panels (only LOG +
  CLIENTS-column slack + the LOG side stretch). This alone kills the block
  jitter.
- **No reflow on value change.** Each line is `Label: value`; the value sits so
  nothing trails it that would get pushed. For the count+rate case
  (`IVs: 8,901 (210/s)`) the rate is last and may nudge ~1 char on a digit
  boundary — acceptable, not block-level jitter. (Fixed-width count field is the
  fallback if even that annoys.)
- **Fat buttons** (height 3, bordered) — the attacks are the core of the view;
  full-size click targets. `min-width: 0; width: 10` (Button defaults to
  `min-width: 16`, which is what ballooned/clipped them).

## Component details

### Buttons (ATTACKS + DEAUTH)
- `width: 10; min-width: 0`. Idle labels (Replay/Chop/Save/Frag, PMKID/SAE/WPA↓)
  all fit. Variants/colors unchanged (Replay green, Stop red, Chop/Frag blue,
  Stop-sub orange).
- Running labels must fit 10: `Stop Chop`/`Stop Frag` (9) fit; **`Stop Replay`
  (11) does not** → use a short stop label, `■ Stop` (square glyph, 1 cell,
  renders everywhere — not the 🛑 emoji).

### TARGET INFO
- **SSID as a "chip"** — drop the `ESSID:` prefix, render the name on a colored
  bar (`[black on cyan] name [/]`) so short names like `NETGEAR` are visible.
- Adaptive truncation: `name` → `name…` (with `…`) to fit width 30.
- BSSID / Channel / Last-beacon keep dim labels.

### SECURITY (condensed)
WEP (≤4 lines): `Encryption` · `Fake-Auth` · `Crack` (+ key result — see open
decision). WPA (3 lines, down from 5):
```
Encryption: WPA3→2 (SAE+PSK)
WPS: Locked  ·  PMF: Required        ← merge WPS + PMF onto one line
SAE groups: 19 20 21                 ← only after a probe (often absent)
```
- **Drop the standalone `WPA3:` line** — redundant with `Encryption:`.

### CLIENTS
- **Selection = highlighted cursor row.** Remove the `[ ]/[X]` checkbox column
  and the space-to-select binding (footgun: accidental broadcast deauth).
- **Remove the ` YOU` marker** — filter our own forged STA out of the list
  entirely (it's not a real client).
- **Drop the CAPTURES/vendor column** (YAGNI; revisit if vendor-by-MAC ships).
- Title shows the count: **`CLIENTS (N)`**.
- Columns: MAC · POWER · PKTS.

### DEAUTH
- Two buttons, one horizontal row: `[Selected] [Broadcast]` (warning / error
  colors as today). `Selected` acts on the highlighted CLIENTS row; multi-select
  is gone (single row + Broadcast covers it).

### CAPTURE
- 4 lines: Beacons · Power · IVs · Replay.
- **Drop the `(N usable)`** from the IVs line — that number lives in SECURITY's
  Crack line (`N/10k usable IVs`).

### EVENT LOG
- The tree-log output (already built). Gets the full-height right column.

## Minimum size

- **Comfortable target: 100×30.** Top row (~86 wide) + tall LOG shine here.
- **Stretch goal: attempt 80×24** (SSH / nethunter / wardriving). Tightening
  TARGET 28 / ATTACKS 25 / CAPTURE 27 ≈ 80 fits width; 24 rows squeezes CLIENTS
  to ~2 rows (scrollable, acceptable). **No responsive code until the nethunter
  test proves it's needed** (YAGNI) — degrade by clipping, don't crash.

## Resolved decisions

- **Cracked-key display** → SECURITY shows a **short** status
  (`Crack: ✓ Recovered`); the **full** `✓ CRACKED WEP KEY …` banner + [c]opy/
  [s]ave leaf stays in the LOG (already there; the LOG is wide). Keeps the left
  column compact (~34) and handles 104-bit keys.

## Layout v2 — the morning plan (chunks 1–6 are built; this REORDERS them)

After living with v1, the panel ORDER felt wrong. v2 keeps all the built
substance (tree logs, single-row select, condensed SECURITY, chip, fixed
widths) and just repositions. **Recommended: Option A.** Nothing to revert.

**Decision: target 120 cols minimum.** Drop the 80×24 ambition entirely — it
was a trap. Assume a real terminal; no responsive code.

**Option A (recommended)** — restore the beloved 3-summary top row, and make
TARGET the head of a unified-width left column:

```
┌TARGET──┐┌SECURITY┐┌CAPTURE─┐    top row = 3 similar INFO panels
│ ‹chip› ││        ││        │    ("Target X, secured Y, capturing Z")
├ATTACKS─┤├────────┴────────┤
│ wide   ││                 │
├CLIENTS─┤│   EVENT LOG      │     left col (TARGET/ATTACKS/CLIENTS/DEAUTH)
│ wide   ││   (tall + wide)  │     all share ONE width (~40); LOG fills rest
├DEAUTH──┤│                 │
└────────┘└─────────────────┘
```
Why A: (1) restores the summary-top-row theme; (2) smallest change — swap
ATTACKS↔SECURITY positions; (3) ATTACKS in the wide left col → "Stop Replay"/
"Stop Frag" fit (no wrap, drop the "■ STOP" hack); (4) TARGET = CLIENTS width
→ alignment irk gone + ESSID emphasized + CLIENTS wide enough (PKTS stops
clipping); (5) all-info top row is shorter → more height for CLIENTS/LOG.

Structure: `Horizontal[ Vertical(left-col ~40: TARGET, ATTACKS, CLIENTS(1fr),
DEAUTH) | Vertical(right 1fr: Horizontal(SECURITY | CAPTURE), EVENT LOG(1fr)) ]`.

Options B and C (considered, not chosen): both put ATTACKS in the top row,
sacrificing the all-summary theme. C (log-on-left, control-on-right) is a
bigger structural rework for marginal LOG gain. Revisit only if fresh-eyes
rejects A.

Bug/nit fixes (apply under A):
- CLIENTS too narrow — `PKTS` clips. Wider left col (~40) fixes it.
- "Stop Frag"/"Stop Chop" wrap — wider ATTACKS (left col) fixes; can revert
  the Replay label from "■ STOP" back to "Stop Replay" too.
- TARGET width == left-col width (alignment) + wider to emphasize ESSID.
- "DEAUTH" → "CLIENT DEAUTH" (room now).
- ESSID chip centered under the TARGET INFO title.

## Future gold-plating (NOT now)

- 5-line animated `wifit3` ANSI art in the top-right dead space, gated on
  terminal size, pulsing the green wifi signal on each new IV. A small reactive
  hook once the skeleton is solid.

## Implementation chunks (each: build → test → hardware-verify)

1. **Compose + CSS skeleton** — new container hierarchy (top row + lower 2-col),
   fixed widths, drop ATTACKS title, top-align, move DEAUTH to lower-left, LOG to
   full-height right column. No behavior change yet.
2. **Buttons** — `width:10; min-width:0`; `■ Stop` for the Replay running label.
3. **CLIENTS** — cursor-row selection, remove checkboxes/space-binding, filter
   our STA, drop vendor col, `CLIENTS (N)` title, single-row deauth wiring.
4. **SECURITY condense** — drop WPA3 line, merge WPS+PMF, short crack status.
5. **CAPTURE** — drop `(N usable)`; confirm no-jitter ordering.
6. **TARGET ESSID chip** — drop prefix, colored chip, adaptive `…` truncation.
7. **Sweep** — full test suite + a headless-pilot layout check at 100×30 and
   80×24; then user hardware pass.
```
