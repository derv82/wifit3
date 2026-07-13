# Wifit3 — Features & QoL Backlog

Known bugs + QoL nits live in `BUGS.md`.

---

## Low Priority

### Multi-card support (Minnie Drivers v2)

Run 2+ USB cards in one session — pool RX, split TX. Possible because drivers are generic
(`WlanDriver`, no global state); the work is making the layer *above* them multi-instance.
Capabilities: pooled RX (~2× beacons/EAPOL, union AP list), hot-plug add/remove mid-session,
split the channel set across cards, dedicate one card to TX so a deauth can't deafen our own
RX, one-card-per-target. **Complexity: big refactor** — `WlanInterface` goes per-card and a
new `CardPool` orchestrator owns the fleet, merged model, channel arbitration, and TX routing.
Enumeration is mostly there (`WlanDeviceManager`); everything downstream of "I have N
interfaces" is singular today.

→ Full design brain-dump (seams, dedup design, milestones — needs ironing): `planning/MULTICARD.md`

### Test & Fix macOS support

Figure out how to detect & access drivers from userland in OSX.

The viable path is a **codeless kext** (Info.plist only, no code) per supported card.
Each plist declares the adapter's VID:PID with a high `IOProbeScore` so the kernel
binds the do-nothing kext and leaves the USB interface unclaimed for libusb. 
Unverified — no macOS hardware tested. Parked until someone wants it.

### Client fingerprinting — if time allows

**Problem.** Clients show bare MACs; a device class (phone / laptop / PS5 / IoT) speeds target
selection — IoT (Ring/Nest/Roku/FireTV) is highest-value for scoping.

**Approach.** Emoji left of the BSSID, one `fingerprint.py`, no DB: ~50 hardcoded OUI prefixes
+ IE fingerprinting for ambiguous OUIs (Murata/Intel modules); returns `(emoji, class,
confidence)`, blank if low; full breakdown in the Focus detail panel.

**Complexity.** Moderate — display is the hard part, not the resolver. (Killed a full
OUI→vendor DB in the Scanner table: cells too cramped for vendor strings, and an OUI names the
Wi-Fi *module* maker, not the device — disambiguation needs IE fingerprinting anyway.)

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

### Belkin WPS PINs from the M1 serial — deferred

Derive Belkin (Arcadyan) default PINs from the serial in the WPS M1 message — deferred because PIN candidates are generated at campaign start from the BSSID, before M1 is available.

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
