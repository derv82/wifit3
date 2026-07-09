# Wifit3 — Known Bugs & QoL

Open bugs and unverified claims only — what's broken or unconfirmed *now*. Fixed work lives in git
history, not here. Cross-cutting issues first, then per-card. Deeper per-chip detail is in each
`<CHIP>.md`; per-attack pass/fail per card is `../VERIFICATION.md`.

`⏳` = a claim lifted from a chip's `<CHIP>.md`, **unconfirmed on hardware** — a HW sweep should
confirm it or kill it (kill → delete the line here *and* in the chip doc). `⚠️` = a chip doc that
contradicts itself; resolve on HW.

---

## Per-card

### rtl8821au_dkms
- ⏳ 5 GHz deauth/TX (ch149) unverified on HW — offline byte-exact only; 2.4 GHz TX is confirmed [RTL8821AU_DKMS.md].

### rtl8814au_dkms
- Wedges under TX + 2.4/5 GHz hopping — 2.4 RX drops, 5 GHz goes intermittent. Steady RX + RSSI match
  the OOT `rtl8814au` driver (mud2g 7.1 vs 8.4/s, RSSI +0.2 dB), and the OOT driver runs
  deauth→hop→deauth on both bands clean — so it's a port dynamic-path (TX/hop state) bug, not the
  silicon. Fresh OOT usbmon captures for the re-port: `usb_dumps_new2/captures_rtl8814au/` (run 3 = good 2G TX).
