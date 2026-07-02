# src/ code audit — handoff

A slow, one-file / one-section-at-a-time review of `src/wifit3/**` (excluding `chips/**`)
for **overly-verbose comments** and **bad design**. Judged against the comment allowlist in
`docs/porting/CODE-STYLE.md`. Interactive by design: link a section, show it, decide, edit,
move on. Started 2026-07-01.

## Scope
- **In:** `errors.py`, `__init__.py`, `__main__.py`, `wlan/`, `engine/`, `ui/`, `setup/`,
  `src/wifit3/scripts/`.
- **Out:** `chips/**` (driver ports — held to a different, cite-the-source comment bar) and
  top-level `scripts/**` (throwaway dev tooling; already reviewed separately).

## Progress (as of 2026-07-02)
Committed to `master`:
- `6d6237a1` — pkg top level (`__init__`, `__main__`, `errors`)
- `34a663b8` — `wlan/`: `channels`, `packet_stats`, `wep_store` comment audit + a
  `wep_store` honesty rename (`record_arp_candidate`→`record_broadcast_frame`,
  `_arp_seen`→`_broadcast_seen`)
- `55110d95` — `interface.py`: `_on_frame_parsed` decomposed (T1) + **phantom-client fix**
- `cc1e7670` — **multicast-MAC-as-client fix**
- `d09fd2f4` — **T2**: parser returns a typed `Packet` hierarchy instead of a dict
- `wlan/manager.py` — comment audit clean; `_match_driver` loop de-shadowed (`.values()`,
  dropped the unused+shadowed dict-key `entry`). Resolved the `wlan/__init__.py` open
  question: the `WlanInterface`/`WlanDeviceManager` re-exports were **dead** (production imports
  `WlanDeviceManager` from `wlan.manager`; tests import `WlanInterface` from `wlan.interface`;
  nothing does `from wifit3.wlan import …`). Blanked `__init__.py` (matches the empty
  `ui/__init__.py` precedent) — chose delete-over-fix (YAGNI). **Watch:** `engine/__init__.py`
  has the *same* unused re-export pattern (`AccessPoint`) — check consumers when we reach `engine/`.

`interface.py`, `manager.py`, and `channels`/`packet_stats`/`wep_store` are fully audited.

## Route remaining (in order)
1. `wlan/packet.py` — the parser. **Includes the T2.25 comment pass** (see below).
2. `engine/` — start with the contracts (`models.py`, `protocols.py`), then the rest, then
   the big `engine/attacks/**` subtree (deepest comments; most likely to hold both gems and
   verbose stragglers).
3. `ui/` — largest by file count but "docstrings only" per CODE-STYLE, so should move fast.
4. `setup/` + `src/wifit3/scripts/`.

## Parked refactors
- **T2.25 — comment pass on `packet.py`.** Deferred *because* T2 rewrote the file; it's now
  stable, so it's ready for the normal audit lens. Fold into step 2 above.
- **T2.5 — collapse the two EAPOL types.** `engine.models.EapolFrame` (the persisted
  handshake record) and the parser's `EapolPacket` overlap ~80% but genuinely differ:
  `EapolFrame` has a load-bearing `timestamp` (binds frames to one handshake instance) and
  `replay_hex` (str); `EapolPacket` has the base 802.11 fields + `replay_counter` (bytes) and
  no timestamp. Kept **out of T2** to avoid touching crackability / `hc22000` / `save` (the
  "get this wrong and the project is worthless" code). Direction if pursued: rename
  `models.EapolFrame` → `StoredEapolFrame`, add `timestamp` to `EapolPacket`, then decide
  whether to merge. Deliberate, standalone effort.
- **T3 — unify the two `register_rx_callback` layers.** Two methods share the name with
  different contracts: `Driver.register_rx_callback(Callable[[Packet], None])` (one subscriber,
  the interface) vs `WlanInterface.register_rx_callback(Callable[[bytes, int, float], None])`
  (raw-frame fan-out to attacks). The interface re-broadcasts a *lossy* raw projection, so
  attacks re-parse. Direction: fan the full `Packet` through the interface subscription and
  disambiguate the names. Cross-driver Protocol change — reaches into `chips/**`; its own
  session. Note: attacks do **not** consume the parsed frame today (they hand-parse raw or
  read `AccessPoint` state), so T2 didn't touch them; T3 would.

## Recurring comment anti-patterns to hunt (found repeatedly)
- **References to implementation elsewhere** — naming another function/component and asserting
  what it does (rots, and was sometimes already wrong, e.g. a docstring promising a "toast"
  that isn't shown).
- **Documenting the way things used to be** — a comment guarding against a past mistake that
  won't recur.
- **Stale milestone/roadmap markers** in code (e.g. "future M7" for already-shipped work).
- Prefer naming over commenting; when unsure, omit.

## Notes / parked non-blockers
- **Scanner sibling-SSID flip-flop** (`scanner.py` ~500, hidden-AP `"A1?"`/`"B2?"` guess
  flipping in dense-BSSID environments): left alone by choice — it's a *guess*, and the flip
  signals uncertainty. Not in the audit's path.
- **`mt76x0u` `scan_channels` / `drain_bulk_in_parsed`**: bespoke methods that exist only to
  feed a throwaway hardware-test script. Migrated to typed access in T2, but the deeper smell
  (script-serving methods living in the driver) is a separate cleanup if ever wanted.
