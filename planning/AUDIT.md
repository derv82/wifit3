# src/ code audit — handoff

A slow, **section-by-section** review of `src/wifit3/**` (excluding `chips/**`) for
**overly-verbose comments** and **bad design**, judged against the comment allowlist in
`docs/porting/CODE-STYLE.md`. Interactive by design: link one small section, show it, decide,
edit, move on — one section at a time, never a broad sweep or a file-wide question.

## Scope
- **In:** `errors.py`, `__init__.py`, `__main__.py`, `wlan/`, `engine/`, `ui/`, `setup/`,
  `src/wifit3/scripts/`.
- **Out:** `chips/**` (driver ports — held to a different, cite-the-source comment bar) and
  top-level `scripts/**` (throwaway dev tooling; already reviewed separately).

## Route remaining (in order)
1. `ui/` — largest by file count but "docstrings only" per CODE-STYLE, so should move fast.
2. `setup/` + `src/wifit3/scripts/`.

`engine/` is fully audited. Its `attacks/**` subtree got a **structural sweep only** (dead
code + factually-wrong comments), not the full verbose-comment gate — by choice, since the
attack docs are intentionally exhaustive (pedagogy + bug-transparency). Revisiting them for
verbosity is optional, not planned.

## Parked refactors
- **T3 — unify the two `register_rx_callback` layers.** Two methods share the name with
  different contracts: `Driver.register_rx_callback(Callable[[Packet], None])` (one subscriber,
  the interface) vs `WlanInterface.register_rx_callback(Callable[[bytes, int, float], None])`
  (raw-frame fan-out to attacks). The interface re-broadcasts a *lossy* raw projection, so
  attacks re-parse. Direction: fan the full `Packet` through the interface subscription and
  disambiguate the names. Cross-driver Protocol change — reaches into `chips/**`; its own
  session. Attacks do **not** consume the parsed frame today (they hand-parse raw or read
  `AccessPoint` state), so it's untouched so far.

## Comment bars (calibrated so far — apply to the remaining files)
- **Data-class docstrings / field comments state only what the class holds** — not who consumes
  it or where related state lives. Cut consumer roll-calls (UI widgets, other engine modules,
  save/persistence); keep magic-value notes + intra-class relationships.
- **References to implementation elsewhere** — naming another function/component and asserting
  what it does; rots, and is sometimes already wrong.
- **Documenting the way things used to be** — a comment guarding against a past mistake.
- **Stale wording** — e.g. "parsed-frame dict" / "rich dictionary" left over from the
  pre-`Packet` parser; "future M7" for shipped work.
- Prefer naming over commenting; when unsure, omit.

## Notes / parked non-blockers
- **`_format_encryption_label` transition/PSK-name edge** (`packet.py`): `transition_mode`
  (flag) keys on `_PSK_SUITES` (incl. FT-PSK / SHA384 = 0x04/0x13/0x14), but the label's
  `has_psk` only matches the plain `"PSK"`/`"PSK-SHA256"` names — so an SAE + FT-PSK-only AP
  (no plain PSK, rare) gets `transition_mode=True` yet a `"WPA3-SAE"` label. Deferred: rare,
  and a fix touches crackability-adjacent labeling that needs its own test.
- **Scanner sibling-SSID flip-flop** (`scanner.py` ~500, hidden-AP `"A1?"`/`"B2?"` guess
  flipping in dense-BSSID environments): left alone by choice — it's a *guess*, and the flip
  signals uncertainty. Not in the audit's path.
- **`mt76x0u` `scan_channels` / `drain_bulk_in_parsed`**: bespoke methods that exist only to
  feed a throwaway hardware-test script. Typed access already migrated, but the deeper smell
  (script-serving methods living in the driver) is a separate cleanup if ever wanted.
