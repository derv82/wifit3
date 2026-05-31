# CAPTURE-MANAGER.md

Design doc + implementation plan for a `CaptureManager` — an app-owned home for
capture auto-saves that also answers the one query we actually need:
**"do we already have this AP's password?"** Written to be executed in a fresh
session; nothing here is implemented yet.

Status: **design locked, not started.** (Parked at ~420k-token session boundary.)

---

## Problem

The opportunistic WPS Push-Button (PBC) "invade" auto-captures an AP's PSK the
instant a push-button window opens — in both Scanner and Focus. The recapture
guard is weaker than it looks:

- **Scanner** dedups on `_pbc_captured` (an in-session `set`, never persisted) →
  a PSK captured *last* session does NOT block a re-invade this session.
- **Focus** dedups on `_pbc_done`, which is **cleared in `_stop_pbc_capture()`**
  (runs on every target-leave) → leave + return to a focused AP re-invades, even
  in the same session.
- The loaded `captures/` history (`ap.persisted`) drives badges but is **never
  consulted by the invade gate.**

Result: PBC can re-invade across views and across restarts. We want: **once we
have an AP's PSK, never auto-invade it again** — in-session, cross-session,
cross-view.

## Scope decision (READ FIRST — prevents over-building)

The dedup has value in **exactly one place: the automatic PBC invade.** Do NOT
add "already captured" gates anywhere else:

- **WEP** — re-cracking the same AP is intentionally allowed (user testing a new
  card against their own router). Keep it unblocked.
- **WPS PIN (manual button)** — clicking it on an already-cracked AP is a
  *feature*: it retries the known PIN, checks whether the PSK rotated, and
  notifies / auto-saves a new file if it changed. Never gate this.
- **Deauth / Generate IVs / any manual button** — never gated.

`has_psk()` gates **only the automatic PBC invade.** That's the whole feature.

(Future "Hack The Planet" war-driving mode would want broad repeat-attack
suppression — but that's YAGNI now. Noted under Future.)

## Design

Three responsibilities, kept separate:

| Concern | Owner | Notes |
|---|---|---|
| Serialize a capture → disk (dedup, multi-session-safe) | `engine/save.py` | **unchanged** — pure functions, live `_existing()` re-glob |
| Deserialize `captures/` → index at startup | `engine/capture_history.py` | **unchanged** — already extracts PSK via `_read_wps_psk` |
| Own the live lifecycle + cache "do we have a PSK?" | **`CaptureManager` (new)** | app-owned, single instance |

`CaptureManager` ties the serializer + deserializer together and holds the one
piece of in-memory state nothing else does today (`save.py` is stateless module
functions; each call re-globs — there is currently NO persistent save state).

### Class sketch (`engine/capture_manager.py`)

```python
class CaptureManager:
    """App-owned home for capture auto-saves + the 'do we have this AP's
    password?' cache. Loads prior PSKs from captures/ at startup; learns new
    ones as this session saves them. NOT a filesystem mirror — save.py remains
    the live disk authority (see Multi-session divergence)."""

    def __init__(self, captures_dir="captures"):
        self._captures_dir = captures_dir
        self._psk: dict[str, str] = {}      # bssid -> PSK (latest); seeds the gate
        self.reload()

    def reload(self) -> None:
        """Seed _psk from captures/ via capture_history.load_capture_index().
        WPS entries carry .value = PSK; that's all we cache. (WEP keys are NOT
        cached — re-cracking is allowed, so no consumer needs them.)"""
        index = load_capture_index(self._captures_dir)
        self._psk = {
            bssid: cap.value
            for bssid, caps in index.items()
            for cap in caps
            if cap.kind == "WPS" and cap.value
        }

    # ---- the one query the feature needs --------------------------------
    def has_psk(self, bssid: str) -> bool:
        """True if we hold a PSK for this AP (prior session OR this one).
        Gates ONLY the automatic PBC invade — never manual buttons."""
        return bssid.lower() in self._psk

    # ---- saves route through here so the cache stays warm ----------------
    # PSK-producing saves update the cache; the rest are thin passthroughs so
    # every auto-save has one home (matches the existing save_* dedup exactly).
    def save_wps_pbc(self, ap, psk, **kw) -> Optional[SaveResult]:
        result = save_wps_pbc(ap, psk, captures_dir=self._captures_dir, **kw)
        if result is not None and psk:
            self._psk[ap.bssid.lower()] = psk
        return result

    def save_wps_pin(self, ap, pin, psk, **kw) -> Optional[SaveResult]:
        result = save_wps_pin(ap, pin, psk, captures_dir=self._captures_dir, **kw)
        if result is not None and psk:
            self._psk[ap.bssid.lower()] = psk
        return result

    # Passthroughs (no cache effect; centralize for consistency):
    def save_handshake(self, ap, client_mac, **kw): ...   # -> save.save_handshake
    def save_pmkid(self, ap, client_mac, **kw): ...       # -> save.save_pmkid
    def save_wep_key(self, ap, key, **kw): ...            # -> save.save_wep_key
```

Owned by the app: `WifiteApp.capture_manager = CaptureManager()` (built in
`__init__` / `on_mount`). Any screen reaches it via `self.app.capture_manager`.

### Why this shape

- **Cache, not mirror.** `has_psk` answers from memory (startup snapshot + this
  session's saves). No per-query re-glob (perf), no attempt to watch disk.
- **`save.py` untouched** = its hard-won, multi-session-safe, content-fingerprint
  dedup (handshake ANonce inspection, pcap companions, live `_existing()`) keeps
  working exactly as-is. The manager delegates to it, never reimplements it.
- **PSK from any source counts.** PBC and PIN both feed `_psk`, so a PIN-cracked
  AP won't later get PBC-invaded.

## What changes (call-site migration)

**STEP 1 — audit.** `grep` for `from wifit3.engine.save import` and `save_(`
call sites. Route each through `self.app.capture_manager.save_*` instead of the
bare module function. Known sites (verify + find the rest):

- `ui/screens/focus.py`: imports `save_handshake, save_pmkid, save_wep_key,
  save_wps_pbc, save_wps_pin`; `_run_pmkid_harvest` → `save_pmkid` (~L1152);
  `_auto_capture_pbc` → `save_wps_pbc` (~L890). (Find WEP / WPS-PIN / handshake
  save sites too.)
- `ui/screens/scanner.py`: imports `save_handshake, save_pmkid, save_wps_pbc`;
  `_invade_pbc` → `save_wps_pbc` (~L682).
- WEP campaign success save site (find it — likely focus.py around the WEP
  campaign, or `engine/attacks/wep/campaign.py`).

**STEP 2 — swap the two PBC gates** to the registry, delete the in-session sets:

- `scanner.py` `_on_pbc_window` (~L651): `... or ap.bssid in self._pbc_captured`
  → `... or self.app.capture_manager.has_psk(ap.bssid)`. Delete `_pbc_captured`
  (decl ~L141 + the `.add` on success ~L676).
- `focus.py` `update_ui` (~L310): `ap.bssid not in self._pbc_done`
  → `not self.app.capture_manager.has_psk(ap.bssid)`. Delete `_pbc_done`
  (decl ~L110, `.add` ~L879) and its `.clear()` in `_stop_pbc_capture` (~L910).

Minimal functionally-required set for the feature: load PSKs at startup,
`has_psk`, and route `save_wps_pbc` + `save_wps_pin` through the manager. The
handshake/pmkid/wep passthroughs are recommended (one home for all saves) but
optional — do them if cheap, skip if they balloon the diff.

## What stays untouched

- `engine/save.py` — pure functions, the live disk dedup authority.
- `engine/capture_history.py` — the deserializer (manager reuses it).
- `CaptureEventDetector` (`ui/capture_events.py`) — UI rendering / "what changed"
  differ; a different concern from "what do we possess." Stays.
- Scanner's `ap.persisted` hydration + headline counts — full capture history for
  badges (HS/PMKID too), not just PSKs. Stays. (Optional later: let Scanner read
  the index from the manager to avoid a second `load_capture_index()` at
  startup — nice-to-have, not required.)
- WEP / manual-WPS-PIN re-attack behavior — intentionally still allowed.

## Multi-session divergence (accepted limitation — document, don't fix)

If a *concurrent* wifit3 instance saves a PSK for AP X mid-run, our cache won't
learn it → we might redundantly auto-invade X once. But `save_wps_pbc`'s live
`_existing()` check finds the other session's file and returns
`was_new=False` (no duplicate file written), and our own save then warms the
cache. Worst case = one wasted invade, never a corrupt/duplicate artifact. This
is the deliberate boundary: the manager is a cache, `save.py` is the live disk
authority. State it in the class docstring so nobody "fixes" it into a
disk-watcher later.

## Test plan (no hardware; mock the AP + a tmp captures dir)

- `reload()` seeds `_psk` from a tmp dir containing a `_wps_pbc.txt` /
  `_wps_pin.txt` → `has_psk(bssid)` True; absent → False.
- `save_wps_pbc` / `save_wps_pin` success warms the cache (`has_psk` flips
  False→True without a `reload()`).
- A failed/None save does NOT warm the cache.
- WEP key save does NOT populate `_psk` (no `has_psk` for WEP).
- bssid case-insensitivity (`has_psk` lowercases).
- Existing `save.py` tests stay green (functions unchanged).
- PBC-gate behavior: an AP with a known PSK is skipped by the invade gate;
  add a regression mirroring the in-session + cross-session cases.

## Open decisions

1. **Class name.** Recommend `CaptureManager` (the `has_psk` query reads better
   than on a `SaveManager`, and it owns both save + query). `SaveManager` was the
   original sketch — fine too. Pick one before implementing.
2. **Wrap-all-saves vs PSK-only.** Recommend wrapping all `save_*` (one home,
   thin delegations) but it's optional beyond the two PSK producers. Decide how
   far to take STEP 1.

## Explicitly OUT of scope (future phases)

- **PBC auto-invade default + toggle rework** — make Scanner auto-invade ON by
  default (global) with `w` as a binary on/off; Focus always-invades-unless-PSK.
  This intentionally violates the passive-by-default principle and requires
  editing CLAUDE.md + `feedback_passive_by_default.md` ("except WPS PBC, which
  intentionally violates this") — already approved by the lead, but deferred to
  its own session/commit.
- **Hack-The-Planet war-driving mode** — broad repeat-attack suppression across
  all attack types. YAGNI until that mode exists.
