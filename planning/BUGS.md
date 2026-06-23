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
   on replug) or Quit. **Not started.** Init-time is the easy half (delete the drivers'
   `except Exception: return False` swallows); the off-thread RX-reader wedge is the hard half.
3. **Informational** (copied, deauth sent, handshake/PMKID/WEP) — non-blocking toasts. **Not started.**

### WPS PBC auto-invade can monopolize the radio on timeout (Focus) — mostly fixed
A timed-out PBC attempt kept retrying for the rest of the PBC window, blocking other attacks.
**Root-caused + fixed 2026-06-21:** the stall was a *slow* AP (5 GHz M6 ~4.6 s vs a 3 s
`msg_timeout`); raised to 5 s + a client-leaving deauth on PBC teardown. **Still open (minor):**
a manual **Stop PBC** button so a single timeout can't hold the radio.

### Channel-Filter modal clips OK/Cancel in small terminals
`ui/screens/channel_filter.py` — the channel list eats the height and hides the buttons.
Shrink/scroll the list when tight, and/or a taller min-height.

### 5 GHz drivers under-list DFS channels — deferred (DFS ≈ empty air)
Every 5 GHz driver except `rtl8814au_dkms` advertises the same 9 non-DFS channels; their
captures show `iw set channel 52/100/144` returning 0, i.e. the cards *do* tune DFS. Deferred
on purpose — DFS is radar-shared, usually empty, and omitting it means faster hops. To add
later: per-driver, confirm the capture tuned it + byte-verify `set_channel`, never a blind
list edit (the truncated drivers likely never exercised the DFS tune path).

---

## Per-card — HW-verify sweep

Each `⏳` is a CLAIM lifted from the card's `<CHIP>.md`, **unconfirmed on hardware**. The
sweep confirms or kills each: a killed one gets deleted here *and* its stale line removed
from the chip doc. `✅` = nothing open (already HW-confirmed). `🛑`/`⚠️` = read before using.

### ar9271 — ⚠️ suspect (the 2-channel-bug card)
- ⏳ Per-channel calibration not ported → RX sensitivity unverified across channels; doc
  flags "PORT NOW SUSPECT, audit the whole port" [AR9271.md:28]. **Test every channel.**
- ⏳ Cleanroom FW only partially promiscuous — passes other-BSSID beacons but drops
  downlink-unicast (RA = other client); may miss frames [AR9271.md:76].

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

### mt76x0u
- ⏳ 5 GHz weak RX — LNA-gain fix landed (3.5 → 9.5 bcn/s on ch157); **full 5 GHz attack
  suite awaits your HW test** [MT76X0U.md:20,698].
- ⏳ RX-poll RxReaderThread unverified on HW [MT76X0U.md:7].

### mt76x2u (AWUS036ACM)
- ⏳ 5 GHz inject — CCK→OFDM rate fix landed source-side; **awaits 5 GHz HW test** [MT76X2U.md].
- ⏳ TSSI gated OFF (deviates from kernel); periodic `tssi_compensate` suspected of zeroing
  TX power [MT76X2U.md:194].
- ⏳ Endpoint stability across power cycles unknown; channel-switch wants ~2 s settle [MT76X2U.md:202-207].
- ⏳ RX-poll unverified on HW [MT76X2U.md:7].

### mt7921au (PAU0F / AXML)
- ⏳ **5 GHz inject FAILS on HW** — frames don't land (5 GHz RX is fine). TXD ruled
  byte-correct; suspect 5 GHz TX-power not programmed on the sniffer channel-switch [MT7921AU.md:30].
- ⏳ Chatty WPS — ~120 EAPOLs vs ~20 elsewhere (no STA_REC, so no TX-status tracking) [MT7921AU.md:151].

### rtl8188eus (mainline) — prefer the DKMS variant
- ⏳ Intermittent RX collapse — bad windows hear the reference AP *worse* than further
  neighbours [RTL8188EUS.md:38].
- ⏳ Encryption mislabel (WPA2 shown as "WEP") fix awaits HW reconfirm [RTL8188EUS.md:27].
- Known limit: mainline RX tops ~77 % with collapses; the DKMS re-port is the better card.

### rtl8188eus_dkms
- ⏳ Live RX ~6.5 vs ~8.9 bcn/s vs kernel — RX-perf gap, cause unconfirmed [RTL8188EUS_DKMS.md:12].
- ⏳ EFUSE antenna/channel-plan hardcoded from the dev card — wrong on other 8188eus units [RTL8188EUS_DKMS.md:62].

### rtl8812au (mainline) — 🛑 DO NOT USE for multi-band scanning
- 🛑 RX wedges **dead** under sustained 2.4 + 5 GHz hopping (RF synth loses lock; replug to
  recover). Doc calls it an rtw88-inherited HW limit; mitigation only delays it [RTL8812AU.md:4].
- ⏳ No UI feedback when it wedges — targets just fade to nothing [RTL8812AU.md:65].

### rtl8812au_dkms (AWUS036ACH)
- ⏳ 5 GHz RX + TX off the antenna **untested** — tune is byte-verified, live RX/TX await your
  HW test (`rx_diag --channel 36`) [RTL8812AU_DKMS.md:69]. (2.4 GHz attack suite HW-confirmed.)

### rtl8821au (mainline) — ✅ clean (M0-M6 + RX-poll/ToDS HW-confirmed)
### rtl8821au_dkms
- ⏳ 5 GHz deauth/TX (ch149) not HW-verified — offline byte-exact only [RTL8821AU_DKMS.md:75].
  (2.4 GHz TX HW-confirmed.)

### rtl8821cu_dkms — 🚧 port in progress (another session) — not part of this sweep
Blocked at discovery: ZeroCD/USB mode-switch unsolved (enumerates as CD-ROM), offline-replay
-only so far, no HW test yet [RTL8821CU_DKMS.md:37].

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

---

## Deliberately excluded from the list above (port-internal, NOT user-facing bugs)
Kept out so this stays "what might not work for a user," not "what a porter hasn't finished":
by-design scope (20 MHz-only, non-DFS channels); port-faithfulness coverage notes (un-walked
code paths, USB3 branches untested on USB2-only cards, monitor-dead-code watchdog members);
deferred TX-power / IQK calibration on cards where inject already works (a quality ceiling, not
a break). All of it lives in the `<CHIP>.md` docs if ever needed.
