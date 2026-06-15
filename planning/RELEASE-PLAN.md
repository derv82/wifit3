# Wifit3 — Release Plan

- BUGS & QoL: `BUGS.md`
- Hardware/driver work → `PORTING.md`
- Product/UX features → `FEATURES.md`
- Current per-card state → `../VERIFICATION.md`

---

## Core hardware

The DKMS re-ports are the headline hardware work (the cross-family 2.4 GHz RX fix). **Detail,
priority order, vendor sources, and cards-in-the-mail live in `PORTING.md`.** One port per
session; stability day(s) between.

### Stress soaks

Run the longrun test script (all attacks, sustained channel hopping, **30 min**) on
tier-1 cards. **Gate alpha on clean soaks for MT7612U and AR9271 at minimum.**
RTL8812AU (DKMS, the default) cleared its 30-min soak; the ⚠️ RF hop-death is the
opt-in `WIFIT3_RTL8812=mainline` fallback only.

---

## Release blockers

### PII / git-history scrub ⚠️ DO THIS BEFORE ANYTHING GOES PUBLIC

Home BSSID, WEP-router details, and related IDs are in **git history**, not just the tree —
hard to undo post-public. (`usb_dumps/`, `data_dumps/` already gitignored.) Decide before going
public: **lean `git filter-repo`** (surgical, keeps history's decision-signal) over a fresh
orphan repo; do **not** squash. Going forward, the `no SSIDs/BSSIDs in commits` rule covers it.

### Licensing — decision: GPLv2

Drivers weren't cleanroom-ported (kernel source referenced throughout; aircrack chopchop
studied), so GPLv2 is the natural fit as a kernel-driver derivative + gives back. (Not legal
advice.)

- [ ] Add `LICENSE` (GPLv2).
- [ ] PyInstaller exe: GPLv2 needs source availability — the public repo satisfies it; document.
- [ ] **Firmware blobs:** verify each redistributable via linux-firmware `WHENCE`; one-line
  provenance per chip.
- [ ] Consider shipping **Minnie Drivers** as a separate GPLv2 package.

### Versioning

- Add `version = "0.1.0-alpha.1"` to `pyproject.toml`; expose `__version__`.
- Tag git: `git tag v0.1.0-alpha.1`.
- Semantic versioning: `0.x.x` pre-release, `0.1.x` alpha patches, `0.2.0` for
  multi-card etc.

### Documentation

Still to add:

- [ ] `CONTRIBUTING.md` — uv setup, the hardware-testing loop, comment-style rule, ground-truth
  doc locations.
- [ ] **Authorized-use / ethics notice** — a clear "your own networks / authorized testing
  only" statement (wifite/aircrack carry one), beyond the README one-liner.
- [ ] `ARCHITECTURE.md` — distill the layer-stack/module-map from `CLAUDE.md` + the
  `WlanDriver` Protocol contract.

### Brick-risk disclaimer ⚠️ (userland USB can damage hardware)

**Release blocker — visible before public.** Userland USB writes registers + the FW-download
path with no kernel driver between us and the silicon, so a bad write/FW-page/power-seq can
brick a card or leave it in an illegal RF state. Amplified by AI-assisted porting (non-
deterministic; the pcap gate catches *unfaithful* sequences, not every *dangerous* one) and
community PRs. Required:
- [ ] **README disclaimer** (reinforced by the LICENSE): *"This software talks to USB Wi-Fi
  hardware at the register level. It can damage or permanently disable ('brick') a device. Use
  at your own risk; no liability for hardware damage."* GPLv2 §15–16 (NO WARRANTY) is the legal
  backstop; this is the human-readable layer.
- [ ] **Porting-safety warning** (`PORTING.md`/`CONTRIBUTING.md`): port at your own risk, test
  on hardware you can lose; risk peaks at FW-download / EFUSE / power-seq.
- [ ] **No-fuse-burn invariant:** we only write RAM/registers + replay the vendor *download*
  path, never program EFUSE/EEPROM fuses — documented so a fuse-write PR is an obvious red flag.
- [ ] (Optional, post-alpha) first-run acknowledgment for the dev/porting tools.

---

## Code quality

- **UI review** (`ui/*`) — biggest blind spot (all agent-authored). Read-only, severity-ranked
  findings doc first; **no speculative hardening** (flag edge-case handling as optional, never
  silently add); bias to delete/simplify.
- **Comment cleanup** — date stamps remain in ~12 `.py` files (heaviest `rt2800usb/reg_init.py`);
  clean opportunistically or per-module. `chips/ar9271/protocol/wmi.py` is the reference for tone.
- **Code-quality audit** — full pass for shortcuts / over-complicated edge cases. Known offender:
  `wlan/packet.py` (802.11 parser) is bare magic numbers (offsets, FC masks, IE tags) → named
  constants + `[WIRE]` cites.

---
