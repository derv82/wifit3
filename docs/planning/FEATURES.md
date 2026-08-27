# Wifit3 — Features & QoL Backlog

Known bugs live in `BUGS.md`.

---

### About page / Check-for-updates

If the user has internet connection, it's trivial to query
[the releases page](https://github.com/derv82/wifit3/releases) to fectch the latest version,
compare with the current version, and show a Toast notification about the newest version,
clicking Toast notification -> opens releases page.

We could automate this as well (opt-**in**), in Preferences: `[x] Automatically check for updates`

### VAULT — loot manager ("HACKLEBOX") — DONE (core view; Check/Hashcat/add deferred)

**Problem.** Half of Wifite's UX is effectively the OS file manager: squinting at `captures/` full
of long BSSID-encoded filenames. The loot (handshakes, PMKIDs, cracked PSKs) deserves a real view,
not a directory listing.

**Shipped.** `ui/screens/vault.py:VaultView`, opened from the Scanner (hotkey `v`) -- read-only
otherwise, no radio/interface dependency, works with no card plugged in. One `DataTable` over
`persist.capture_history.load_capture_index()` (already existed, built for the Scanner's own
capture-badge history -- VAULT is genuinely "just a new screen over" it, per the original
complexity note), newest-first, re-scanned from disk on every visit (`on_screen_resume`) so a
save from a running campaign shows up next time you open it. `PersistedCapture` gained an `ssid`
field (parsed from the filename, previously discarded) so the table can show a name, not just a
BSSID. Per-entry: **Remove** (`r`, deletes the file + reloads), **Copy** (`c`, the WEP/WPS
credential itself, or -- since HS/PMKID have no single "value" -- the file's own hashcat hashline
content, via Textual's native OSC-52 clipboard). Bulk: **Export all as Zip** (`z`, always written
*beside* `captures/`, never inside it, so a re-export can never bundle a previous export into
itself) and **Show directory** (`o`, `xdg-open`/`open`/`explorer` per platform). Verified with a
real Textual SVG render (title bar, columns, newest-first ordering, footer keybindings all
correct), not just unit assertions.

**Deferred, not attempted:**
- **"add" (manually enter a credential you already have from elsewhere).** No design decided yet
  for the entry form; low value next to the read/remove/copy path that's actually built.
- **Check button** (re-authenticate against the live AP to confirm a stored PSK still works).
  Needs a real target AP in range to test meaningfully, and VAULT has no "current target"/card
  context to decide which interface would even attempt it -- a class-design question for a
  session with hardware in the loop, not a solo overnight guess.
- **Launch Hashcat** (subprocess launch of an external tool). Spawning and babysitting an external
  process has real UX questions (detached terminal? inline output? which mode per capture type?)
  worth a quick design pass rather than silently picking conventions unasked.

------------

### EAP-MSCHAPv2 / PEAP via Evil Twin

Most enterprise Wi-Fi is PEAP-MSCHAPv2, which cracks with hashcat `-m 5500` (DES half near-
instant via crack.sh): recovering the *domain* credential is a far higher value than a PSK,
PEAP wraps MSCHAPv2 in TLS, so it **can't be captured passively**. Stand up an Evil Twin 
so the client auths to *you*.

Some things we'll need:
- target-ESSID beacons,
- RADIUS/EAP state machine
- cert handling.

When a second hashcat mode lands (`-m 4800`/`5500`), the save layer needs a per-attack
(mode + line-format) map instead of the hardcoded `-m 22000`.

------------

## Deferred / Chopping Block

### WPS improvements - Low priority (who even has a vulnerable WPS router?)

The WPS engine is built, offline-proven, and HW-validated (full PIN crack on AirLink). Gaps:
- **Lock-cycle matrix** — only AirLink soft-lock tested; exercise no-lock, long cooldowns, hard-lock.
- **PixieDust (PRNG seed recovery)** — Phase 1 (Null Secret) and Phase 2 (Static Secrets)
  landed natively in `campaigns/wps/pixie.py`. Advanced PRNG seed-search modes (Broadcom
  timestamp search, Realtek/MediaTek LCG) remain deferred due to the CPU cost of
  evaluating 32-bit seed spaces in pure Python.
