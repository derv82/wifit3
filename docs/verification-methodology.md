# Verification methodology

How each card in `VERIFICATION.md` earns its row. That doc is **results only** — grades, per-card
notes, the matrix. This doc is the **process** behind it: the columns, the grade rubric, the per-card
checklist, and the metric definitions. Not auto-loaded; open when running a verification pass or
editing `VERIFICATION.md`.

The tooling lives in `scripts/diag/` (`baseline-linux.py`, `baseline-wifit3.py`, `driver_health.py`);
`scripts/diag/HEALTH-CHECK.md` documents those scripts. This doc is the layer above them: what we
measure, how we score it, how we fill a card's subsection.

## Two axes, kept separate

A card can be bad for two unrelated reasons, and the table must say which:

- **Port fidelity** — wifit3 vs the Linux kernel driver *on the same card*. A gap here is our port
  leaving performance on the table (fixable). This is the **Port** column.
- **Hardware ceiling** — how good the card is at all, even under the best (Linux) driver, vs the
  field. If Linux itself is weak on this silicon, no port can save it. This feeds the **Grade**.

Example: RTL8814AU is a faithful port of a weak card (`Port ✅`, low Grade); RTL8188EUS is a
lower-fidelity port of a capable card (`Port ⚠️`, Grade dinged by the RX gap).

## Columns (top-level matrix)

- **RX** (was "Scan") — the card's receive health: beacon rate, breadth, channel tune. `✅ / ⚠️ / ❌`.
- **Handshake · PMKID · WEP · WPS · ACKs · Stress** — capability status (`✅` works · `⚠️`
  works-with-caveat · `❌` broken · `⬜` not run).
- **Port** — fidelity vs the kernel driver on the same card: `✅` matches Linux · `⚠️` trails Linux
  (work to chase) · `⬜` no Linux baseline captured yet.
- **Grade** — `NN% (Letter)`. The card as a tool for someone choosing hardware.

## Grade rubric (human-computed, not a script)

A consistency anchor, applied holistically — never run blindly. The percentage is a weighted blend,
each sub-score 0–100, scored **relative to the best card in the field** (so "it technically works"
lands low, not at an A):

- **RX health — 35%.** Beacon rate off the reference AP as a fraction of the best card's rate
  (~9–10 bcn/s ceiling), plus breadth (APs/band vs best) and channel tune (heard/total).
- **Capabilities — 45%.** Handshake, PMKID, WEP, WPS, ACKs — each `✅`=full, `⚠️`=half, `❌`=0. The
  WEP sub-score scales with sustained IVs/s vs the best card (a 60-IVs/s cracker is worse than a
  350-IVs/s one even though both "work"). Do **not** add a separate penalty for a missing capability
  (e.g. no auto-ACK) — it is already paid for by PMKID/WPS/ACKs scoring low here.
- **Stability — 20%.** Soak: flat=100, mild taper=50, decays=0.

Letters: **A ≥90 · B 80–89 · C 70–79 · D 60–69 · F <60.** **Hard cap:** a card that can't usefully
receive (`RX ❌`) caps at **D** regardless of the rest — a blind radio isn't a B.

The peer set defines "best," so early grades are provisional and the bar tightens as more cards are
measured. Re-check anchor cards when the field grows.

## Per-card checklist

Run per card; **document into that card's subsection at each step, overwriting stale data.** The
top-level matrix is *not* touched until every card is done.

1. **Linux baseline first** (card bound to its kernel driver). Verify the bound driver matches the one
   we ported from (`modinfo` / the source bundle in `usb_dumps_new/`), else the comparison is
   apples-to-oranges. Run `baseline-linux.py --capture` over the card's channels.
2. **Replug into wifit3-ready state** (install rules, then *physically replug* — stale warm state
   carries over otherwise), confirm beacons. Run `baseline-wifit3.py` over the **same** channels.
3. **Compare** — `driver_health.py --diff wifit3-<slug>.json linux-<slug>.json` → the **Port %** and
   the RX numbers (beacon rate, breadth, channel tune, RSSI).
4. **TX attacks** (user runs live TX): Deauth, PMKID, WPS PBC, WEP (2.4 GHz). On 5 GHz: Deauth,
   PMKID, WPS PBC (WEP skipped — no 5 GHz WEP target). Record sustained IVs/s from the WEP crack.
5. **Soak** — 20-min sustained hop (`sweep.py --skip-baseline --longrun-min 20`); flat = pass.
   Deferrable: it's the last RX datapoint, so grade the rest first and backfill (grade
   provisional-on-soak until it lands).

**Channels.** Sweep the same list on both sides. Under the US regulatory domain the Linux monitor
capture can't tune 2.4 GHz ch12–13 (kernel-disabled), so the comparable set is **ch1–11 + 5 GHz
non-DFS**. wifit3's userland tuning *can* reach 12–13, but they're excluded for parity and carry no
real-world APs in a US environment.

## Metric definitions

- **Beacon rate** — beacons/sec from a **pinned reference AP** (by BSSID, supplied at runtime — kept
  out of git). Current references: `ref2g` (2.4 GHz, ch1) and `ref5g` (5 GHz, ch149; can drift — reject
  the run if it's off-channel). Read straight from the baseline rollup.
- **Breadth / channel tune / RSSI** — from `driver_health` (APs per band; channels that heard their
  own beacons / silent / cross-channel; median RSSI delta vs Linux on shared APs).
- **Sustained IVs/s** — `total_IVs / ARP-replay-window` (replay start → crack). The card's TX
  throughput ceiling under load; the number that predicts time-to-crack.
- **Deauth sent/ACKed** — *(deferred; tracked in `planning/FEATURES.md`)* unicast-deauth ACK count in
  a post-burst window; a reachability readout, observational (not a grade input).

## DKMS vs mainline

Some Realtek cards ship both a DKMS-source port and a mainline port (`WIFIT3_<CHIP>=mainline` opts
into the non-default). Two distinct comparisons:

- **Justify the default** — wifit3-dkms vs wifit3-mainline, both userland, no kernel driver needed.
- **True Port %** — wifit3-<variant> vs the Linux **same** driver; needs that kernel driver installed
  (in-tree Ralink is already present; out-of-tree Realtek DKMS drivers must be installed — source
  versions are in `usb_dumps_new/driver-sources/`). Where the matching driver isn't installed, note
  `Port` as measured against mainline instead — a cross-driver number, not true fidelity.

## Subsection template

Each card's entry under `## Per-card notes`:

```
### <CHIP>
*<adapter> · <bands>*

> **<headline caveat, if any — e.g. "5 GHz TX is dead">**

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **NN% (L)** | <date> | One-line why; "provisional (soak pending)" until the soak runs. |
| RX | ✅/⚠️/❌ | <date> | Beacon rate vs linux (pinned ref APs); breadth vs kernel. |
| Port | ✅/⚠️/⬜ | <date> | Fidelity vs the same kernel driver; the gap, if any. |
| Handshake / PMKID / WEP / WPS / ACKs | … | … | 2.4 + 5 GHz where applicable; the WEP note carries sustained IVs/s. |
| Stress | … | … | 20-min soak. |

Keep the heading bare (`### <CHIP>`) — the top-level matrix links to its anchor. Grade and Port are
table rows, never the heading.
```
