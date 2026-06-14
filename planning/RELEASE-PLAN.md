# Wifit3 — Release Plan

- BUGS & QoL: `BUGS.md`
- Hardware/driver work → `PORTING.md`
- Product/UX features → `FEATURES.md`
- Current per-card state → `../VERIFICATION.md`

---

## Core hardware

The DKMS re-ports are the headline hardware work and the fix for the cross-family
2.4 GHz RX weakness. **Full detail + priority order + per-card vendor sources live
in `PORTING.md`** (also: cards-in-the-mail, blank-EEPROM rescue). One
port per session; testing + stability day(s) after each before the next. The
8812AU hop-death (random RF wedge under sustained scanning) is the primary scan
stability issue — expected to improve via the shared phydm RX/AGC path in the
8821au vendor port.

### Stress soaks

Run the longrun test script (all attacks, sustained channel hopping, **30 min**) on
tier-1 cards. **Gate alpha on clean soaks for MT7612U and AR9271 at minimum.**
RTL8812AU (DKMS, the default) cleared its 30-min soak; the ⚠️ RF hop-death is the
opt-in `WIFIT3_RTL8812=mainline` fallback only.

---

## Release blockers

### PII / git-history scrub ⚠️ DO THIS BEFORE ANYTHING GOES PUBLIC

Home BSSID, WEP test-router details, and related identifiers are in **git
history**, not just the working tree. This is the one thing that's hard to undo
after going public.

- ✅ Already safe: `usb_dumps/` and `data_dumps/` are gitignored, never committed.
- Decision required: `git filter-repo` surgical rewrite (preserves commit history
  + reasoning) vs a fresh orphan public repo (clean but loses history). **Lean
  filter-repo** — commit history has high signal for coding decisions beyond what
  comments capture. Do **not** squash. Decide *before* flipping public.
- Going forward this is covered by the `no SSIDs/BSSIDs in commits` rule.

### Licensing — decision: GPLv2

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
- [ ] Consider **Minnie Drivers** as a separate GPLv2 package — the
  cleanest way to give back to the ecosystem.

### Versioning

- Add `version = "0.1.0-alpha.1"` to `pyproject.toml`; expose `__version__`.
- Tag git: `git tag v0.1.0-alpha.1`.
- Semantic versioning: `0.x.x` pre-release, `0.1.x` alpha patches, `0.2.0` for
  multi-card etc.

### Documentation

Still to add:

- [ ] `CONTRIBUTING.md` — uv dev setup, the hardware-testing loop, the comment-
  style rule, where ground-truth docs live.
- [ ] **Authorized-use / ethics notice** — beyond the README's one-line
  disclaimer, a clear "authorized testing / your own networks only" statement
  (wifite/aircrack-ng both carry one). See Ethics & scope boundaries below.
- [ ] `ARCHITECTURE.md` — mostly distillation (the layer stack + module map
  already live in `CLAUDE.md`); add the `WlanDriver` Protocol contract. Feeds off
  the Phase 5 driver-comparison matrix.

### Brick-risk disclaimer ⚠️ (userland USB can damage hardware)

**Release blocker — must be visible before the repo goes public.** Userland USB (PyUSB/libusb)
writes a card's registers + firmware-download path with **no kernel driver between us and the
silicon**. A wrong register write, a malformed firmware page, or a power-sequence misstep can
leave a device unresponsive (a "brick") or in an illegal RF/regulatory state. Two amplifiers
make this a **hard, prominent** disclaimer, not a soft one:

- **AI-assisted self-porting.** The porting playbook leans on AI agents, which are
  **non-deterministic** — an agent can emit a register sequence that bricks a card. The pcap
  verifier catches *unfaithful* sequences, not every *dangerous* one (a faithful-looking port
  can still misbehave on a colder boot).
- **Community PRs.** A well-meaning but wrong PR can ship a bricking sequence; review can't
  catch every hardware-specific footgun.

Required before public:
- [ ] **Top-level hardware-risk disclaimer** in `README.md` (reinforced by the LICENSE): plain
  language — "This software talks to USB Wi-Fi hardware at the register level. It can damage or
  permanently disable ('brick') a device. Use entirely at your own risk; the authors and
  contributors accept no liability for hardware damage." GPLv2 §15–16 (NO WARRANTY) is the legal
  backstop (see 2b); this is the human-readable, hardware-specific layer on top.
- [ ] **Porting-safety warning** in `PORTING.md` / `CONTRIBUTING.md`: porting your own card
  (especially with an AI agent) is at your own risk; test on hardware you can afford to lose;
  the brick risk peaks during firmware-download, EFUSE/EEPROM, and power-sequence work.
- [ ] **No-fuse-burn invariant, stated + reviewed:** wifit3 only ever writes RAM/registers and
  replays the vendor's *download* path — it does **not** program EFUSE/EEPROM fuses (one-time,
  irreversible). Make it a documented invariant so a PR adding a fuse-write is an obvious red
  flag. (Ties to the Blank-EEPROM override below — soft RAM override only, never burn.)
- [ ] (Optional, post-alpha) a first-run acknowledgment for the dev/porting tools.

---

## Code quality

- **UI review** (`ui/*`) — the biggest blind spot (all Textual work was
  agent-authored). Read-only findings doc first, severity-ranked, each with a
  confidence level. **No speculative hardening** — working code does not get
  5×-complicated for an unprovable edge case; flag such things as optional with
  the tradeoff, never silently add. Bias to delete/simplify.
- **Comment cleanup** — rule codified in `CLAUDE.md` + memory. Date stamps remain
  in ~12 `.py` files (heaviest `rt2800usb/reg_init.py`). Do opportunistically
  (clean any file you touch) or as a deliberate per-module pass with small
  commits; `chips/ar9271/protocol/wmi.py` is the calibrated reference for the
  right aggressiveness.
- **Code Quality audit** — full review for shortcuts, over-complicated
  edge-case handling, anything that wouldn't survive community scrutiny. Known
  offender: **`wlan/packet.py`** (the 802.11 frame parser) is dense with bare
  magic numbers — header offsets, FC type/subtype masks, IE tag IDs — that read
  as guesswork; replace with named constants + brief `[WIRE]`-cited rationale.

---
