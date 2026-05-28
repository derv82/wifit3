# Auto-save everything — design + handoff (2026-05-28)

Refactor scope: rip out the user-facing **Save** button and `s` hotkey; auto-save
every saveable artifact (handshake, PMKID, WEP key, WPS-PIN credential, WPS-PBC
credential); dedupe by content per kind; rename `.wps` files into the
`_wepkey.txt`-style naming convention; split mixed `.hc22000` writes into
per-kind files. Lead's design call (this is **not** an open question — just
context for the next agent so they understand the why):

> "I'm sure I had a Save button for a reason. There's 's to save WEP Key' and
> 's to save Handshake' and 's to save PMKID' and… it's just so messy, dude.
> And we auto-save some things like WPS progress, PBC/PIN cracks, no prompt from
> the user needed. It's inconsistent."

> "If we auto-saved everything: No more Save button … No more 's to save X' …
> nasty if/elif/elif on the hotkey, it's text … User never has to worry about
> the program crashing and them losing their progress."

## Audit (current state — verified, all line numbers from this commit base)

- **Scanner already has no save plumbing.** `s` binds `cycle_sort` (sort
  column), not save. The capture events drained in Scanner's
  `_log_capture_event` (`scanner.py:483`+) just *log* arrivals — no save call,
  no "press s" text. Nothing in Scanner needs to change.
- **Focus owns it all** — and this is what gets stripped:
  - `Binding("s", "save_capture", "Save Capture")` `focus.py:87`
  - `Binding("s", "save_key", "Save Key")` `focus.py:88` (two stacked bindings;
    the active one is chosen by `check_action` at `focus.py:1091`-1102)
  - `Button("Save", id="btn-save")` `focus.py:157`
  - The btn-save dynamic enable in `update_ui` at `focus.py:615`-623
  - `action_save_capture` / `action_save_key` / `on_button_pressed("btn-save")`
    `focus.py:1067`, 1105-1111
  - `_save_capture()` dispatch (`focus.py:1436`) + `_save_wep_key()`
    (`focus.py:1498`)
  - Three **"press s to save"** event-log strings: `focus.py:857`, `focus.py:863`,
    `focus.py:1215`
- **CaptureEvent kinds** (`ui/capture_events.py`): `decloak`, `eapol`,
  `handshake_complete`, `pmkid`. Handshakes + PMKIDs flow through the event
  stream; we hook the existing detector — no new RX plumbing.
- **WEP cracker DOESN'T emit a CaptureEvent.** `WepCampaign.recovered_key`
  (`wep/campaign.py:70`) is set inside the crack loop (`:185`); Focus's
  `update_ui` polls it at `focus.py:427`-430. Auto-save for WEP hooks on the
  None→bytes state transition, not on a subscription.

So: **handshake + PMKID** are CaptureEvent-driven; **WEP key** is a state-poll;
**WPS-PIN + WPS-PBC** already auto-save at the end of their attack trees.

## Design — typed `AutoSave` class

One module, `src/wifit3/engine/save.py`. One class with one typed method per
saveable kind. Captures-dir lives **inside** the class so callers don't pass it
through (and it stays in one place when it becomes configurable later).

```python
# src/wifit3/engine/save.py
from pathlib import Path
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint


class AutoSave:
    """One method per saveable kind — typed args, dedupe by content, single
    "(saved …)" log line at ROOT level when a new file is written. Idempotent:
    if an identical artifact is already on disk, returns None and logs nothing.

    Callers don't pass captures_dir; this class owns the directory location so
    a future "configurable captures path" change touches exactly one file.
    """

    def __init__(self, captures_dir: Path = Path("captures"),
                 log: Optional[Callable[[str], None]] = None):
        self.captures_dir = captures_dir
        self.log = log or (lambda _m: None)

    # ----- Per-kind typed save methods ---------------------------------------

    def save_handshake(self, ap: AccessPoint, client_mac: str,
                       anonce: bytes, frames: list[bytes]) -> Optional[Path]:
        """Dedup by (BSSID, client_mac, ANonce). Writes the .hc22000 hashline
        + companion .pcap with the EAPOL frames."""
        ...

    def save_pmkid(self, ap: AccessPoint, client_mac: str,
                   pmkid: bytes, frames: list[bytes]) -> Optional[Path]:
        """Dedup by (BSSID, client_mac, PMKID-value). Same PMK from the same
        client → same PMKID → skipped. PSK rotation gives a fresh PMKID →
        written. Writes .hc22000 + .pcap (split out from handshake)."""
        ...

    def save_wep_key(self, ap: AccessPoint, key: bytes) -> Optional[Path]:
        """Dedup by key value for this BSSID. Writes the standard
        '_wepkey.txt' format."""
        ...

    def save_wps_pin(self, ap: AccessPoint, pin: str, psk: str) -> Optional[Path]:
        """Dedup by (PIN, PSK) for this BSSID. PSK rotation under same PIN →
        new file (high-value: re-verify caught the rotation, we want it
        persisted). Writes '_wps_pin.txt'."""
        ...

    def save_wps_pbc(self, ap: AccessPoint, psk: str) -> Optional[Path]:
        """Dedup by PSK for this BSSID. Writes '_wps_pbc.txt'."""
        ...

    # ----- Internals (private; never called from outside) --------------------

    def _file_prefix(self, ap: AccessPoint) -> str:
        """'<safe_ssid>_<bssid-dashed>_<epoch>' — the common stem every kind
        builds its filename on. SSID sanitized to [A-Za-z0-9_-]{1,32}."""
        ...

    def _save_bytes(self, filename: str, payload: bytes) -> Path:
        """Resolve filename under self.captures_dir, mkdir -p, write bytes,
        log '(saved <name>)' at root level."""
        ...

    def _save_text(self, filename: str, payload: str) -> Path:
        """Same as _save_bytes but for plain-text creds files."""
        ...

    def _existing(self, bssid: str, suffix: str) -> list[Path]:
        """All files in captures_dir whose name matches this BSSID + suffix —
        used by dedupe predicates per kind."""
        ...
```

**Lifecycle**: one instance per `WifiteApp`, attached at startup
(`self.app.auto_save = AutoSave(log=…)`) so every screen / orchestrator gets it
via `self.app.auto_save.save_*(…)`.

## Dedupe per kind (locked decisions)

| Kind | Dedup key | If duplicate |
|---|---|---|
| Handshake | `(BSSID, client_mac, ANonce)` | silently skip, return None |
| PMKID | `(BSSID, client_mac, PMKID-value)` | silently skip, return None |
| WEP key | the key value for this BSSID | silently skip |
| WPS PIN | `(PIN, PSK)` for this BSSID | silently skip |
| WPS PBC | PSK for this BSSID | silently skip |

**Existence-based, no overwrites, no destructive dedupe.** Each `save_*` method
implements its own predicate over `_existing(bssid, suffix)` (read each
candidate's parseable body — hashline / PSK line / key hex — and compare).

## Filename scheme

```
<safe_ssid>_<bssid-dashed>_<epoch>_wep_key.txt         ← NEW (was _wepkey.txt)
<safe_ssid>_<bssid-dashed>_<epoch>_wps_pin.txt         ← NEW (was *.wps method=WPS-PIN)
<safe_ssid>_<bssid-dashed>_<epoch>_wps_pbc.txt         ← NEW (was *.wps method=WPS-PBC)
<safe_ssid>_<bssid-dashed>_<epoch>_handshake.hc22000   ← NEW (was *.hc22000 mixed)
<safe_ssid>_<bssid-dashed>_<epoch>_handshake.pcap      ← NEW (was *.pcap)
<safe_ssid>_<bssid-dashed>_<epoch>_pmkid.hc22000       ← NEW (split-out)
<safe_ssid>_<bssid-dashed>_<epoch>_pmkid.pcap          ← NEW (split-out)
```

**No `.wps` backwards-compatibility.** Lead's explicit call: "I am the only one
that has ever executed wifit3 before. There's nothing backwards to be
compatible with." Delete or hand-rename existing `.wps` files in the working `captures/`.

The `method:` line drops from WPS file *bodies* — the filename suffix carries
the method now. Bodies become:
```
SSID: NETGEAR
BSSID: 31:21:01:01:92:7c
PSK: abcdefg
PIN: 12345670              ← only in _wps_pin.txt
```

## Tree-vs-root logging rule

Auto-save log should be the leaf of a tree if we're inside of a log tree.

Example:

```
WPS PIN brute started on NETGEAR
 ├─► trying 12340006 → first half OK [M5] — sweeping second half
 ├─► trying 12340013 → second half wrong [M6]
 …
 ├─✓  WPS PIN for NETGEAR: 12345670
 ├─►  Password for NETGEAR: "abcdefgh"
 └─► (saved NETGEAR_31-21-01-01-92-7c_…_wps_pin.txt)
```
```
[bold green]✓ HANDSHAKE[/bold green] on NETGEAR from aa:bb:cc:…
 └─► (saved NETGEAR_…_handshake.hc22000)
```

## Where each kind's auto-save trigger lives (5 sites)

1. **Scanner handshake/PMKID arrival** — extend `scanner._log_capture_event`
   (`scanner.py:483`+) to call `self.app.auto_save.save_handshake(…)` /
   `save_pmkid(…)` after the existing `_write_log(…)` line. *Note: today
   Scanner just LOGS arrivals; it doesn't save. After this refactor it does.*
2. **Focus handshake/PMKID arrival** — same hook in Focus's capture-event
   drain (`focus.py:_drain_capture_events` or equivalent). Same calls.
3. **Focus active PMKID harvest** — `_run_pmkid_harvest` worker
   (`focus.py:1089` region) — after the harvest succeeds, call
   `self.app.auto_save.save_pmkid(…)` then log success.
4. **WEP key crack** — Focus's `update_ui` already detects
   `camp.recovered_key is not None` (`focus.py:427`-430) and tears the
   campaign down. Insert `self.app.auto_save.save_wep_key(ap, key)` before
   teardown.
5. **WPS attacks** — already auto-save today, but currently *inside* the tree.
   Move the save to follow the tree-close: in `focus._stop_wps_pin` change the
   Password line to `treelog.leaf(…)` and replace the in-tree `(saved …)` leaf
   with a post-tree `self.app.auto_save.save_wps_pin(ap, pin, psk)`. Same
   surgery in `focus._auto_capture_pbc` for PBC.

## Stuff to rip out (in this exact order in commit 2)

```
focus.py:87       Binding("s", "save_capture", "Save Capture", show=True),
focus.py:88       Binding("s", "save_key", "Save Key", show=True),
focus.py:157      yield Button("Save", variant="success", id="btn-save", disabled=True)
focus.py:615-623  btn_save query + display/visible/disabled flags
focus.py:857      "[dim](press s to save)[/dim]"     ← handshake event log
focus.py:863      "[dim](press s to save)[/dim]"     ← PMKID event log
focus.py:1067-70  on_button_pressed("btn-save") → _save_capture()
focus.py:1091+    check_action save_capture / save_key router
focus.py:1105-11  action_save_capture / action_save_key
focus.py:1215     "[dim]press[/] [cyan bold]s[/] [dim]to[/] [cyan bold]save[/] …"
focus.py:1436     def _save_capture(self) — the if/elif/elif mess
focus.py:1498     def _save_wep_key(self, ap)
```

Also drop the `ap.has_capture` property in `engine/models.py` (it existed only
to gate the Save button's enable state). Confirm no other readers via grep
first.

## `capture_history.py` updates

The loader's regex (`engine/capture_history.py:_NAME_RE`) currently accepts
`pcap | hc22000 | txt | wps`. After this refactor it accepts the
`_<kind>.<ext>` suffixes:

```python
_NAME_RE = re.compile(
    r"^(?P<ssid>.+)_"
    r"(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})_"
    r"(?P<epoch>\d+)"
    r"_(?P<suffix>wepkey|wps_pin|wps_pbc|handshake|pmkid)"
    r"\.(?P<ext>txt|hc22000|pcap)$"
)
```

`_parse_file` dispatches by `suffix` (not `ext`): `wepkey` → WEP, `wps_pin` /
`wps_pbc` → WPS (kind="WPS" stays — UI badges don't differentiate), `handshake`
→ HS, `pmkid` → PMKID. The pcap files still classify as "no PersistedCapture"
(they're companions to the hashline files, same as today).

`_read_wps_psk` continues to parse `PSK:` line; can also pick up `PIN:` if
present and stash in `PersistedCapture.value` only for `_wps_pin.txt` (PBC
files won't have it). Trivial change.

`summarize()` already returns `(hs, pmkid, wep, wps)` — no signature change.
Tests in `tests/engine/test_capture_history.py` need updates for the new
filename patterns.

## Tests

- `tests/engine/test_save.py` — NEW. Each `AutoSave.save_*` method:
  - Writes the right filename + body when no dupe exists.
  - Returns None + writes nothing when a duplicate is on disk.
  - Logs `(saved …)` exactly once.
  - Sanitizes SSIDs with path-traversal characters.
- `tests/engine/test_capture_history.py` — update existing tests to use the
  new `_<kind>.<ext>` filenames; add cases for `_handshake.hc22000` and
  `_pmkid.hc22000` parsing.
- `tests/engine/test_wps_campaign.py` + `test_wps_pbc.py` — update existing
  save tests to reflect that save now happens *outside* the orchestrator
  (mock `AutoSave` or replace `save_pbc_credential` mentions).

## Commit shape (3, in order)

1. **`feat(save): typed AutoSave class + capture_history reads new filenames`**
   Adds `engine/save.py`, updates `_NAME_RE` regex in `capture_history.py`,
   adds tests. No callers changed. Pure additive foundation.
2. **`refactor(ui): rip out Save button, wire auto-save into every site`**
   The big surgery: deletes from `focus.py` per the "stuff to rip out" list,
   instantiates `AutoSave` on `WifiteApp` startup, calls `auto_save.save_*`
   from the 5 trigger sites. Updates capture-event log strings to drop
   "(press s to save)" tails.
3. **`refactor(captures): split handshake/pmkid filenames + drop .wps method body`**
   Renames the producer side: `save_pbc_credential` removed, replaced by the
   `AutoSave.save_wps_pin` / `save_wps_pbc` methods; the existing handshake /
   PMKID writers in `_save_capture` (now moved into AutoSave) split into
   `_handshake.<ext>` / `_pmkid.<ext>`. Updates `gitignore` if needed.

## Open questions left for the next agent (small, ask Lead if unsure)

- **Does the WepCampaign tree need to end before the WEP-key save line?** From
  the audit, the WEP campaign isn't actually in a "tree" the same way WPS
  attacks are — its log lines are interleaved Replay/Frag/Chop status updates,
  not a header→branches→leaf structure. Treat the save-on-recovery as a
  root-level line and you're fine. Verify by reading `focus.py:_update_wep_capture`
  + the relevant CAPTURE panel block.
- **Should the "(saved …)" line itself be `[dim]`?** Lean yes — it's confirmation,
  not the main signal. Lead can re-style after seeing it live.
- **Path resolution for `captures_dir`** — keep as `Path("captures")` (cwd-relative)
  for now to match the existing `engine/pcap.write_pcap` behavior; the
  `User persistence + decloak DB` line in NEXT-STEPS.md will eventually fold
  this into a `platformdirs`-based config-driven path.

## Don't touch in this refactor

- Scanner UI — already correct, no save plumbing exists there.
- `engine/pcap.write_pcap` — the existing PCAP writer is fine; AutoSave reuses
  it via composition, doesn't replicate.
- `engine/hc22000.write_hc22000` — same; AutoSave calls into it for the
  handshake / PMKID hashline writes.
- The `captures/` directory itself (gitignored). Existing `.wps` files in the
  user's tree should be hand-deleted by the user; no migration code.
