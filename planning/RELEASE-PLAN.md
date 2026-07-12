# Wifit3 — Release Plan

- BUGS & QoL: `BUGS.md`
- Hardware/driver work → `docs/porting/METHODOLOGY.md`
- Product/UX features → `FEATURES.md`
- Current per-card state → `../VERIFICATION.md`

---

## Core hardware

The DKMS re-ports are the headline hardware work (the cross-family 2.4 GHz RX fix). **Detail,
priority order, vendor sources, and cards-in-the-mail live in `docs/porting/METHODOLOGY.md`.** One port per
session; stability day(s) between.

---

## Release blockers

### Brick-risk disclaimer ⚠️ (userland USB can damage hardware)

**Release blocker — visible before public.** Userland USB writes registers + the FW-download
path with no kernel driver between us and the silicon, so a bad write/FW-page/power-seq can
brick a card or leave it in an illegal RF state. Amplified by AI-assisted porting (non-
deterministic; the pcap gate catches *inaccurate* sequences, not every *dangerous* one) and
community PRs. Required:
- [x] **README disclaimer** (reinforced by the LICENSE): *"This software talks to USB Wi-Fi
  hardware at the register level. It can damage or permanently disable ('brick') a device. Use
  at your own risk; no liability for hardware damage."* GPLv2 §15–16 (NO WARRANTY) is the legal
  backstop; this is the human-readable layer.
- [x] **Porting-safety warning** (`docs/porting/METHODOLOGY.md` Step 0 callout): port at your own risk, test
  on hardware you can lose; risk peaks at FW-download / EFUSE / power-seq.
- [x] **No-fuse-burn invariant** (`docs/porting/METHODOLOGY.md` Prerequisites): we only write RAM/registers +
  replay the vendor *download* path, never program EFUSE/EEPROM fuses — documented so a
  fuse-write PR is an obvious red flag.
- [ ] (Optional, post-alpha) first-run acknowledgment for the dev/porting tools.

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

## Code cleanup / de-vibe

### Delete AR9271 v1 — ✅ DONE (2026-07-12)

v2 is the maintained clean-room re-port and the default; v1 was dead (heard 1 AP total, channel
hopping broken — 5.3 bcn/s on ch1 only vs v2's 8.0/s across 71 APs). Done:
- ✅ Deleted `chips/ar9271/`, its tests, `scripts/ar9271/`, and the verify_pcap stub (254f6eb).
- ✅ Dropped the `WIFIT3_AR9271` env var + v1 branch; registry `"ar9271"` → `AR9271V2Driver`.
- ✅ Purged v1 doc refs (CLAUDE.md, VERIFICATION.md, STEERING.md).
- ⏸ Deliberately NOT renaming `ar9271_v2` → plain `ar9271` (kept the suffix for now — derv's call).

---
