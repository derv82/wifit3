# Wifit3 — Known Bugs & QoL

Open bugs and unverified claims only — what's broken or unconfirmed *now*. Fixed work lives in git
history, not here. Cross-cutting issues first, then per-card. Deeper per-chip detail is in each
`<CHIP>.md`; per-attack pass/fail per card is `../VERIFICATION.md`.

`⏳` = a claim lifted from a chip's `<CHIP>.md`, **unconfirmed on hardware** — a HW sweep should
confirm it or kill it (kill → delete the line here *and* in the chip doc). `⚠️` = a chip doc that
contradicts itself; resolve on HW.

---

## Cross-cutting (not card-specific)

### Hardware failures don't all surface in the UI — release blocker
Some failures reach only the dev-only `wifit3.log`, not the Textual UI. Still open:
- **Bring-up failures are swallowed** — drivers `except Exception: return False` on a failed
  `connect()`, so nothing reaches the user. Delete those swallows so the failure raises to a modal.
- **No informational toasts** — "copied", "deauth sent", and handshake/PMKID/WEP captures are
  silent; they should be non-blocking toasts.
- **ar9271 v1 unplug detection is delayed** (opt-in `WIFIT3_AR9271=v1` only) — its own RX loop has
  no `on_fatal`, so an unplug isn't caught until the next `set_channel` hop. The default
  `ar9271_v2` is sub-second. Fix: give v1's RX loop an `on_fatal` → `register_disconnect_callback`,
  or port it to the shared RxReaderThread.

### Foreign-warmed chip → degraded RX until replug — release blocker
A card the kernel driver warmed that can't force a cold boot in userland comes up with
silently-degraded RX after wifit3 takes it over without a replug (RTL8814AU: 5 beacons/20 s vs 106
after a replug). Cards that self-cold (AR9271, mt76x0u, mt76x2u) are fine; the ones that can't
(RT*/Realtek) must be physically replugged. Fix: after a successful Linux install, show a "Please
replug" modal that polls for the card to be unplugged, then falls back to the splash screen, instead
of auto-connecting.

### EAP/Enterprise 4-way is reported as crackable
An EAP (enterprise) 4-way is captured and emitted as "crackable," but its PMK comes from the EAP/MSK
exchange, not a passphrase — hashcat `-m 22000` can't touch it. Extend the crackability gate
(`handshake.py`) to withhold it and badge it "EAP/Enterprise" instead of reporting a capture.

### No manual "Stop PBC" button (Focus)
A timed-out WPS PBC attempt can hold the radio for the rest of the PBC window, blocking other
attacks. There's no way to stop it by hand, so a single slow AP monopolizes the radio.

### PMKID fails on a WPA3→WPA2 transition AP (PMF:Optional)
PMKID reports "M1 not found" while Data frames appear right when M1 should arrive — likely a more
complex AKM (SAE/transition) routes M1 somewhere we don't match, or we forge the assoc with the
wrong AKM. Not yet confirmable without logging: log the AKM we advertise in the forged assoc and how
we classify each inbound frame. First check: do we send AKM=PSK in the assoc for *all* cases?

---

## Per-card

Clean, no known bugs: **rt2500usb, rt3070, rt5372, rtl8821au (mainline)**.

### rtl8187 (8187L)
- ⏳ Injected deauth uses `duration=0` instead of the unicast-ACK NAV — minor TX nit [RTL8187L.md:296].
- Limit: always cold-inits (no warm reattach) — replug to recover.

### rt2800usb
- ⏳ RX-poll (shared RxReaderThread) unverified on HW [RT2800USB.md:7].
- ⏳ RX-AGC link tuner unverified on HW — weak/unstable RX suspected [RT2800USB.md:18].
- ⏳ Focus → first `set_channel` tune is flaky ("a re-tune fixed it") [RT2800USB.md:525].

### mt76x2u (AWUS036ACM)
- ⏳ 5 GHz inject unverified on HW (the CCK→OFDM rate fix is in) [MT76X2U.md].
- ⏳ TSSI gated off; periodic `tssi_compensate` is suspected of zeroing TX power [MT76X2U.md:194].
- ⏳ Endpoint stability across power cycles unknown; channel-switch wants ~2 s settle [MT76X2U.md:202-207].
- ⏳ RX-poll unverified on HW [MT76X2U.md:7].

### rtl8188eus (mainline) — prefer the DKMS variant
- ⏳ Intermittent RX collapse — bad windows hear the reference AP worse than further neighbours [RTL8188EUS.md:38].
- ⏳ Encryption mislabel (WPA2 shown as "WEP") — fix awaits HW reconfirm [RTL8188EUS.md:27].
- Limit: mainline RX tops ~77 % with collapses; the DKMS re-port is the better card.

### rtl8188eus_dkms
- ⏳ Live RX ~6.5 vs ~8.9 bcn/s vs kernel — RX-perf gap, cause unconfirmed [RTL8188EUS_DKMS.md:12].
- ⏳ EFUSE antenna/channel-plan hardcoded from the dev card — wrong on other 8188eus units [RTL8188EUS_DKMS.md:62].

### rtl8812au (mainline, opt-in `WIFIT3_RTL8812=mainline`)
- Mainline still wedges on multi-band hopping (rtw88 HW limit) with no UI feedback. The default
  (DKMS) driver is clean, so this bites only the opt-in legacy driver; the UI-feedback gap is the
  "Hardware failures" item above [RTL8812AU.md:65].

### rtl8821au_dkms
- ⏳ 5 GHz deauth/TX (ch149) unverified on HW — offline byte-exact only; 2.4 GHz TX is confirmed [RTL8821AU_DKMS.md:75].

### rtl8821cu_dkms
- ⏳ WEP + Stress soak not yet run (Scan/handshake/PMKID/WPS confirmed on 2.4 + 5 GHz).

### rtw88_8814au (mainline) — prefer the DKMS variant
- ⏳ Weak 2.4 GHz RX (2G AP −82 dBm vs 5G −54; 0.5–2 bcn/s vs ~10) — 2G AGC/gain suspect [RTL8814AU.md:74].
- ⏳ Fast-hop RX death — ~1 s of 0 frames/s at 0.25 s dwell; PLL relock eats the dwell [RTL8814AU.md:194].
- ⚠️ Chip doc contradicts itself on `spur_calibration` (skipped vs ported) and IQK — resolve on HW [RTL8814AU.md:203/318].

### rtl8822bu (mainline) — prefer the DKMS variant
- ⏳ DIG watchdog unverified on HW (IGI was frozen → deaf/saturating) [RTL8822BU.md:18].
- Limit: EFUSE / TX-power calibration not implemented (mainline is the thinner port).

### rtl8822bu_dkms
- ⏳ Strong-AP saturation — DIG must back gain off; near APs (~−41 dBm) need tuning to reach 8–10 bcn/s [RTL8822BU_DKMS.md:21].
- ⏳ Cold-boot 2.4 GHz synth wedge (~20 % of boots) — `_heal_cold_synth` recovery added; confirm it holds across a soak [RTL8822BU_DKMS.md:44].
- ⏳ Matched-load RX capture % unconfirmed vs vendor ~84 % (needs a quiet ch1) [RTL8822BU_DKMS.md:41].
- ⚠️ Chip doc contradicts itself on the TX descriptor (unported vs byte-for-byte 251/251) — resolve [RTL8822BU_DKMS.md:262 vs 225].
