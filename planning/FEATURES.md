# Wifit3 — Features & QoL Backlog

Forward-looking. Product/UX features we plan to build, and the small
bugs/quality-of-life fixes. Each entry: the problem it solves, the approach, and
a rough complexity/notes read. Driver/hardware work lives in `PORTING.md`;
release logistics in `RELEASE-PLAN.md`; current state in `../VERIFICATION.md`.

Ordering within each section is rough priority: pre-alpha → soon → post-Defcon.

---

## Features

### Signal-quality bar (replace raw beacons/sec) — pre-alpha

**Problem.** Raw "beacons/sec" is a poor display: it ceilings at ~9.77/s (one
beacon per 102.4 ms beacon interval), and "3/s → red" conveys nothing but "weak."

**Approach.** A **reception-quality bar** normalized to that ceiling — 100 % =
every beacon the AP sent was received (0 % loss). 10-glyph colored bar (Textual):
each glyph ≈ 1 beacon/s of the ~10/s max, colorized **red 1–3 / orange 4–7 /
green 8–10**. The running beacons/sec already collected by `beacon_history` feeds
it directly — no new data collection.

**Complexity.** Low — display-only, data already exists. (Rejected alternative: a
bare "XX % loss" number — accurate but not glanceable.)

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
  learned-backoff cycles with zero progress → stop and tell the user to
  reboot/toggle WPS" (the warm-reattach "please replug" honesty pattern) instead
  of spinning silently.
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

### WPA3 downgrade (transition mode) — post-Defcon

Respond to probe requests to elicit a downgrade in WPA3 transition-mode networks.

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

### Triangulation map — post-1.0

Three cards + RSSI trilateration + a drag-to-place UI. Fun, novel, not soon. 😄

---

Known bugs + QoL nits live in `BUGS.md`.
