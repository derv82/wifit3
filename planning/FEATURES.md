# Wifit3 — Features & QoL Backlog

Known bugs + QoL nits live in `BUGS.md`.

---

## High Priority

### WPS PIN Progress  — ✓ implemented this session

When we first target an AP, `_log_persisted_history` (`focus_v2/screen.py`) surfaces the on-disk
handshake/PMKID/WEP/WPS counts. Add the in-progress WPS PIN sweep state, read from the
`wps_<bssid>.run` resume file (colons→dashes) — which is NOT in `load_capture_index()`, so it needs
its own JSON read keyed by BSSID.

    |-> WPS PIN sweep: {N/11k during first-half | N/1k once first-half is locked}

The `N/11k | N/1k` formatter already exists (`focus_model.wps_status_markup`, plus the campaign's
`_load_state` resumed message) — factor a pure `progress_from_state(dict)->str` and call it here.
Show it only for *in-progress* runs: a cracked run already surfaces as a saved WPS PSK row, and a
failed/exhausted run should say so, not show a frozen bar.

(The `12345678` checksum question turned into a real defect — see **WPS PIN reliability** below. The
checksum-invalid common PINs stay; the fix is that the sweep must stop looping on them.)

### Improve 802.11 Logs — consistency pass  — ✓ implemented this session

The gripe is *inconsistency*, not volume. `[NEW AP]` (interface.py:233, free-form, INFO) breaks the
column layout that `[RXFRAME]` (DEBUG) otherwise holds, so next to the structured frame traces it
reads as noise. The RX filter is already sane — data/eapol/wep_data/assoc_resp/deauth/mgmt_*, no
beacons/probes/control (interface.py:160) — so the fix is formatting, not filtering.

- **Unify every frame line under one `[RXFRAME]` schema.** Fold the new-AP event into it:
  `[RXFRAME] beacon   New AP on Ch {ch}: {bssid} ("{ssid}")`. One padded `{type}` column, then a
  per-type detail; `[TXFRAME]` mirrors the same column so injects align with RX.
- **Keep `data` frames** — reframe them as the monitor-mode-health signal they are (seeing data to
  MACs that aren't ours = promiscuous capture working), rather than dropping them as "not relevant."
- **`[TXFRAME]` stays DEBUG-guarded**, not INFO — a deauth burst / WPS sweep would flood it.
- **Per-driver TX hex dump** (`rt2800usb/tx.py:188`, `rt3070`/`rt5370`/`rt5372`/`rt5572`, `mt76x2u`…) —
  inconsistent prefixes, all DEBUG; normalize the format string (per-driver sweep, anti-DRY).

**Done this session:** `[NEW AP]` folded into a `[RXFRAME] beacon` line; `[RXFRAME]`/`[TXFRAME]`
share one padded-type column via `_fmt_frame` (to_ds/from_ds dropped as noise; data frames kept);
the five Ralink drivers' bulk-OUT hex dumps normalized to one format. The `wlan0`-in-logs bug is
fixed — `interface.py` logs the driver's `_chipset`, not the synthetic `self.name`. `self.name =
wlan{N}` stays as the unique interface handle (splash/manager selection key, disambiguates two
identical cards); the full rename waits for **Multi-card support**.

### WPS PIN reliability — retriable vs. "this PIN is wrong"

The campaign *correctly* retries retriable failures forever — this is intentional and regression-
tested (`test_wps_campaign.py:186`: a `PROTO_ERROR` must NOT advance the keyspace, else a locked /
rate-limited / distant AP makes us skip good PINs and the whole brute-force is worthless). The real
gap is narrower: we never *read* the AP's stated reason, so we can't distinguish a retriable refusal
from a definitive "PIN rejected." The **only** signals that may ever advance a PIN are
`FIRST/SECOND_HALF_WRONG` and an actual AP PIN/checksum-rejected error.

**Design (agreed):** advance ONLY on `*_HALF_WRONG` or a confirmed "PIN rejected" config-error;
everything else retries with **infinite patience** — no skip-after-N caps, no give-up bails (a far /
locked / silent AP is still a valid target, just slow). Terminology: a `WSC_NACK` is the AP
*answering* with a config-error code; a timeout is **"AP didn't respond"** — not a NACK.

**Done (this session):** de-swallow `ATTR_CONFIG_ERROR` — the registrar parses it, logs it by name,
and carries it (+ `reached_m1`) on `AttemptOutcome`; the per-attempt log shows the reason; the silent
case now reads "AP didn't respond." **Pending hardware:** capture which config-error code the APs
that choke on a checksum-invalid PIN (e.g. `12345678`) actually send, then map *that specific code* →
advance. Until it's confirmed, nothing new auto-advances (play-it-safe). Note: the checksum-VALID
sweep never emits a bad-checksum PIN, so a "checksum rejected" error can only occur for the COMMON
literals — advancing there just moves to the next common PIN.


-----

## Low Priority

### OUI-Specific WPS PIN Selection — ✓ implemented this session

A target's OUI (first 6 hex of the BSSID) → known factory PINs, seeded ahead of the generic COMMON
list + 11k sweep (`known_pins.py` + `known_pins.json`, 534 OUIs / 1807 PINs; `campaign.py` seeds
`_common_pins`, logs an "OUI match: N known default PIN(s)" line). Slots into the existing
`common`-phase machine — each is a full 8-digit PIN, `first_half_ok` still short-circuits to the
second-half sweep, and the dead-first-half skip dedups shared prefixes among the seeded PINs.

**Licensing (resolved):** airgeddon is **GPLv3**, wifite3 **GPLv2** — so we did NOT copy its
`known_pins.db`. The OUI↔PIN pairs are *facts* (a family ships/computes a given PIN), uncopyrightable
under Feist; `known_pins.json` is our own re-expression of those facts, credited to airgeddon's
compilation in `known_pins.py`. Data-only reuse with attribution — the maintainers' call, documented.
Future: an OUI→vendor table (companion to client-fingerprinting) could name the matched vendor.

### Multi-card support (Minnie Drivers v2)

Run 2+ USB cards in one session — pool RX, split TX. Possible because drivers are generic
(`WlanDriver`, no global state); the work is making the layer *above* them multi-instance.
Capabilities: pooled RX (~2× beacons/EAPOL, union AP list), hot-plug add/remove mid-session,
split the channel set across cards, dedicate one card to TX so a deauth can't deafen our own
RX, one-card-per-target. **Complexity: big refactor** — `WlanInterface` goes per-card and a
new `CardPool` orchestrator owns the fleet, merged model, channel arbitration, and TX routing.
Enumeration is mostly there (`WlanDeviceManager`); everything downstream of "I have N
interfaces" is singular today.

### Test & Fix macOS support

Figure out how to detect & access drivers from userland in OSX.

The viable path is a **codeless kext** (Info.plist only, no code) per supported card.
Each plist declares the adapter's VID:PID with a high `IOProbeScore` so the kernel
binds the do-nothing kext and leaves the USB interface unclaimed for libusb. 
Unverified — no macOS hardware tested. Parked until someone wants it.

### Config persistence — pre-alpha

**Problem.** Nothing persists — theme (hardcoded `textual-dark`), WPS PBC auto-invade, paths,
and Scanner sort all reset every launch.

**Approach.** A TOML file via `platformdirs` (`tomllib` is stdlib 3.11+) — `~/.config/wifit3/`
on Linux, `%APPDATA%` on Windows, `~/Library/Application Support` on macOS. Sticky: theme,
Scanner sort, PBC auto-invade, capture dir, channel-filter defaults. **Complexity: low.**

### Client fingerprinting — if time allows

**Problem.** Clients show bare MACs; a device class (phone / laptop / PS5 / IoT) speeds target
selection — IoT (Ring/Nest/Roku/FireTV) is highest-value for scoping.

**Approach.** Emoji left of the BSSID, one `fingerprint.py`, no DB: ~50 hardcoded OUI prefixes
+ IE fingerprinting for ambiguous OUIs (Murata/Intel modules); returns `(emoji, class,
confidence)`, blank if low; full breakdown in the Focus detail panel.

**Complexity.** Moderate — display is the hard part, not the resolver. (Killed a full
OUI→vendor DB in the Scanner table: cells too cramped for vendor strings, and an OUI names the
Wi-Fi *module* maker, not the device — disambiguation needs IE fingerprinting anyway.)

### Deauth effectiveness feedback (sent / ACKed) — if time allows

**Problem.** Deauth is fire-and-forget — we show frames *sent*, not *landed*. A unicast deauth
is hardware-ACKed, so "N sent → M ACKed" is a real reachability readout in Focus.

**Approach** (no live-TX needed to design):
- **Sniff ACKs** — RX already sees control frames; an ACK is FC=0xD4 with RA = our spoofed src,
  correlate by timing against each deauth sent. Driver-agnostic.
- **HW retry tally (rtl8187 only)** — `0xFFFA` cumulative retry count rises when un-ACKed;
  coarser, and L has no TX-status URB, so ACK-sniff is the portable route.

**Related (tiny):** `build_deauth` writes `duration = 0`; a correct injector sets the
unicast-ACK NAV (`SIFS + ACK@rate`, e.g. `0x013a` @ 1 Mbps) — one line, do it here.

**Complexity.** Low-moderate; the ACK-sniff correlator is the real work.

### Per-AP persistent log — if time allows

**Problem.** The Focus log is per-session and per-target — switch APs (or bounce to Scanner)
and the previous AP's attack log is gone. Re-entering a target you already worked shows a blank
log, even though its handshake / WPS / WEP history is exactly what you'd want back.

**Model.** One new field on `AccessPoint` (`engine/models.py`, a plain `@dataclass`): a capped
ring buffer `log_history: deque[str] = field(default_factory=lambda: deque(maxlen=200))`. It
stores the *composed* line — timestamp prefix included — so replay shows the original event
times, not the revisit time. It lives on the AP object, so its lifetime is that AP's lifetime
in the session registry: in-memory only, gone on app exit, never touches disk.

**Capture.** `FocusViewV2._log` (`screen.py:543`) is the single chokepoint — every line flows
through it. Build the line once (`[dim]{ts}[/dim]  {markup}`), write it to the `LogBand`, and,
when a target is bound (`self._target_ap`), also append it to `self._target_ap.log_history`.
Lines emitted with no target (the demo seed) are simply not stored.

**Replay.** `_enter_target` (`screen.py:292`) today does `LogBand.clear()` then re-seeds via
`_log`. Change to: clear, then if `ap.log_history` is non-empty replay it straight into the band
(`LogBand.write` — *not* `_log`, so replay doesn't re-capture itself) and skip the fresh seed
block; otherwise seed as now (first visit). A `─ resumed ─` divider before the live stream keeps
the boundary readable. The seed lines (Target acquired / BSSID / encryption / tuned-to-channel)
are themselves `_log` calls and thus already in `log_history`, so re-seeding on revisit would
duplicate them — that first-visit-vs-revisit branch is the one real piece of logic here.

**Cap & tradeoff.** `maxlen` makes append O(1) and growth bounded: ~200 lines × ~60 B × N APs ≈
a few hundred KB even for a crowded scan — negligible. The cost is that a marathon WPS PIN sweep
(thousands of lines) keeps only its last ~200 per AP; the live band still shows everything in
the moment, only the *replay* is truncated to the tail. The cap is a single constant to tune if
a use case wants deeper scrollback.

**Out of scope.** ScannerView keeps its own session log (`_write_log`); this is the Focus
per-target log only. Mirroring it for the scanner would be a separate, optional follow-up.

**Complexity.** Low — one dataclass field, one append in `_log`, one replay branch in
`_enter_target`. No threading, no disk, no new widgets.

### WinUSB-install mascot — "WiFFy" — post-alpha, Windows-only delight

The WinUSB install (`wdi-simple`) is 1–3+ min of unspeedable dead air (a Windows driver
install, not our code). Fill it: **WiFFy**, a googly-eyed take on the logo's Wi-Fi bars
(original, Clippy-in-spirit), floats into the install screen, rotates slow-typed one-liners,
waves off on success, bolts off-screen on failure. Tone: authorized-tool parody (CTF / "your
own AP" humor), ~30–50 lines, install screen only. Windows-only — Linux's `pkexec` install
returns in a second, no void to fill. **Complexity: low**, pure presentation: the install
already runs off-thread and the REQUIRED-badge pulse timer machinery is reusable. 📎

### Triangulation map — post-1.0

Three cards + RSSI trilateration + a drag-to-place UI. Fun, novel, not soon. 😄

------------

## Chopping Block / Graveyard

### WPS improvements - Low priority (who even has a vulnerable WPS router?)

The WPS engine is built, offline-proven, and HW-validated (full PIN crack on AirLink). Gaps:
- **Lock-cycle matrix** — only AirLink soft-lock tested; exercise no-lock, long cooldowns, hard-lock.
- **Terminal hard-lock escape** — `lock.py` learns a measured backoff but loops forever on a
  perma-locked AP; bail after N zero-progress cycles and tell the user.
- **Focus WPS panel** (passive-by-default, behind a button).
- **PixieWPS** — designed in `engine/attacks/wps/README.md` (native, all 5 modes, no binary).
  Deferred on effort + one real dep call: **numpy**, wanted to keep the Realtek RTL819x/eCos
  2³¹–2³² seed sweep interactive (Ralink/MediaTek instant). The old glibc-dep worry is a
  non-issue (`random()` is ~30 reimplementable lines). Tractable, not a wall.

---

## Rogue AP Graveyard

**Problems.**
1. EvilTwin/RogueAP requires responses within microsecond for ACKs.
  - We cannot achieve this from software <-> USB (multi-millisecond latency).
  - Hard-MACs that auto-ACK *could* be considered. We don't want card-specific solutions!
2. No native AP/STA support on most cards.
  - We skipped most/all of the STA/AP modes from the wireless drivers we ported.
  - Monitor + Inject was the goal.
  - Rewriting all drivers to support STA/AP = Significant effort.

### EAP-MSCHAPv2 / PEAP via Rogue AP / Evil Twin — "active", big build

Blocked by: ***Tag + suppress EAP/Enterprise handshakes***

Most enterprise Wi-Fi is PEAP-MSCHAPv2, which cracks with hashcat `-m 5500` (DES half near-
instant via crack.sh) — recovering the *domain* credential, far higher value than a PSK. The
marquee enterprise capability. PEAP wraps MSCHAPv2 in TLS, so it **can't be captured
passively** — stand up a rogue AP / evil twin so the client auths to *you*. Active, TX-heavy,
AP-impersonating → behind the explicit-action gate; large build (target-ESSID beacon, RADIUS/
EAP state machine, cert handling). Our `engine.campaign` format could compose it cleanly —
worth a design pass, and an area to beat Wifite2 (no native enterprise).

When a second hashcat mode lands (`-m 4800`/`5500`), the save layer needs a per-attack
(mode + line-format) map instead of the hardcoded `-m 22000`.

## WPA3 downgrade upgrade: EvilTwin

The Focus **WPA Downgrade** button reads as dead because the implemented path is weak. Both
paths win the same prize — the client's **EAPOL M1+M2** for a *WPA2* assoc (M2's MIC is all an
offline PSK crack needs) — and both work **only on WPA3-transition** APs; Transition-Disable
kills them.

- **Path 1 — passive (implemented, weak).** Forge WPA2-only beacons/probe-resps so a client
  downgrades and 4-ways with the *real* AP, sniffed passively. But the real AP still advertises
  SAE on-channel, so a sane client picks SAE → nothing to capture. (Never confirmed to inject on
  HW.) `engine/attacks/wpa3_downgrade.py`.
- **Path 2 — evil twin (the reliable build).** Rogue AP (same SSID/BSSID, ideally a different
  channel), WPA2-only; accept auth+assoc, **send M1 yourself** (random ANonce), capture M2.
  Deterministic. A minimal AP responder in the inject path (beacon/probe/auth/assoc/M1) — *not*
  a hostapd shell-out (Linux-only, breaks cross-platform). Feature-scale.

**Near-term QoL:** disable/annotate the button unless the target is WPA3-transition, and log
"passive — waiting for a natural reconnect (minutes–hours)" so it stops looking broken.
