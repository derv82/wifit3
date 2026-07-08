# Wifit3 — Known Bugs & QoL

Open bugs and unverified claims only — what's broken or unconfirmed *now*. Fixed work lives in git
history, not here. Cross-cutting issues first, then per-card. Deeper per-chip detail is in each
`<CHIP>.md`; per-attack pass/fail per card is `../VERIFICATION.md`.

`⏳` = a claim lifted from a chip's `<CHIP>.md`, **unconfirmed on hardware** — a HW sweep should
confirm it or kill it (kill → delete the line here *and* in the chip doc). `⚠️` = a chip doc that
contradicts itself; resolve on HW.

---

## Per-card

### rtl8187 (8187L)
- ⏳ Injected deauth uses `duration=0` instead of the unicast-ACK NAV — minor TX nit [RTL8187L.md].
- Limit: always cold-inits (no warm reattach) — replug to recover.

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

### rtl8821au_dkms
- ⏳ 5 GHz deauth/TX (ch149) unverified on HW — offline byte-exact only; 2.4 GHz TX is confirmed [RTL8821AU_DKMS.md].

### rtl8821cu_dkms
- ⏳ WEP + Stress soak not yet run (Scan/handshake/PMKID/WPS confirmed on 2.4 + 5 GHz).
- No bring-up progress — `connect()` sits at 0% then jumps to 100%, so the UI flashes straight to
  Scanner with no feedback. Add ~5 `progress_cb` callbacks across the bring-up phases.

### rtl8814au_dkms
- Wedges under TX + 2.4/5 GHz hopping — 2.4 RX drops, 5 GHz goes intermittent. Steady RX + RSSI match
  the OOT `rtl8814au` driver (mud2g 7.1 vs 8.4/s, RSSI +0.2 dB), and the OOT driver runs
  deauth→hop→deauth on both bands clean — so it's a port dynamic-path (TX/hop state) bug, not the
  silicon. Fresh OOT usbmon captures for the re-port: `usb_dumps_new2/captures_rtl8814au/` (run 3 = good 2G TX).

### rtl8822bu_dkms
- ⏳ Matched-load RX capture %: 78 % live on a busy ch1 vs the vendor's ~84 % busy-ch1 reference
  (within contention, 2026-07-08) — a clean quiet-ch1 number is still outstanding [RTL8822BU_DKMS.md].
