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

---

## Release blockers

### Documentation

Still to add:

- [ ] `CONTRIBUTING.md` — uv setup, the hardware-testing loop, comment-style rule, the
  `<CHIP>.md` port-reference doc locations.
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
- [x] **README disclaimer** (reinforced by the LICENSE): *"This software talks to USB Wi-Fi
  hardware at the register level. It can damage or permanently disable ('brick') a device. Use
  at your own risk; no liability for hardware damage."* GPLv2 §15–16 (NO WARRANTY) is the legal
  backstop; this is the human-readable layer.
- [x] **Porting-safety warning** (`PORTING.md` Step 0 callout): port at your own risk, test
  on hardware you can lose; risk peaks at FW-download / EFUSE / power-seq.
- [x] **No-fuse-burn invariant** (`PORTING.md` Prerequisites): we only write RAM/registers +
  replay the vendor *download* path, never program EFUSE/EEPROM fuses — documented so a
  fuse-write PR is an obvious red flag.
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
- **Kill Focus V1** — delete `ui/screens/focus.py` (the legacy panel grid), the `WIFIT3_FOCUS_V1`
  routing in `app.py`, and `planning/FOCUS-REDESIGN.md`. V2 is the default and proven; V1 was
  kept only as a soak fallback. Self-contained delete (watch for v1-only tests).

---

## Channel Tune Crackdown

The ar9271 shipped a 2-channel placeholder that silently pinned every tune to CH6 — a
card-wrecking bug that bring-up testing (CH1/CH6 only) sailed right past. Before release,
exercise the tune path on every common channel, every card:

- Test every (common) channel on every card thoroughly.
  - Use test routers (dual 2G & 5G) when possible to enforce a high expected beacon throughput.
  - Common 2G: 1, 3, 6, 8, 11
  - Common 5G: 36, 44, 149, 157
- Focus a unique target in every common channel.
  - Look for: lower beacon rate compared to other channels.
- Channel-filter each of the common channels.
  - Look for: seeing *other* channels' APs when filtering for one channel.

---
