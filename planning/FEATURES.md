# Wifit3 — Features & QoL Backlog

Forward-looking. Product/UX features we plan to build, and the small
bugs/quality-of-life fixes. Each entry: the problem it solves, the approach, and
a rough complexity/notes read. Driver/hardware work lives in `PORTING.md`;
release logistics in `RELEASE-PLAN.md`; current state in `../VERIFICATION.md`.

Ordering within each section is rough priority: pre-alpha → soon → post-Defcon.

---

## Not-Yet-Supported Attacks (Poorly Supported?)

> Common thread: an AKM whose PMK is passphrase-derived is *not* enough — hashcat
> `-m 22000` must also be able to crack the *specific artifact*. Two AKMs slip past
> our crackability gate (`engine.wpa.handshake`) today and get captured, written,
> and reported as "crackable" when stock hashcat cannot crack them (FT-PSK, and
> the SHA256 PMKID). Those are near-term **correctness bugs**; the EAP families
> below are new-attack territory.

### FT-PSK / FT-* (802.11r Fast Transition) — correctness bug, fix wanted

**What's wrong.** `_CRACKABLE_AKMS` includes FT-PSK (AKM 4) and FT-PSK-SHA384
(AKM 19), so we capture, write a `WPA*02`/`WPA*01` line, and tell the user
"crackable." But **hashcat `-m 22000` has no hash mode for FT at all** — 802.11r
derives the PTK through PMK-R0 → PMK-R1 and hashcat only does the direct
`PRF(PMK, …)`. It's an open feature request (hashcat issue #3887). Those lines
never crack: we lie to the user.

**Why it's not auto-caught.** The gate's master switch is "does the AP offer
SAE?"; on a non-SAE WPA2+FT network it short-circuits to "crackable" and never
inspects the AKM. Fixing FT means letting an AKM check run even when SAE isn't
offered (a scoped widening of the gate). Our `akm_client` (from M2 / (Re)Assoc)
already identifies which associations chose AKM 4/19, so detection is cheap — the
*policy* is the open question:

1. **Suppress like SAE** *(recommended — minimal, consistent)* — withhold + badge
   "FT — not hashcat-crackable," reusing the SAE machinery (gate → no pairs → no
   banner/save). Smallest change, fully closes the gap. Discards the capture, but
   FT-aware cracking isn't in our pipeline anyway.
2. **Emit but flag** — keep writing the hc22000/pcap but surface "⚠ FT-PSK — needs
   FT-aware tooling, not `-m 22000`" (mirrors hcxpcapngtool's flag + `--ignore-ie`).
   Faithful to the reference tool; most UI/format work; still writes a line that
   `-m 22000` chokes on.
3. **Pcap-only** *(future enhancement)* — keep the pcap so an FT-aware cracker (or
   hashcat once #3887 lands) can re-extract, but drop the `WPA*02`/`WPA*01` line and
   the "crackable" claim. Honest and preserves the data (FT-PSK's PMK *is*
   passphrase-derived, unlike SAE's), but needs new plumbing: today a missing
   crackable hashline makes `save_handshake` write nothing, pcap included.

**Out of scope:** FT *roaming* handshakes ride Reassoc/Auth frames (MDE/FTE KDEs),
not a 4-way EAPOL — a separate capture path we don't have, uncrackable by hashcat
anyway.

### SHA256 PMKIDs (PSK-SHA256, AKM 6) — same class of bug, PMKID-only

The plain-PSK (AKM 2) PMKID is `HMAC-SHA1(PMK, "PMK Name" ‖ AA ‖ SPA)` — what
hashcat `-m 22000` cracks. A **PSK-SHA256 (AKM 6) PMKID is HMAC-SHA256** (the
PMKID hash follows the AKM's hash), and hashcat's PMKID path is SHA1-only ("no AKM
suite detection or SHA-256 variant handling"). hcxpcapngtool agrees: it flags
these "possible PSKSHA256" and needs `--ignore-ie` to force-emit past its AKM
filter.

**What's wrong.** Our gate treats AKM 6 as crackable, so we'd write a `WPA*01` for
an AKM-6 PMKID hashcat can't crack. The AKM-6 *handshake* (`WPA*02`) is fine — the
keyver in the embedded EAPOL tells hashcat to use AES-CMAC. So the fix is
**PMKID-only**: exclude SHA256/FT AKMs from `pmkid_hashline`. Rare in the wild,
but a free correctness win riding the FT fix.

### EAP-MD5 (WPA/2-Enterprise) — low priority, near-extinct

EAP-MD5 puts its challenge/response in the clear (not TLS-tunneled), so it's
passively capturable — but it's essentially extinct (deprecated, offers no key
material protection). Reference extractor: `data_dumps/eapmd5hcgen.py` (pulls
Identity / Challenge / Response from a pcap). That 2014 script uses the `-m 10` +
`^<id>` rule trick (prepend the EAP identifier byte so `md5($pass.$salt)` =
`MD5(id ‖ pass ‖ challenge)`); the cleaner modern fit is **hashcat `-m 4800`**
(iSCSI CHAP — same `MD5(id ‖ pass ‖ challenge)` construction, format
`response:challenge:id`, no rule). Parked unless a real target appears.

### EAP-MSCHAPv2 / PEAP via Rogue AP / Evil Twin — high value, active, big build

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
> lands.

## Features

### Hardware-failure UX — pre-alpha (release blocker)

> Moved here from `RELEASE-PLAN.md` § 2c. It gates the alpha, but it carries real
> design weight — it's a feature, not a logistics afterthought. This section is
> *what we think we want*; the wedge half is still an open design problem.

**Problem.** When a card fails, the UI is unhelpful: an init failure shows a
generic message, a runtime wedge just lets the Scanner fade to empty, and the only
real detail lives in `wifit3.log` — which a user gets *only* by knowing to set
`WIFIT3_LOG=1` before launch. That's a developer affordance, not a user one (and
it gets worse under PyInstaller/launchers, where there's no obvious shell to set an
env var in). A user who hits a failure should get a clear, actionable message
**and** be able to see the gory details — without a terminal, without an env var.

**Headline requirement: logs/details reachable from *inside* the UI.**
`wifit3.log` (behind `WIFIT3_LOG`) stays exactly what it is — a developer trace of
the code path, intentionally hidden from users. Separately, the UI must surface
what a *user* needs when something breaks: a plain reason up front, the technical
detail one expand away. The user never learns an env var exists.

**The error modal (the shape we want).**
- Main line, red, plain + actionable: *"Driver is borked — please unplug and
  replug the adapter."*
- A collapsed **Details** disclosure holding the full technical dump: the exception
  + stack trace, plus whatever state the driver knew (e.g. "RF went dead",
  register/hex values, addresses). Copy-able. *This* is the in-UI "logs" the
  requirement above asks for.
- Dismiss returns to the splash, where device re-discovery already runs on its 1 s
  poll — so a replug recovers without relaunching.

**Mechanism — two cases, and we strongly prefer `raise()` over callbacks.**
The ideal: a driver `raise`s at the point of failure, from anywhere in its code,
and that plops the user out to the modal with the message + stack trace. Callbacks
for this are explicitly *disliked* — they scatter the failure path. How achievable
that is splits by case:

1. **Init failure — the easy half, low-invasive.** `connect()` runs inside an
   awaited Textual worker, so a raised exception already has a call stack to ride
   up to one UI-level `except`. The reason it doesn't work today is *self-inflicted*:
   every driver wraps bring-up in `except Exception: return False` and swallows the
   cause, and `connect()` piles broad catches on top. The fix is mostly **deleting**
   those swallows so the exception propagates — less code, no new subsystem. One
   catch in the splash worker → modal → splash.

2. **Runtime wedge — the hard half, no plan yet.** A driver detects mid-session
   that it's borked and needs a replug. Several drivers *can* already self-detect
   this (warm-reattach bulk-IN smoke tests, RX-dead watchdogs). The trouble: it's
   often detected on a **detached background thread** (the RX reader) or a
   fire-and-forget hop task — there's no `await` for a `raise` to bubble to, so a
   raise there just dies on that thread. Whether raising works "depends on the
   stack trace": clean when the wedge is noticed during an awaited call (e.g.
   `set_channel`), useless when it's noticed off-thread.
   - **Open question — how does an off-thread wedge become a UI `raise` without
     callbacks?** One candidate to explore (undecided): the driver stashes the
     failure as state and a UI-side poll (the Scanner already ticks) notices it and
     raises at a UI-reachable point — turning it back into the clean "raise →
     modal" flow, no callback wiring. This is the core thing to design *before* any
     code.

**Hard constraints.**
- **Non-blocking + instant clean shutdown.** The app closes cleanly and instantly
  on `q` today, *including* after an init failure — that must stay true. The
  failure path can't join a dead thread, trap a modal, or otherwise make quit hang.
  (The earlier abandoned callback attempt's "won't close" was a *test-harness*
  hang, not the app — but it's the warning shot for this constraint.)

**Out of scope here (separate, later).** Non-fatal **toast** notifications — low
beacon rate, weak RX, per-driver `known_issues` surfaced on bring-up. Useful, but a
different mechanism and a lower urgency than "the card died, tell the user."

**Complexity.** Init half: low (delete the swallowing + one catch). Wedge half:
genuinely hard, design-doc-first — *not* a zero-shot. Confirmed failure modes to
cover when built: warm-reattach init wedge (RTL8822BU — replug message currently
lost behind a generic error) and runtime RX wedge (RTL8812AU — Scanner fades
silently).

> **Related consideration — a `BaseDriver` class (its own design, not a v1
> dependency).** Worth *considering*: an abstract `BaseDriver` that all drivers
> inherit, holding the logic genuinely common to every driver — and, since not all
> drivers run an RX reader thread (ar9271 doesn't), perhaps a
> `ReaderThreadDriver(BaseDriver)` tier for the ones that do. This is
> **significant** work: it touches all ~13 drivers and warrants a design of its own
> — the families differ enough (HTC/WMI vs direct-register vs MCU-firmware) that a
> premature base would be the wrong one (see `RELEASE-PLAN.md` Phase 5, "abstract
> after proven duplicated"). It's flagged *here* because it would pay off for the
> hard half above: if `BaseDriver` / `ReaderThreadDriver` already existed, surfacing
> a wedge from inside `RxReaderThread` (the off-thread `raise` problem) could live
> in one shared place instead of being re-implemented per driver — the DRY win. So:
> not required for v1, but a real reason `BaseDriver` is worth designing.

### Config persistence — pre-alpha

**Problem.** No stored config today — theme resets every launch (hardcoded
`textual-dark` in `ui/app.py`), WPS PBC auto-invade resets, paths reset, Scanner
sort resets.

**Approach.** A TOML file via `platformdirs` (`tomllib` is stdlib on 3.11+):
- Linux: `~/.config/wifit3/wifit3.toml`
- Windows: `%APPDATA%/wifit3/wifit3.toml`
- macOS: `~/Library/Application Support/wifit3/wifit3.toml`

Sticky settings: theme, Scanner sort column/direction, WPS PBC auto-invade,
`hashcat` path, capture output dir, channel filter defaults, update-check opt-out.

**Complexity.** Low-moderate — one storage layer, human-editable TOML.

> **Dropped:** the decloaked-SSID DB (persist `bssid → ssid` with confidence
> scoring). Narrow use case, over-engineered for the actual value, and a passive
> sniffing artifact with privacy weight. Punted indefinitely — do **not**
> re-pitch.

### Update check — soon

**Problem.** No way for a user to learn a newer release exists.

**Approach.** On startup, async-check `https://pypi.org/pypi/wifit3/json`, compare
`info.version` against the running version. If newer → toast "Update available:
v0.1.2". Non-blocking, 2–3 s timeout, fails silently if offline.
`--no-update-check` flag for airgapped / corporate users.

**Complexity.** Low. Depends on the package being on PyPI.

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

### Dynamic channel re-steering (handshake) — soon

**Problem.** Focus stays glued to the entry channel. If the AP CSA-jumps or shows
stronger signal on another band, we miss it.

**Approach.** Periodically probe nearby channels (<100 ms each) and re-tune. Ties
into ESSID-based targeting (one logical AP, multiple BSSIDs across bands).

**Complexity.** Moderate — touches the Focus channel/hop logic. Coordinate with
the Focus channel-tune race fix (Bugs/QoL below).

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

### Target-based RX gain steering (focus) — post-Defcon, validate first

**Problem.** When focused on one target, we passively accept whatever RX gain the
card's stock dynamic-gain loop lands on. On Realtek that loop is DIG — it drives
the initial gain (IGI, `0xc50`) to **minimize false alarms**, which is *not* our
goal. Our goal is to **maximize the target's frame rate**. Nobody does this —
airodump certainly doesn't; it takes whatever the driver gives. Biggest payoff is
the weak/marginal AP near the sensitivity floor — often exactly the handshake
we're straining to capture. (Purely RX-register tuning → passive, no TX.)

**Approach — same knob, different objective.** DIG already *is* a steering loop;
we want the same knob driven by target-beacons/sec instead of FA counters. The
kernel even has the hook: `odm_pause_dig` pins IGI and takes the automatic loop
out. So focus-steering = *pause the card's auto-gain, hill-climb its gain on the
target's observed frame rate, restore auto-gain on un-focus.*

Keep it from metastasizing across all ~13 drivers by splitting **the control loop
(generic, once)** from **the knob (tiny, per-card)**:
- An optional capability Protocol — `RxGainSteerable`: `gain_bounds()`,
  `get_rx_gain()`, `set_rx_gain(v)`. Realtek implements it as the IGI read/write we
  already have in the DKMS port (`0xc50`); setting gain pauses that card's DIG
  watchdog. Other families (rt2x00 link-tuner, mt76 AGC, ath9k) expose their own
  analogous gain/AGC watchdogs we could override the same way — each is ~3 methods,
  not a re-implemented loop.
- The hill-climb controller lives **once** in the Focus/`WlanInterface` layer:
  count the target's frames over a window, perturb gain ±1, keep the move if the
  rate improved, back off otherwise, with hysteresis. Card-agnostic.
- A driver that doesn't implement the Protocol simply gets no "steer" toggle —
  graceful, no steering code copied anywhere.

**The control realities (design around, don't wish away).** The feedback signal is
slow + noisy — ≤10 beacons/s, so telling 7/s from 8/s needs multi-second windows;
each hill-climb step costs seconds and the loop needs hysteresis/confidence or it
oscillates on noise. Per-frame RSSI (a sample every frame, not one per 100 ms) is a
faster proxy worth folding in. And the optimum drifts (RF is non-stationary), so
it's a *continuous* controller, not converge-and-stop.

**Honest caveat — gain steering helps weak targets, ~nothing for strong ones.**
The whole 8188eus beacon-rate hunt established that a strong AP's losses were
*external* (host load / USB power) with IGI already sitting more sensitive than the
kernel — steering it would move nothing. So this is a weak-target tool.

**Validate before building the abstraction.** Throwaway sweep first, reusing the
DKMS `0xc50` read/write + `beacon_watch` counting: pin IGI at each value
`0x1c…0x2a`, measure a **weak** AP's beacons/sec for ~10 s each, plot it. Peaked
curve (a best IGI that beats the DIG default) → real signal, build the
`RxGainSteerable` capability + generic controller (prove on the DKMS card, then
extend per-card). Flat curve → gain isn't the lever; drop it before writing any
cross-card layer. Pick a weak AP — the strong canary will read flat and tell us
nothing.

**Complexity.** Controller: moderate. Per-card: low (≈3 methods) *if* the
capability split holds. The validate-first sweep is cheap and decisive — do it
before committing.

### WPS improvements — post-Defcon

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

### Multi-card support (Minnie Drivers v2) — post-Defcon, big swing

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

---

Known bugs + QoL nits live in `BUGS.md`.
