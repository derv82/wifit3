# Wifit3 — Features & QoL Backlog

Known bugs + QoL nits live in `BUGS.md`.

---

## High Priority

### CI, Packaging, and Deployment Pipeline

**Problem.**
- We have absolutely zero CI right now.
  - All users get "whatever version was in main" when they cloned.
- We have no plan for how releases will be structured & rolled out.

**GitHub Actions.**
- `ci.yml` — on every PR: imports, unit tests, smoke test.
- `release.yml` — on git tag: matrix build Windows + Linux, PyInstaller on the
  Windows runner, create a GitHub Release with artifacts.
- `smoke.yml` — reusable: headless Textual launch, clean exit, catches bundling
  issues.

**Bundle Testing.**
PyInstaller bundles the interpreter + deps + firmware assets + `wdi-simple.exe`.
Textual + PyInstaller has known quirks — test carefully on the Windows runner.

**Complexity.** High. Learning Actions/Workflows, avoiding paid tiers, release process.

### Test & Fix macOS support

Figure out how to detect & access drivers from userland in OSX.

The viable path is a **codeless kext** (Info.plist only, no code) per supported card.
Each plist declares the adapter's VID:PID with a high `IOProbeScore` so the kernel
binds the do-nothing kext and leaves the USB interface unclaimed for libusb. 
Unverified — no macOS hardware tested. Parked until someone wants it.

### Config persistence — pre-alpha

**Problem.** No stored config today — theme resets every launch (hardcoded
`textual-dark` in `ui/app.py`), WPS PBC auto-invade resets, paths reset, Scanner
sort resets.

**Approach.** A TOML file via `platformdirs` (`tomllib` is stdlib on 3.11+):
- Linux: `~/.config/wifit3/wifit3.toml`
- Windows: `%APPDATA%/wifit3/wifit3.toml`
- macOS: `~/Library/Application Support/wifit3/wifit3.toml`

Sticky settings: theme, Scanner sort column/direction, WPS PBC auto-invade,
capture output dir, channel filter defaults, (YAGNI: `hashcat` path, auto-update toggle).

**Complexity.** Low-moderate — one storage layer, human-editable TOML.

### Client fingerprinting — if time allows

**Problem.** The clients list shows bare MACs; a device class (phone / laptop /
TV / PS5 / IoT) would make target selection far faster. IoT devices
(Ring/Blink/Nest/Roku/FireTV) are highest-value for engagement scoping.

**Approach.** Emoji client identification, one character left of the BSSID. All in
one `fingerprint.py` module, **no SQLite/JSON lookups**:
- Top ~50 OUI prefixes hardcoded (Apple, Samsung, Google, Amazon/Ring/FireTV,
  Roku, Nest, Microsoft, Sony, Nintendo).
- IE fingerprinting for ambiguous OUIs (Murata/Intel modules used across
  devices).
- Returns `(emoji, device_class, confidence)` — blank if confidence too low.
- Focus detail panel shows the full breakdown.

**Complexity.** Moderate. Display is the hard part, not the resolver — see the
shelved-OUI-DB lesson below.

> **Why not a full OUI→vendor DB in the Scanner table?** Designed end-to-end
> (IEEE MA-L/M/S registries, longest-prefix match) and killed on UX: the AP/Client
> tables are horizontally cramped — vendor strings don't fit in a cell at all
> ("Sony Interactive Entertainment"); Textual has no icons; and the OUI usually
> identifies the Wi-Fi *module* maker (Intel/Murata/AzureWave/Foxconn), not the
> device brand. True device-type disambiguation needs IE fingerprinting anyway.
> The realistic home is a **Focus-screen detail panel**, opt-in — which is exactly
> the fingerprint approach above. The build/resolver design is sound and can be
> lifted wholesale if revived.

### Deauth effectiveness feedback (sent / ACKed) — if time allows

**Problem.** A deauth burst is fire-and-forget: we show frames *sent*, but not
whether they *landed*. A unicast deauth (addr1 = a specific client) is link-layer
ACKed by the recipient's hardware, so "N sent → M ACKed" is a real reachability
signal — and a nice live confidence readout in Focus ("client is hearing us").

**Approach.** Two sources, neither needing live-TX from the agent to design:
- **Sniff the ACKs** — our own RX path already sees control frames (RX_CONF now
  carries `RX_CONF_CTRL` since the rtl8187 monitor-filter fix). An ACK is a 14-byte
  control frame (FC=0xD4) whose RA = our spoofed source MAC; correlate by timing +
  RA against each deauth we just sent. Driver-agnostic, works fleet-wide.
- **Hardware tally (rtl8187-only)** — `rtl8187_work` reads the cumulative retry
  count at reg `0xFFFA`; a rising tally means the chip is retransmitting (no ACK).
  Coarser than sniffing, and L-path has no TX-status URB, so the ACK-sniff route is
  the portable one.
  Tie the count into the deauth UI; pairs naturally with the unicast-NAV fix below.

**Related (tiny):** our `build_deauth` writes `duration = 0`; aireplay sets the
unicast-ACK NAV (`SIFS + ACK@rate`, e.g. `0x013a` @ 1 Mbps). Harmless today (the
deauth still lands), but a faithful injector should set it — do it alongside this.

**Complexity.** Low-moderate. The ACK-sniff correlator is the real work; the NAV
fix is one line in `build_deauth`.

### Multi-card support (Minnie Drivers v2)

**Problem / opportunity.** Run 2+ USB cards concurrently in one session, pooling
their RX and splitting their TX. It's *possible* because the drivers were built
generic from day one (`WlanDriver` Protocol, no global state) — the substrate is
there; what's missing is making the layer ABOVE the driver multi-instance-safe.

**The vision:**
- **Pooled RX** — two cards → ~2× the beacons/EAPOL/IVs; handshake capture lands
  on whichever card hears the client. Scanner shows the *union* of APs.
- **Hot-plug** — plug a second (even shitty) card *while running* and watch its
  APs merge into the live list; unplug → its contribution drains, session
  continues.
- **Coordinated channel strategy** — split the channel set across cards (A does
  1–6, B does 7–13) so each dwells longer; or pin one card to a target while
  another scans.
- **TX/RX split** — dedicate one card to injection (deauth/replay) and another to
  pure RX, so we never miss the handshake our own deauth provoked (the
  half-duplex radio can't TX and RX the same instant).
- **Multi-target** — one card per target, attacking several APs at once.

**Complexity.** Big refactor. Nearly everything stateful today implicitly assumes
*one* card — channel hopping, the AP/Client registry, per-attack campaigns, the
RX reader thread, the UI's "the interface." `WlanInterface` becomes
one-per-card; a new `CardPool`/orchestrator owns the fleet + the merged model the
UI renders, arbitrates channel plans, routes TX, and handles dynamic add/remove
(USB enumeration watcher). The `WlanDeviceManager` already does generic VID:PID
discovery, so multi-device *enumeration* is mostly there; the work is everything
downstream of "I have N interfaces" being singular today. The demo writes itself.

### WinUSB-install mascot — "WiFFy" — post-alpha, Windows-only delight

**Problem.** The WinUSB install (`wdi-simple` swapping the card's PnP driver) takes **1–3+
minutes** — measured north of three on fast hardware — and there is *nothing* we can do to
speed it up: it's a Windows driver install, not our code. During it the splash just reads
"installing… this can take a minute" while the user stares at a seemingly-frozen screen,
partway through a privileged operation they were already anxious enough to consent to. We
can't make it faster; we can make it not *feel* like three minutes of dead air. This is a
real UX hole, not just camp — there is genuinely no other lever for those minutes.

**Approach.** **WiFFy** — the logo's own upside-down-triangle Wi-Fi bars with two googly eyes
slapped on the green, an *original* character (Clippy in spirit, not in copyright — no
Microsoft lawyers). It peels off the logo and floats down into the install screen when the
bind starts, then chatters: one random
intro line, then a loop of random one-liners on a timer (`random(intro)` → `loop:
random(lines)`), slow-typed, until the install resolves — on success it waves off as the
scanner takes over; on failure it **bolts off-screen the instant the error modal appears**,
abandoning the user without a word (peak Clippy — he does not do consequences). It's *cheap*
to build
because the install already runs off-thread (`asyncio.to_thread`), so the event loop is wide
open: the same Textual timer machinery that drives the REQUIRED-badge pulse drives the
float-down, the type-on text, and the line rotation with zero contention. (The "bright letter
flowing through a word" shimmer — a highlight index walked through the string per tick — is
the natural speech-text flourish if we want it.)

**Tone — keep it on-brand for an *authorized* tool.** Self-aware Clippy parody: lean into
CTF / "your own AP" / engagement-scoping humor, dumb references and shoutouts — *not* literal
how-to-trespass copy (this is an authorized-auditing tool; the documented lines stay parody,
not instruction). e.g. *"It looks like you're auditing your own network — want a hand?"* /
*"Reticulating splines…"* / *"WinUSB: because Microsoft said so."* A corpus of ~30–50 lines;
the comedy is in the rotation. Scope it to the install screen — not an app-wide gremlin.

**Windows-only by nature — and that's the joke.** Linux's "install" is a one-line
`pkexec`/`sudo` prompt that returns in a second; there's no void to fill, so the mascot
simply never appears there. It exists *only* where the platform inflicts the wait.

**Complexity.** Low–moderate, pure presentation — no new subsystem, no driver touch, lives
entirely behind the existing install worker. The proven pieces are already here (timer
animation from the pulse; off-thread install keeping the UI live); the work is the ANSI
mascot frames + float-down keyframes, the type-on effect, and writing the lines. 📎

### Triangulation map — post-1.0

Three cards + RSSI trilateration + a drag-to-place UI. Fun, novel, not soon. 😄

------------

## Chopping Block / Graveyard

### WPS improvements - Low priority (who even has a vulnerable WPS router?)

The WPS attack engine is built, offline-proven, and HW-validated (full PIN crack
on the AirLink router). Remaining gaps:

- **Multi-router lock-cycle matrix.** Only the AirLink soft-lock is exercised.
  Test other behaviours: no-lock, longer cooldowns, and a hard-lock AP that never
  reopens.
- **Terminal hard-lock escape hatch.** `lock.py` already reads the out-of-band
  beacon `wps_locked` IE, splits hard vs soft locks, and learns a *measured*
  backoff biased to the AP's real observed lock duration. The gap: a permanently
  locked AP still loops lock→wait→retry forever. Add "locked across N
  learned-backoff cycles with zero progress → stop and tell the user it's perma-locked.
- **Focus WPS panel** (passive-by-default behind a button).
- **PixieWPS** — already designed in detail in `engine/attacks/wps/README.md`
  (native, all 5 modes, no external `pixiewps` binary). Deferred on
  **effort/priority + one real dependency call: numpy.** The *glibc* half of the
  old "numpy/glibc dependency" worry is a misconception — glibc `random()` is a
  published ~30-line algorithm you reimplement (`r[i] = r[i-3] + r[i-31]`, output
  `>>1`); pixiewps ships its own C version, so there's no OS/ctypes/platform tie.
  The *numpy* half is real: most vulnerable APs are instant (Ralink/MediaTek
  `E-S1 = E-S2 = 0`), but the Realtek RTL819x time-seed + eCos modes sweep a
  2³¹–2³² seed space, which wants the `glibc_fast_seed` 1-word pre-filter + numpy
  vectorization to stay interactive (a worker process parallelizes it further,
  like the WEP PTW cracker). Tractable, not a runtime wall — the cost is
  implementation correctness + deciding to take numpy as a dep.

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

**Why.** Most enterprise Wi-Fi is PEAP-MSCHAPv2. The MSCHAPv2 challenge/response
cracks with hashcat `-m 5500` (and the DES half is near-instant via crack.sh) —
recovering the *domain* credential, far higher value than a Wi-Fi PSK. The marquee
enterprise capability.

**Why it's different.** PEAP wraps MSCHAPv2 in TLS, so unlike EAP-MD5 you **cannot
capture it passively** — you stand up a **rogue AP / evil twin** (hostapd-wpe /
eaphammer-class) so the client authenticates to *you*, exposing the MSCHAPv2
exchange (or accepting your cert). Active, TX-heavy, AP-impersonating → strictly
behind the explicit-action gate, and a large build (beacon as the target ESSID, a
RADIUS/EAP state machine, cert handling).

**The opportunity.** Our `engine.campaign` composition format could orchestrate the
EvilTwin/RogueAP scenario as a first-class, *correct* capability — an area to do
better than Wifite2 (which doesn't do native enterprise). Worth a design pass on
whether the campaign primitives compose it cleanly before committing.

> **Cross-cutting design note — multi-mode hash output.** Everything we emit today is
> hashcat `-m 22000`. The moment we add EAP-MD5 (`-m 4800`) or MSCHAPv2 (`-m 5500`),
> the save/hc layer needs a per-attack **(hashcat mode + line format)** mapping
> instead of the hardcoded 22000. Small, but design it once when the second target
> lands. One solution: Write scripts to hold the mode/format: Windows=Batch Linux/OSX=Bash.

## WPA3 downgrade upgrade: EvilTwin

The Focus **WPA Downgrade** button reads as dead because the current approach
genuinely is weak. Both viable approaches end at the same prize — the client's
**EAPOL M1 + M2** for a *WPA2* association (M2's MIC is all an offline PSK crack
needs; M3/M4 are gravy, and you can't forge M3 without the PSK anyway) — and both
work **only on WPA3-*transition*** APs (a pure-SAE client refuses WPA2). If the AP
sets **Transition-Disable**, both die.

**Path 1 — passive downgrade (what's implemented).** Forge WPA2-only beacons /
probe responses for the target's BSSID and let a client downgrade and run its WPA2
4-way *with the real transition AP*, sniffing it passively. Cheap — no AP to run —
but at the client's mercy: the real AP is advertising SAE on the same channel the
whole time, so a sane client just picks SAE and there's nothing to capture. (Also
never confirmed to actually inject on hardware — only docstring intent.)
`engine/attacks/wpa3_downgrade.py`.

**Path 2 — evil twin / rogue AP (the reliable build).** Isolate the client onto a
rogue AP — same SSID (+ BSSID, to impersonate), ideally a *different* channel so it
isn't fighting the real SAE beacon — advertising WPA2-only; accept the client's
auth + assoc, **send EAPOL M1 yourself** (a random ANonce, no secret), capture M2.
Deterministic: WPA2 is the only option offered. Can't finish (no PSK for M3),
doesn't need to. The RSNE-confirmation check at M3 may make the client abort the
connection — fine, M1+M2 are already in hand. In wifit3 this is a **minimal AP
responder in the inject path** (beacon + probe-resp + auth + assoc + M1 → catch
M2), *not* a shell-out to hostapd like Wifite2 (hostapd is Linux-only — it would
kill the Windows/cross-platform model). Feature-scale, not a tweak.

**Near-term QoL** on the current button regardless: disable/annotate it unless the
target is WPA3-transition, and log "passive — waiting for a natural reconnect
(minutes–hours)" on start so it stops looking broken.
