# Wifit3 — Release Plan

Living document — the road to a public (open-source) Defcon release. This is the
**logistics + timeline master**: it sequences the work and owns the ship
blockers. Detail lives in the sibling docs:

- Hardware/driver work → `PORTING.md`
- Product/UX features + QoL bugs → `FEATURES.md`
- Current per-card state → `../VERIFICATION.md`

Defcon is the target event (~2 months). One week of vacation end of June eats
into the timeline — plan accordingly.

---

## Guiding principles

- **Alpha early, fix publicly.** Ship alpha with enough runway to squash bugs
  before Defcon. Silent failures and crashes on tier-1 cards are more damaging to
  reputation than missing features.
- **"Not vibe-coded slop."** The verification matrix, per-chip docs, and honest
  ⚠️ cells already signal rigor. The code and UX must match.
- **Tier-1 cards must be bulletproof.** RTL8812AU (AWUS036ACH), MT7612U
  (AWUS036ACM), AR9271 (AWUS036NHA) — these are in the most hands. Everything
  else is secondary.
- **Scope cuts are features.** WEP fragmentation, PixieWPS, 40/80 MHz, MT7921AU,
  multi-card — all deferred deliberately. Document limitations honestly rather
  than shipping them broken.

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

The DKMS re-ports are the headline hardware work and the fix for the cross-family
2.4 GHz RX weakness. **Full detail + priority order + per-card vendor sources live
in `PORTING.md`** (also: cards-in-the-mail, blank-EEPROM rescue, MT7921AU). One
port per session; testing + stability day(s) after each before the next. The
8812AU hop-death (random RF wedge under sustained scanning) is the primary scan
stability issue — expected to improve via the shared phydm RX/AGC path in the
8821au vendor port.

### Stress soaks (release-gating)

Run the longrun test script (all attacks, sustained channel hopping, **30 min**) on
tier-1 cards. **Gate alpha on clean soaks for MT7612U and AR9271 at minimum.**
RTL8812AU (DKMS, the default) cleared its 30-min soak; the ⚠️ RF hop-death is the
opt-in `WIFIT3_RTL8812=mainline` fallback only.

---

## Phase 2 — Release blockers (must ship before public)

### 2a. PII / git-history scrub ⚠️ DO THIS BEFORE ANYTHING GOES PUBLIC

Home BSSID, WEP test-router details, and related identifiers are in **git
history**, not just the working tree. This is the one thing that's hard to undo
after going public.

- ✅ Already safe: `usb_dumps/` and `data_dumps/` are gitignored, never committed.
- Decision required: `git filter-repo` surgical rewrite (preserves commit history
  + reasoning) vs a fresh orphan public repo (clean but loses history). **Lean
  filter-repo** — commit history has high signal for coding decisions beyond what
  comments capture. Do **not** squash. Decide *before* flipping public.
- Going forward this is covered by the `no SSIDs/BSSIDs in commits` rule.

### 2b. Licensing — decision: GPLv2

Drivers were **not** cleanroom-ported — Linux kernel source was referenced
throughout, and aircrack-ng's chopchop was studied. GPLv2 is the natural fit as a
derivative of the Linux kernel drivers, and signals respect to the kernel
community rather than extracting without giving back. (Not legal advice — record
the decision + reason.)

- [ ] Add `LICENSE` (GPLv2).
- [ ] PyInstaller exe distribution: GPLv2 requires source availability — the
  public GitHub repo satisfies this; document it.
- [ ] **Firmware blobs:** provenance is documented per-chip ("pcap-extracted,
  byte-verified vs linux-firmware"). Verify each blob is redistributable via the
  linux-firmware `WHENCE` file and add a one-line provenance per chip in its doc.
- [ ] Consider **Minnie Drivers** as a separate GPLv2 package post-Defcon — the
  cleanest way to give back to the ecosystem (see Post-Defcon).

### 2c. Hardware-failure UX

**Release blocker — gates the alpha.** Design + detail moved to `FEATURES.md`
§ "Hardware-failure UX" (it's a real feature with real design weight, not an
afterthought slot). The short of it: drivers `raise` on failure (we prefer that
over callbacks), one UI catch → red error modal with an expandable **Details**
box, and — the headline — logs/details reachable from *inside* the UI, not via a
`WIFIT3_LOG` env var. The runtime-wedge (off-thread) half is still an open design
problem; do it design-doc-first, not zero-shot.

### 2d. Zadig / udev automation

Goal: the app handles WinUSB/permission setup, no manual Zadig, no terminal sudo.

- **Windows:** detect known VID:PID via `usb.core`, show a per-device prompt
  ("RTL8812AU detected but not configured — [Setup WinUSB (requires Admin)]"),
  shell to a bundled `wdi-simple.exe` with the right VID:PID args, UAC handles
  elevation, device re-enumerates. Triggered by need (hot-plug), not a first-run
  wizard.
- **Linux:** install udev rules for known VID:PIDs via `pkexec` (polkit GUI auth).
  One rule per supported card, `MODE="0666"`. Permanent — subsequent plugs need no
  elevation. Same prompt pattern.
- **Unknown VID:PID:** "Unknown USB device (0bda:1234) — not supported by wifit3."

### 2e. Versioning

- Add `version = "0.1.0-alpha.1"` to `pyproject.toml`; expose `__version__`.
- Tag git: `git tag v0.1.0-alpha.1`.
- Semantic versioning: `0.x.x` pre-release, `0.1.x` alpha patches, `0.2.0` for
  multi-card etc.

### 2f. README + docs

The `README.md` **already exists** (pitch, TUI screenshots, supported-cards table
with the asterisk rule, feature list, how-it-differs-from-wifite, install via
uv/Zadig/rmmod, disclaimer) — this item is **review/polish**, not write-from-
scratch. Still to add:

- [ ] `CONTRIBUTING.md` — uv dev setup, the hardware-testing loop, the comment-
  style rule, where ground-truth docs live.
- [ ] **Authorized-use / ethics notice** — beyond the README's one-line
  disclaimer, a clear "authorized testing / your own networks only" statement
  (wifite/aircrack-ng both carry one). See Ethics & scope boundaries below.
- [ ] `ARCHITECTURE.md` — mostly distillation (the layer stack + module map
  already live in `CLAUDE.md`); add the `WlanDriver` Protocol contract. Feeds off
  the Phase 5 driver-comparison matrix.

---

## Phase 3 — Pre-alpha polish (time permitting)

Picked from `FEATURES.md` — prioritize the **signal-quality bar** and **config
persistence** (both low-cost, high-visibility), then the **update check**. The
Focus channel-tune race and the beacon-count truncation (Bugs/QoL in
`FEATURES.md`) are good cheap fixes for the same window.

---

## Phase 4 — Distribution + CI

### 4a. GitHub Actions

- `ci.yml` — on every PR: imports, unit tests, smoke test.
- `release.yml` — on git tag: matrix build Windows + Linux, PyInstaller on the
  Windows runner, create a GitHub Release with artifacts.
- `smoke.yml` — reusable: headless Textual launch, clean exit, catches bundling
  issues.

### 4b. Distribution targets

| Audience | Method | Python required |
|---|---|---|
| Non-technical Windows (kids test) | `wifit3.exe` (PyInstaller) | ❌ |
| Security researchers (any platform) | `pipx install wifit3` | ✅ |
| Power users | `git clone` + `uv run` | ✅ |
| Kali / Debian (eventually) | apt package (community) | ❌ |

PyInstaller bundles the interpreter + deps + firmware assets + `wdi-simple.exe`.
Expected 30–60 MB, acceptable for a security tool. Textual + PyInstaller has known
quirks — test carefully on the Windows runner. GPLv2 + PyInstaller: the public
GitHub repo satisfies the source-availability requirement.

### 4c. macOS (research only, not committed)

`detach_kernel_driver()` is unimplemented on the macOS libusb backend, and
unloading Apple's driver needs SIP disabled (a non-starter). The viable path is a
**codeless kext** (Info.plist only, no code) per supported card, declaring the
adapter's VID:PID with a high `IOProbeScore` so the kernel binds the do-nothing
kext and leaves the USB interface unclaimed for libusb. Ship a prebuilt one per
card. Unverified — no macOS hardware tested. Parked until someone wants it.

---

## Phase 5 — Code quality (pre or post alpha)

- **UI review** (`ui/*`) — the biggest blind spot (all Textual work was
  agent-authored). Read-only findings doc first, severity-ranked, each with a
  confidence level. **No speculative hardening** — working code does not get
  5×-complicated for an unprovable edge case; flag such things as optional with
  the tradeoff, never silently add. Bias to delete/simplify.
- **Driver-comparison matrix** — what each of the ~10 drivers does at each
  lifecycle stage (discover / cold-vs-warm connect / set_channel / inject / RX /
  close). Gives a holistic grok without reading every driver cold, **and** reveals
  where a shared base class is actually earned. Doubles as input for
  `ARCHITECTURE.md`. Abstract **surgically and test-backed** afterward — the
  families differ enough (HTC/WMI vs direct-register vs MCU-firmware) that a
  premature base class would be the wrong one. (`RxReaderThread` and `rtw88_base/`
  are the model: extracted *after* being proven duplicated.)
- **Comment cleanup** — rule codified in `CLAUDE.md` + memory. Date stamps remain
  in ~12 `.py` files (heaviest `rt2800usb/reg_init.py`). Do opportunistically
  (clean any file you touch) or as a deliberate per-module pass with small
  commits; `chips/ar9271/protocol/wmi.py` is the calibrated reference for the
  right aggressiveness.
- **De-vibe audit** — full review for agent-authored shortcuts, over-complicated
  edge-case handling, anything that wouldn't survive community scrutiny. Known
  offender: **`wlan/packet.py`** (the 802.11 frame parser) is dense with bare
  magic numbers — header offsets, FC type/subtype masks, IE tag IDs — that read
  as guesswork; replace with named constants + brief `[WIRE]`-cited rationale.
  - **Comment-voice exemplar:** commit `d0d94ac` (`docs(wps/campaign): Comment
    cleanup`) is the worked example of the target voice for **our own Python /
    orchestration** code — lean, human, one line where the code is, cruft cut
    (−63 net). Run it as a dedicated session: tell Claude to *forget the house
    comment guidance and write like a human*, with that commit's diff as the only
    reference. **Code-type split (do not collapse):** this lean voice is for app
    code only — **ported / RE'd driver code (`chips/`) stays comment-rich**, where
    the comments ARE the reverse-engineering knowledge (`ar9271/protocol/wmi.py`
    is the gold standard, ~33% comment-to-code). (If the § 2a history rewrite
    lands first, re-point this hash — `filter-repo` rewrites it.)

---

## Ethics & scope boundaries

The authorized-use notice (2f) is the headline. Two deliberate **boundaries** —
recorded so they aren't re-pitched as easy wins:

- **No configurable TX-power override.** The silicon supports power indices above
  the EFUSE regulatory caps and userland bypasses the kernel's clamping, so a knob
  is technically easy — but per-family constants differ wildly, "max index" means
  different dBm per chip, and a blanket `--tx-power N` invites real-world harm. A
  researcher who genuinely needs it in an RF cage should fork; owning that choice
  is the point. (Also feeds the safety/guardrails discussion.)
- **Thermal / DoS guardrails (parked).** Whether each chip exposes a thermal
  sensor; if so, a conservative watchdog (with hysteresis, fail-safe, no spurious
  trips) that cuts TX past a threshold. Long-term hardware burn-in / stress
  testing belongs on the same track.

---

## Post-Defcon backlog

Real features, deferred for timeline not merit. Detail in the sibling docs:

- **Multi-card support (Minnie Drivers v2)** → `FEATURES.md`. Run N cards
  concurrently; the demo writes itself.
- **Minnie Drivers standalone package** — separate the PyUSB userspace driver
  layer into its own GPLv2 library; Wifit3 ships as the reference implementation.
  Community builds other tools on top (triangulation, custom scanners). Central
  driver registry + hardware matrix. Lowers the contribution barrier. (Tied to the
  2b licensing/give-back strategy.)
- **MT7921AU (AWUS036AXML)** and **blank-EEPROM override (RT3572 rescue)** →
  `PORTING.md`.
- **WPS improvements, WPA3 downgrade, triangulation map** → `FEATURES.md`.
