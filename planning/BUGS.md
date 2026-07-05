# Wifit3 — Known Bugs & QoL

Single source of truth for "does Wifit3 have bugs, y/n." Cross-cutting issues first;
per-card status below. The gory per-chip detail lives in each `<CHIP>.md` — you shouldn't
have to open those, this is the index. Per-attack pass/fail per card is `../VERIFICATION.md`.

---

## Cross-cutting (not card-specific)

### Tag + suppress EAP/Enterprise handshakes — correctness, small
An EAP (enterprise) 4-way is captured and emitted as "crackable," but its PMK comes from
the EAP/MSK exchange, not a passphrase — hashcat `-m 22000` can't touch it. Extend the
crackability gate (`handshake.py`) so an EAP-negotiated 4-way is withheld + badged
"EAP/Enterprise" instead of reported as a capture.

### Hardware-failure UX — pre-alpha (release blocker)
Failures must surface *in the Textual UI*, not just dev-only `wifit3.log`. Three tiers:
1. **Fatal** (no libusb backend) — modal + trace; Copy + Quit. **Done-ish.**
2. **Functional** (card wedged → replug) — modal + opt-in trace; OK (→ splash auto-recovers
   on replug) or Quit. **Partial (`0f8495ac`, 2026-07-04):** mid-run *unplug* (device-gone) is
   now caught off-thread — RxReaderThread `on_fatal` → interface disconnect sink → Quit-only
   fatal modal, wired into all 21 RxReaderThread drivers. **Remaining:** the graceful OK→splash
   replug-recovery variant (today it's Quit-only); init-time swallows (delete the drivers'
   `except Exception: return False`); and the **ar9271 (mainline) unplug gap** below.

   - ⏳ **ar9271 (mainline) — no instant unplug detection.** It's the one supported card with its
     own RX loop (not the shared RxReaderThread), so it has no `on_fatal` hook. On unplug it falls
     back to the interface hopper-guard (the next `set_channel` tune raises device-gone → modal),
     so detection is delayed to the next hop, not sub-second. Fix: port ar9271 to the shared reader,
     or give its RX loop an equivalent `on_fatal` → `register_disconnect_callback`.
3. **Informational** (copied, deauth sent, handshake/PMKID/WEP) — non-blocking toasts. **Not started.**

### WPS PBC auto-invade can monopolize the radio on timeout (Focus) — mostly fixed
A timed-out PBC attempt kept retrying for the rest of the PBC window, blocking other attacks.
**Root-caused + fixed 2026-06-21:** the stall was a *slow* AP (5 GHz M6 ~4.6 s vs a 3 s
`msg_timeout`); raised to 5 s + a client-leaving deauth on PBC teardown. **Still open (minor):**
a manual **Stop PBC** button so a single timeout can't hold the radio.

### PMKID on WPA3→WPA2 transition (PMF:Optional) — "M1 not found" but Data frames seen
On a WPA3→2 transition AP (PMF:Optional), PMKID reports "M1 not found" while frames appear in
the Data sparkline right when M1 should arrive — suspect a more complex AKM (SAE/transition)
routes M1 somewhere we don't match, or we forge the assoc with the wrong AKM. **Needs logging
before it's confirmable:** log the AKM we advertise in the forged assoc + how we classify each
inbound frame, so it's provable at the UI without the agent. First check: confirm we send
AKM=PSK in the assoc for *all* cases.

### Warm-attach inherits a foreign driver's chip state — silent RX degradation (release blocker)
If another driver is installed it sets up the card before wifit3 does; `modprobe -r` unloads it but
the chip stays *warm* with that foreign config, and our warm-reattach (built for a Wifit3-left-warm
card) inherits it → connects but RX is silently degraded, non-deterministic (RTL8814AU: ~4 beacons/10s
vs ~30 after replug). **Fix:** thread `is_dirty` (`kernel_driver_bound`, read pre-detach) into each
driver's `connect()`; `if is_warm and is_dirty: raise BringUpError(replug)` else continue — cards that
reset cold on unbind read not-warm and pass. One-time replug/card; subsumes
`LINUX_REPLUG_AFTER_MODPROBE`; ~22-driver port.

---

## Per-card — HW-verify sweep

Each `⏳` is a CLAIM lifted from the card's `<CHIP>.md`, **unconfirmed on hardware**. The
sweep confirms or kills each: a killed one gets deleted here *and* its stale line removed
from the chip doc. `✅` = nothing open (already HW-confirmed). `🛑`/`⚠️` = read before using.

> **Active Mode** (HW-ACK forged MAC) landed on every card on 2026-06-21, each with a named
> test card in the commit (AWUS1900 / AWUS036ACS / Archer T3U Plus / …) — implemented +
> once-tested per card; re-confirm in the sweep. **rt2500usb + rtl8187 are NONE** (no active
> mode, by design).

### rtl8187 (8187L)
- ⏳ Injected deauth `duration=0` instead of the unicast-ACK NAV — minor TX-correctness nit
  [RTL8187L.md:296].
- Known limit: always cold-inits (no warm reattach) — replug to recover.

### rt2800usb
- ⏳ RX-poll moved to the shared RxReaderThread but **unverified on HW** (Scan ⚠️) [RT2800USB.md:7].
- ⏳ RX-AGC link tuner ported, **unverified on HW** — weak/unstable RX symptom [RT2800USB.md:18].
- ⏳ Focus → set_channel first-tune flakiness ("a re-tune fixed it") [RT2800USB.md:525].

### rt2500usb — ✅ clean (full attack matrix + soak HW-confirmed 2026-06-11)
### rt3070 — ✅ no open defects
### rt5372 — ✅ clean (byte-perfect, full attack matrix green, 30-min soak, warm reattach)

### mt76x2u (AWUS036ACM)
- ⏳ 5 GHz inject — CCK→OFDM rate fix landed source-side; **awaits 5 GHz HW test** [MT76X2U.md].
- ⏳ TSSI gated OFF (deviates from kernel); periodic `tssi_compensate` suspected of zeroing
  TX power [MT76X2U.md:194].
- ⏳ Endpoint stability across power cycles unknown; channel-switch wants ~2 s settle [MT76X2U.md:202-207].
- ⏳ RX-poll unverified on HW [MT76X2U.md:7].

### rtl8188eus (mainline) — prefer the DKMS variant
- ⏳ Intermittent RX collapse — bad windows hear the reference AP *worse* than further
  neighbours [RTL8188EUS.md:38].
- ⏳ Encryption mislabel (WPA2 shown as "WEP") fix awaits HW reconfirm [RTL8188EUS.md:27].
- Known limit: mainline RX tops ~77 % with collapses; the DKMS re-port is the better card.

### rtl8188eus_dkms
- ⏳ Live RX ~6.5 vs ~8.9 bcn/s vs kernel — RX-perf gap, cause unconfirmed [RTL8188EUS_DKMS.md:12].
- ⏳ EFUSE antenna/channel-plan hardcoded from the dev card — wrong on other 8188eus units [RTL8188EUS_DKMS.md:62].

### rtl8812au — RESOLVED at the product level (`26c93d7`, Jun 5)
- ✅ The multi-band hop-death is **resolved**: the DKMS port is now the **default** for
  `0bda:8812` (240 s+ hopping, zero wedge). The mainline driver still wedges (rtw88 HW limit)
  but users only reach it via `WIFIT3_RTL8812=mainline`, so the 🛑 applies to the opt-in
  legacy driver only. Use 8812 on its default.
- ⏳ No-UI-feedback-on-wedge applies only if someone opts into mainline [RTL8812AU.md:65];
  the cross-cutting "Hardware-failure UX" item is the real fix.

### rtl8821au (mainline) — ✅ clean (M0-M6 + RX-poll/ToDS HW-confirmed)
### rtl8821au_dkms
- ⏳ 5 GHz deauth/TX (ch149) not HW-verified — offline byte-exact only [RTL8821AU_DKMS.md:75].
  (2.4 GHz TX HW-confirmed.)

### rtl8821cu_dkms
- ✅ Landed: 2.4 + 5 GHz Scan/handshake/PMKID/WPS HW-confirmed 2026-06-24 (fixed-ch1 ~6.5 bcn/s,
  kernel parity). ZeroCD resolved — eject the CD-ROM first (Windows only) [VERIFICATION.md].
- ⏳ WEP + Stress soak not yet run.

### rtw88_8814au (mainline 8814au) — prefer the DKMS variant
- ⏳ Weak 2.4 GHz RX (2G AP −82 dBm vs 5G −54; 0.5–2 bcn/s vs ~10) — 2G AGC/gain suspect [RTL8814AU.md:74].
- ⏳ Fast-hop RX death — ~1 s of 0 frames/s at 0.25 s dwell; PLL relock eats the dwell [RTL8814AU.md:194].
- ⚠️ Doc self-contradicts on `spur_calibration` (skipped vs ported) and IQK — resolve on HW [RTL8814AU.md:203/318].

### rtl8814au_dkms
- ⏳ Intermittent 2.4 GHz dropouts under sustained hopping — a 60 s bucket dropped **all** 2.4 GHz
  APs (Scan/Stress ⚠️); the signal-strength fix didn't make the 2.4 GHz path solid [RTL8814AU_DKMS.md:281].

### rtl8822bu (mainline) — prefer the DKMS variant
- ⏳ DIG watchdog ported, pending HW A/B (IGI was frozen → deaf/saturating) [RTL8822BU.md:18].
- Known limit: EFUSE / TX-power calibration not implemented; mainline is the thinner port.

### rtl8822bu_dkms
- ⏳ Strong-AP saturation — DIG must back gain off; near APs (~−41 dBm) need tuning to reach
  8–10 bcn/s [RTL8822BU_DKMS.md:21].
- ⏳ Cold-boot 2.4 GHz synth wedge (~20 % of boots) — `_heal_cold_synth` recovery added
  (80/80); confirm it holds across your soak [RTL8822BU_DKMS.md:44].
- ⏳ Matched-load RX capture % unconfirmed vs vendor ~84 % (needs a quiet ch1) [RTL8822BU_DKMS.md:41].
- ⚠️ Doc self-contradicts on the TX descriptor (⛔ unported vs ✅ byte-for-byte 251/251) — resolve [RTL8822BU_DKMS.md:262 vs 225].
