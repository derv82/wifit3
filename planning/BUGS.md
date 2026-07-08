# Wifit3 — Known Bugs & QoL

Open bugs and unverified claims only — what's broken or unconfirmed *now*. Fixed work lives in git
history, not here. Cross-cutting issues first, then per-card. Deeper per-chip detail is in each
`<CHIP>.md`; per-attack pass/fail per card is `../VERIFICATION.md`.

`⏳` = a claim lifted from a chip's `<CHIP>.md`, **unconfirmed on hardware** — a HW sweep should
confirm it or kill it (kill → delete the line here *and* in the chip doc). `⚠️` = a chip doc that
contradicts itself; resolve on HW.

---

## Per-card

Clean, no known bugs: **rt2500usb, rt2800usb, rt3070, rt5372, rtl8821au (mainline)**.

### AR9271 (V1) is broken
- v1 heard 1 AP total, nothing off channel 1 — its channel hopping is dead (mud2g 5.3/s on ch1 only
  vs v2's 8.0/s across 71 APs, matching linux). v2 is the maintained clean-room re-port + the default.
  TODO: delete v1 (`chips/ar9271/`), drop the `WIFIT3_AR9271` env var + v1 branch in `manager.py`,
  remove v1 tests + mainline/v1 doc refs, then move `ar9271_v2` into its place (plain AR9271 driver,
  no v2 suffix). Purge every v1 remnant *before* the move.

### rtl8187 (8187L)
- ⏳ Injected deauth uses `duration=0` instead of the unicast-ACK NAV — minor TX nit [RTL8187L.md].
- Limit: always cold-inits (no warm reattach) — replug to recover.

### mt76x2u (AWUS036ACM)
- ⏳ Endpoint stability across power cycles unknown; channel-switch wants ~2 s settle [MT76X2U.md].

### rtl8188eus (mainline) — prefer the DKMS variant
- ⏳ Intermittent RX collapse — bad windows hear the reference AP worse than further neighbours [RTL8188EUS.md].
- ⏳ Encryption mislabel (WPA2 shown as "WEP") — fix awaits HW reconfirm [RTL8188EUS.md].
- Limit: mainline RX tops ~77 % with collapses; the DKMS re-port is the better card.

### rtl8188eus_dkms
- RX-perf gap: parity fixed-channel, but REAL in the hopping sweep (2026-07-07). Fixed-channel 60 s on
  a strong AP: port 6.5/s ≈ kernel-usbcap 6.2/s (parity). But the 7/6 SAME-SESSION Kali sweep A/B
  (hops 1–13 @ 15 s, our driver vs kernel `8188eu`, same box) shows port 5.3 vs kernel 7.0 on the
  reference AP — a real ~18% gap (after the 16s-vs-14s span artifact), concentrated on weaker /
  adjacent-channel APs (ch1/2 parity, ch3–11 deficits). Ruled out by live RX-counter instrumentation:
  USB/software drop (we deliver 92–97% of chip-demodulated frames) and gross DIG deafness (IGI parks
  0x20–0x22, sensitive). DIG is fully faithful in monitor: `phydm_dig_go_up_check` (and its NHM→DIG
  feedback) is dead code here — it early-returns true in `PHYDM_PERFORMANCE_MODE`, which this driver
  assigns once and never flips (hal_dm.c:202), so our unconditional IGI raise matches the vendor's
  0x20→0x22 step. (Verified 2026-07-07; documented in `dig._new_igi_by_fa` — not ported, it'd be a
  no-op.) No single smoking gun. Remaining port-side lead: a small busy-channel pipeline loss (ch11
  92% vs ch1 97% of chip-demodulated frames — URB/buffer under load); the rest is sweep
  span/measurement confounds + environment. NB mainline had a *smaller* sweep gap than dkms, so a
  default flip isn't justified on rate [RTL8188EUS_DKMS.md].
- EFUSE board options: resolved for register-wire correctness — antenna (`0xC9`) and regulatory
  (`0xC1`) are inert in this build, channel plan (`0xB8`) is SW-only; the one wire-affecting byte,
  external-PA/LNA (`0xCA[3:2]`), is now fail-loud (`efuse.assert_board_options_ported`). Residual: an
  external-PA/LNA unit is *refused*, not supported (needs `PHY_SetRFEReg_8188E` + the ext-LNA AGC
  table) [RTL8188EUS_DKMS.md].

### rtl8812au (mainline, opt-in `WIFIT3_RTL8812=mainline`)
- Mainline wedges on multi-band hopping (rtw88 HW limit) with no UI feedback — confirmed 2026-07-07
  (RF synth lost lock, ch153/161 dropped to ~0.5/s). The default (DKMS) driver hops clean, so this
  bites only the opt-in legacy driver; no UI surfaces the mid-run RF-synth loss [RTL8812AU.md].
- Note: the OOT DKMS driver (`rtl8812au` 5.13.6) won't compile on kernel 6.19 (`drv_types.h` include
  path) — no fresh same-driver linux-DKMS baseline possible on this box; graded vs prior + live hop.

### rtl8821au_dkms
- ⏳ 5 GHz deauth/TX (ch149) unverified on HW — offline byte-exact only; 2.4 GHz TX is confirmed [RTL8821AU_DKMS.md].

### rtl8821cu_dkms
- ⏳ WEP + Stress soak not yet run (Scan/handshake/PMKID/WPS confirmed on 2.4 + 5 GHz).
- No bring-up progress — `connect()` sits at 0% then jumps to 100%, so the UI flashes straight to
  Scanner with no feedback. Add ~5 `progress_cb` callbacks across the bring-up phases.

### rtw88_8814au (mainline) — prefer the DKMS variant
- ⏳ Weak 2.4 GHz RX (2G AP −82 dBm vs 5G −54; 0.5–2 bcn/s vs ~10) — 2G AGC/gain suspect [RTL8814AU.md].
- ⏳ Fast-hop RX death — ~1 s of 0 frames/s at 0.25 s dwell; PLL relock eats the dwell [RTL8814AU.md].
- ⚠️ Chip doc contradicts itself on `spur_calibration` (skipped vs ported) and IQK — resolve on HW [RTL8814AU.md].

### rtl8814au_dkms
- Wedges under TX + 2.4/5 GHz hopping — 2.4 RX drops, 5 GHz goes intermittent. Steady RX + RSSI match
  the OOT `rtl8814au` driver (mud2g 7.1 vs 8.4/s, RSSI +0.2 dB), and the OOT driver runs
  deauth→hop→deauth on both bands clean — so it's a port dynamic-path (TX/hop state) bug, not the
  silicon. Fresh OOT usbmon captures for the re-port: `usb_dumps_new2/captures_rtl8814au/` (run 3 = good 2G TX).

### rtl8822bu (mainline) — prefer the DKMS variant
- ⏳ DIG watchdog unverified on HW (IGI was frozen → deaf/saturating) [RTL8822BU.md].
- Limit: EFUSE / TX-power calibration not implemented (mainline is the thinner port).

### rtl8822bu_dkms
- ⏳ Strong-AP saturation — DIG must back gain off; near APs (~−41 dBm) need tuning to reach 8–10 bcn/s [RTL8822BU_DKMS.md].
- ⏳ Cold-boot 2.4 GHz synth wedge (~20 % of boots) — `_heal_cold_synth` recovery added; confirm it holds across a soak [RTL8822BU_DKMS.md].
- ⏳ Matched-load RX capture % unconfirmed vs vendor ~84 % (needs a quiet ch1) [RTL8822BU_DKMS.md].
- ⚠️ Chip doc contradicts itself on the TX descriptor (unported vs byte-for-byte 251/251) — resolve [RTL8822BU_DKMS.md].
