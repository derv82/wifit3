# Wifit3 — Release Plan

Living document. Updated as milestones are completed. Defcon is the target
audience event (~2 months). One week of vacation end of June eats into the
timeline — plan accordingly.

---

## Guiding principles

- **Alpha early, fix publicly.** Ship alpha with enough runway to squash bugs
  before Defcon. Silent failures and crashes on tier-1 cards are more damaging
  to reputation than missing features.
- **"Not vibe-coded slop."** The hardware verification matrix, per-chip docs,
  and honest ⚠️ cells already signal rigor. The code and UX must match.
- **Tier-1 cards must be bulletproof.** RTL8812AU (AWUS036ACH), MT7612U
  (AWUS036ACM), AR9271 (AWUS036NHA). These are in the most hands. Everything
  else is secondary.
- **Scope cuts are features.** WEP fragmentation, PixieWPS, 40/80 MHz,
  MT7921AU, multi-card — all deferred deliberately. Document limitations
  honestly rather than shipping them broken.

---

## Timeline overview

```
Now          → End of June   DKMS ports + hardware + blockers
End of June  → 1 week        VACATION (no releases, no alphas)
Return       → Defcon        Alpha release, bug fixing, polish
Defcon                       Demo, community feedback
Post-Defcon                  Multi-card, Minnie Drivers, etc.
```

---

## Phase 1 — Core hardware (do first, gates everything)

### 1a. DKMS re-ports (priority order)

Mainline rtw88 is the source of the cross-family 2.4 GHz RX weakness. All four
re-ports use vendor/aircrack-ng/morrownr DKMS source as truth, cleanroom session
(no mainline context in view). Pcap-as-truth protocol applies: every USB transfer
in the working Kali DKMS capture must exist in the port; source explains why.

| Card | Vendor source | Expected gain | Notes |
|---|---|---|---|
| RTL8822BU | morrownr `88x2bu` | 8 → 29 APs (3.6×) | Highest payoff, do first |
| RTL8814AU | morrownr `8814au` | noisy mainline → robust 21–24 APs | Second priority |
| RTL8821AU | Lucid-Duck `8821au` | stability + carries 8812au | 2-for-1; preserve `SUPPORTS_SW_SEQ` / `en_hwseq=0` |
| RTL8812AU | rides 8821au port | bottom-tier 8–10 APs → lifted | No separate port; folds into 8821au as sibling |
| RTL8188EUS | defer unless proven win | breadth tied mainline ≈ DKMS | Skip unless concrete measurable gain appears |

One port per Claude Code session. Testing + stability day(s) after each port
before moving to the next. The 8812AU hop-death (random RF wedge under sustained
scanning) is the primary scan stability issue — expected to improve via the shared
phydm RX/AGC path in the 8821au vendor port.

### 1b. Cards in the mail

- **Panda PAU06** — RT5372, slots into existing `chips/rt2800usb/` as a DeviceID
  entry, minimal delta
- **ALFA AWUS036NH** — RT3070, same family, similar treatment

Bring-up + matrix verification once hardware arrives. No new port expected for
either — existing rt2800usb driver should cover both.

### 1c. Stress soaks

Run the longrun test script (all attacks, sustained channel hopping, 1 hour) on
tier-1 cards. Gate alpha on clean soaks for MT7612U and AR9271 at minimum.
RTL8812AU stress is ⚠️ by known limitation — document, don't block on it.

---

## Phase 2 — Release blockers (must ship before public)

### 2a. PII / git history scrub ⚠️ DO THIS BEFORE ANYTHING GOES PUBLIC

Home BSSID (`aa:bb:cc:dd:ee:01` / NETGEAR2G), WEP test router details, and related
identifiers are in git history, not just the working tree. This is the one thing
that's hard to undo after going public.

Decision required: `git filter-repo` surgical rewrite (preserves commit history
and reasoning) vs fresh orphan public repo (clean but loses history). Lean toward
filter-repo — commit history has high signal for coding decisions beyond what
comments capture. Do not squash.

Going forward: `no SSIDs/BSSIDs in commits` rule already in place.

### 2b. Licensing

**Decision: GPLv2.** Drivers were not cleanroom-ported — Linux kernel source
referenced throughout, aircrack-ng chopchop studied. GPLv2 is the natural fit as
a derivative of the Linux kernel drivers. Also signals respect to the kernel
community rather than extracting without giving back.

- [ ] Add `LICENSE` (GPLv2)
- [ ] PyInstaller exe distribution: GPLv2 requires source availability — public
  GitHub repo satisfies this, document it
- [ ] Firmware blobs: pcap-extracted, byte-verified vs linux-firmware. Verify
  each blob is redistributable via linux-firmware `WHENCE` file. Add one-line
  provenance per chip in the chip doc.
- [ ] Consider Minnie Drivers as a separate GPLv2 package post-Defcon — the
  cleanest way to give back to the ecosystem

### 2c. Hardware failure UX

Currently: driver wedge, init failure, and pipe errors log a warning but the UI
stays silent. Users see targets fade to dark or a generic error with no action.

Design:
- **Callback pattern** on `BaseDriver`: `on_warning(msg)` and `on_fatal(msg)`
  wired from driver layer to UI at instantiation
- **Toast** (`app.notify()`) for non-fatal warnings: low beacon rate, weak RX
  detected, known card limitations surfaced on bring-up
- **Modal** for hard failures requiring user action: wedge, pipe error, replug
  required — after exception catch + failed recovery attempt, not before
- `call_from_thread` required (watchdog runs in background thread)
- Each driver declares `known_issues = [...]` at class level — shown as toast on
  successful bring-up

Two confirmed failure modes to fix:
- Warm-reattach init wedge (RTL8822BU): splash screen shows generic
  `RuntimeError` instead of replug message
- Runtime RX wedge (RTL8812AU): Scanner fades to empty, no message

### 2d. Zadig / udev automation

Currently requires manual Zadig for Windows (selecting correct interface is
error-prone) and sudo for Linux. Goal: the app handles everything.

**Windows:** detect known VID:PID via `usb.core`, show per-device prompt
"RTL8812AU detected but not configured — [Setup WinUSB (requires Admin)]",
shell to bundled `wdi-simple.exe` with correct VID:PID args, UAC handles
elevation, device re-enumerates automatically. Triggered by need (hot-plug),
not a first-run wizard.

**Linux:** install udev rules for known VID:PIDs via `pkexec` (polkit GUI auth,
no terminal sudo). One rule per supported card, `MODE="0666"`. Permanent fix —
subsequent plugs need no elevation. Same prompt pattern as Windows.

**Unknown VID:PID:** "Unknown USB device (0bda:1234) — not supported by wifit3."

### 2e. Versioning

- Add `version = "0.1.0-alpha.1"` to `pyproject.toml`
- Expose `__version__` in the package
- Tag git: `git tag v0.1.0-alpha.1`
- Semantic versioning: `0.x.x` pre-release, `0.1.x` alpha patch increments,
  `0.2.0` for multi-card etc.

### 2f. README + docs

- `README.md`: one-line pitch, TUI screenshots, supported-cards table (user-
  facing), feature list, how it differs from wifite (PyUSB userspace, no
  aircrack subprocess wrappers, cross-platform including Windows via WinUSB),
  installation instructions
- `CONTRIBUTING.md`: uv dev setup, hardware testing loop, comment style,
  ground-truth doc locations
- Authorized-use / ethics notice: "authorized testing / your own networks only"
  (wifite/aircrack-ng both carry one)
- `ARCHITECTURE.md`: layer stack, module map, `WlanDriver` Protocol contract

---

## Phase 3 — Polish (pre-alpha, time permitting)

### 3a. Config persistence

No stored config today — theme resets every launch, PBC auto-invade resets,
paths reset. Fix: TOML file via `platformdirs`:
- Linux: `~/.config/wifit3/wifit3.toml`
- Windows: `%APPDATA%/wifit3/wifit3.toml`
- Mac: `~/Library/Application Support/wifit3/wifit3.toml`

`tomllib` is stdlib (Python 3.11+). Sticky settings: theme, scanner sort
column/direction, WPS PBC auto-invade, hashcat path, capture output dir, channel
filter defaults, update check opt-out.

Decloaked SSID DB: **punted indefinitely** — narrow use case, over-engineered
for actual value.

### 3b. Update check

On startup, async check `https://pypi.org/pypi/wifit3/json`, compare
`info.version` against running version. If newer → toast "Update available:
v0.1.2". Non-blocking, 2–3 second timeout, fails silently if offline.
`--no-update-check` flag for airgapped / corporate users.

### 3c. Signal quality bar

Replace raw beacons/sec display with a 10-glyph colored reception bar:
- Each glyph ≈ 1 beacon/s of the ~10/s ceiling (one beacon per 102.4 ms interval)
- Red 1–3 / orange 4–7 / green 8–10
- Feeds directly from existing `beacon_history` — no new data collection

### 3d. WEP fragmentation gating

Gate `fragmentation.py` / `campaign.py` on `iface.supports_sw_seq`. Disable /
grey the Frag button (or refuse with a clear message) for cards that don't
support it instead of spinning on "seed wouldn't relay." Only RTL8821AU has
`SUPPORTS_SW_SEQ` today.

### 3e. Focus channel tune race fix

Entering Focus on an AP occasionally shows 0 beacons/s until re-entering.
Confirmed cross-family (RT3572, MT7610U) — bug is in the shared
Focus→stop-hop→`set_channel` path, not a driver. Likely a race/ordering issue
where the channel set on Focus entry is overridden by hopper teardown.

### 3f. Beacon count column truncation

`10512` renders as `0512`. Auto-size the BEACONS column without breaking
right-alignment.

### 3g. Client fingerprinting (if time allows)

Emoji client identification in the clients list (one character left of BSSID).
All in one `fingerprint.py` module, no SQLite or JSON lookups:
- Top ~50 OUI prefixes hardcoded (Apple, Samsung, Google, Amazon/Ring/FireTV,
  Roku, Nest, Microsoft, Sony, Nintendo)
- IE fingerprinting for ambiguous OUIs (Murata/Intel modules used across devices)
- Returns `(emoji, device_class, confidence)` — blank if confidence too low
- Focus detail panel shows full breakdown
- IoT devices (Ring/Blink/Nest/Roku/FireTV) are highest-value targets for
  engagement scoping — prioritize those OUIs

---

## Phase 4 — Distribution + CI

### 4a. GitHub Actions

- `ci.yml`: runs on every PR — imports, unit tests (537 passing), smoke test
- `release.yml`: triggers on git tag — matrix build Windows + Linux, PyInstaller
  on Windows runner, create GitHub Release with artifacts
- `smoke.yml`: reusable — headless Textual launch, clean exit, catches bundling
  issues

### 4b. Distribution targets

| Audience | Method | Python required |
|---|---|---|
| Non-technical Windows (kids test) | `wifit3.exe` (PyInstaller) | ❌ |
| Security researchers (any platform) | `pipx install wifit3` | ✅ |
| Power users | `git clone` + `uv run` | ✅ |
| Kali / Debian (eventually) | apt package (community) | ❌ |

PyInstaller bundles Python interpreter + all deps + firmware assets +
`wdi-simple.exe`. Expected size 30–60 MB, acceptable for a security tool.
Textual + PyInstaller has known quirks — test carefully on Windows runner.

GPLv2 + PyInstaller: public GitHub repo satisfies source availability requirement.

### 4c. macOS (research only, not committed)

`detach_kernel_driver()` unimplemented on macOS libusb backend. Viable path is
a codeless kext (Info.plist only, no code) per supported card. Unverified — no
macOS hardware tested. Parked until someone wants it.

---

## Phase 5 — Code quality (pre or post alpha)

- **UI review** (`ui/*`): read-only findings doc first, severity-ranked with
  confidence levels. No speculative hardening. Bias to delete/simplify.
- **Driver comparison matrix**: what each driver does at each lifecycle stage
  (discover / cold+warm connect / set_channel / inject / RX / close). Feeds
  `ARCHITECTURE.md`, reveals where shared base class is earned.
- **Comment cleanup**: date stamps remain in ~12 `.py` files. Do opportunistically
  (clean any file you touch) or as deliberate per-module pass. `chips/ar9271/
  protocol/wmi.py` is the calibrated reference for right aggressiveness.
- **De-vibe audit**: full code review for agent-authored shortcuts, over-
  complicated edge case handling, anything that wouldn't survive community scrutiny.

---

## Post-Defcon backlog

These are real features, not rejected ideas. Deferred for timeline, not merit.

**Multi-card support (Minnie Drivers v2)**
Run N USB cards concurrently. Pooled RX (~N× beacons), coordinated channel
strategy (split band per card), TX/RX split (dedicated inject card so deauth
never misses its own handshake), hot-plug (plug in a card mid-session, watch
beacons jump). The demo writes itself. Architecture: `CardPool` abstraction
presenting single-driver interface to everything above it. Big refactor —
`WlanInterface` is singular throughout today.

**Minnie Drivers standalone package**
Separate the PyUSB userspace driver layer into its own GPLv2 library. Wifit3
ships as the reference implementation. Community can build other tools on top
(triangulation, custom scanners, research tools). Central driver registry with
hardware matrix documentation. Lowers contribution barrier — adding a chipset
becomes a contained, approachable task.

**MT7921AU (AWUS036AXML)**
Paused: active monitor mode crashes firmware as of early 2026, FW_START_REQ
wall on bring-up, RX uses deep pre-submitted URB pool incompatible with current
sync transport. Would need libusb async URB port (`libusb_submit_transfer`,
pre-submit ~32 URBs/EP). High effort, unstable upstream. Revisit post-Defcon.

**Blank EEPROM override (RT3572 rescue)**
Inject known-good 512-byte image into RAM struct for cards with blank EFUSE.
Soft override only — never burn fuses. Gate behind explicit CLI opt-in. May
rescue the counterfeit AWUS051NH v2 unit; builds the genuine-no-EFUSE-card
feature either way.

**WPS improvements**
- Multi-router lock-cycle matrix (only AirLink soft-lock tested)
- Terminal hard-lock escape hatch (stop and tell user instead of looping forever)
- Focus WPS panel
- PixieWPS (numpy/glibc dependency question to settle first)

**WPA3 downgrade** (transition mode — respond to probe requests)

**Triangulation map** (three cards + RSSI trilateration + drag-to-place UI)
Fun, novel, post-1.0. 😄

---

## Idea graveyard (decided against, don't re-pitch)

- **Configurable TX-power override** — per-family constants differ, "max index"
  means different dBm per chip, invites real-world harm. Researcher who needs it
  in an RF cage should fork.
- **Evil Twin** (2nd interface) — unproven value, low priority.
- **Decloaked SSID DB** — narrow use case, UX doesn't justify the complexity.
- **OUI vendor in Scanner table** — no room, vendor ≠ device type anyway
  (identifies WiFi module not device brand). Revived as client fingerprint
  emojis in Focus panel instead (§3g).
- **40/80 MHz channel width** — wifit3 is 20 MHz primary only. Every frame it
  captures and transmits rides the primary at legacy rates. 40/80 buys nothing.
