# Wifit3 — Hardware Verification Matrix

Per-driver × per-attack verification status. This is the **dashboard**; the deep
evidence (commits, `[HW]`/`[WIRE]` citations, exact symptoms) lives in each
chip's `src/wifit3/chips/<chip>/<CHIP>.md`.

Status here is **deliberately conservative**: a cell is only ✅ when a chip doc
records a real hardware run. **A non-✅ cell means "not yet confirmed," NOT
"broken"** — most ⬜ cells simply haven't been exercised on that card yet. This
table was seeded 2026-05-31 from the chip docs + `NEXT-STEPS.md` as a starting
point; re-run each cell and stamp the real date as it's confirmed.

## Legend

| Mark | Meaning |
|:--:|---|
| ✅ | **Verified** on hardware (a `<CHIP>.md` records the run) |
| ⚠️ | **Caveat** — works, but with a documented limitation or only partially |
| ❌ | **Broken** — tried on hardware, does not work |
| ⬜ | **Untested** — no hardware run recorded yet (this is *not* a failure) |

Column meanings: **Scan** = beacon / AP / client discovery (monitor RX).
**Deauth** = on-air frame injection. **Handshake** = 4-way (M1–M4) recapture via
deauth. **PMKID** = PMKID harvest. **WEP** / **WPS** collapse their sub-attacks
(ARP-replay / frag / chopchop / crack; PIN / PBC / pixie) — broken out per card
below. **Stress** = endurance / thermal (no standard soak protocol defined yet;
tracked pass/fail).

**WEP-cell convention:** the WEP cell is ✅ when **ARP-replay + ChopChop** both
pass. **Fragmentation is tracked separately** as a cross-card known issue: cleanly
verified on the RTL8821AU (the card with the `en_hwseq=0` software-sequence TX
path), failing on the rtw88 cards + AR9271, and *reported working but slow* on the
RT3572 — a tension with the sw-seq theory that's still open (see
`engine/attacks/wep/README.md` § "Known issue"). So a ✅ WEP cell here does *not*
claim frag works — check the per-card detail / the WEP README for frag status.

## Matrix

| Chipset | Scan | Deauth | Handshake | PMKID | WEP | WPS | Stress |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| AR9271 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| RTL8187L | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⬜ |
| RTL8188EUS | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⬜ |
| RTL8821AU | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| RTL8812AU | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ |
| RTL8822BU | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| RTL8814AU | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| MT7612U | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| MT7610U | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| RT5372 (PAU05) | ⚠️ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⬜ |
| RT5572 (PAU09) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| RT2500USB | ⚠️ | ✅ | ❌ | ✅ | ⚠️ | ⚠️ | ❌ |

**RT3572 is not in this matrix** — the only test unit (ALFA AWUS051NH v2) turned
out to be **counterfeit with a blank EFUSE** (no factory RF calibration), so it
can't validate the chipset. Moved to "Unsupported — pending genuine hardware"
below; its driver is shared with RT5372/RT5572 (both green/partial here), so the
`chips/rt2800usb/` driver itself stays supported.

### README asterisk rule

A chipset carries a `*` in the README's supported-hardware table when it has a
**❌, or a ⚠️ that is a genuine hardware/driver limitation of that card** (RF
hop-death, a hard-MAC WPS failure, partial-direction capture / RF-death). A ⚠️ that
only reflects **incomplete attack-suite coverage** (the attack works where it
was run, just not fully exercised on that card) does **not** asterisk — the card
is fine, the testing is simply unfinished. Neither do known **shared / cross-card
issues tracked centrally** (the fragmentation sw-seq gap, the 2.4 GHz-RX
investigation, the WinUSB warm-reattach replug) — those affect the family, not
one card, so they don't single a card out.

Asterisked today: **RTL8187L, RTL8812AU, RT2500USB**. (RT2800USB no longer
asterisked — RT3572, its only flagged unit, was a counterfeit and is demoted; the
RT5372/RT5572 cards carry no known limitation.)

Note for README readers: the *absence* of an asterisk means "no known problem,"
**not** "every attack verified" — check this matrix for the full per-attack
state.

---

## Per-chipset detail

Bring-up (Scan + Deauth) is ✅ across every supported card per `NEXT-STEPS.md`
("fully functional: cold + warm bring-up, channel hop, inject + sniff, TUI"); the
notes below cover the attack columns and any caveats.

### AR9271 — ALFA AWUS036NHA (2.4 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-25 | Full M1–M4, warm + cold. The QoS DMA-pad FCS bug (all M1–M4 are QoS) is fixed. |
| PMKID | ✅ | 2026-05-25 | First-try on cold boot after the DATA-EP credit-seed fix; verified vs real APs. |
| WEP | ⚠️ | 2026-05-31 | Replay ✅; ChopChop ✅; **Fragmentation ✗** — "seed wouldn't relay", cancelled after 60 s. Same failure as 8812au/8822bu (a different family from this Atheros card) — see the cross-family frag issue in `engine/attacks/wep/README.md`. |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC ✅. |
| Stress | ⬜ | — | Untested (nothing notable in brief use). A 1,094-pkt calibration marathon ran clean, but no attack-soak endurance test. |

→ `chips/ar9271/AR9271.md`

### RTL8187L — ALFA AWUS036H / various (2.4 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Works (classic AWUS036H). |
| Handshake | ✅ | 2026-05-31 | Full M1–M4 captured. |
| PMKID | ✅ | 2026-05-31 | Captured passively + active extract. |
| WEP | ✅ | 2026-05-31 | ARP replay ✅ and ChopChop ✅. (Fragmentation tracked separately — no `SUPPORTS_SW_SEQ`, the known sw-seq gap.) |
| WPS | ❌ | 2026-05-31 | **PBC timed out; PIN got NACKs, no crack.** WPS passes on all 7 firmware-based cards but fails/struggles on the two old **hard-MAC / no-firmware** parts (this ❌ + RT2500USB ⚠️) — likely a TX-timing / no-hardware-ACK sensitivity in the longer WPS EAP exchange. See WPS README § "Hard-MAC WPS gap". |
| Stress | ⬜ | — | Nothing adverse in brief use; no dedicated 1-hour soak yet. |

→ `chips/rtl8187/RTL8187L.md`

### RTL8188EUS — TP-Link TL-WN722N v2/v3 (2.4 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Works. Beacon rate looked low — ~6–7/s on a CH1 AP, ~1–3/s on the CH6 dd-wrt box (healthy ~10/s). Different APs/channels, so weaker evidence than the rtw88 cards, but adds to the weak-2.4 GHz-RX question. NB: this is rtl8xxxu, **not** rtw88 — see NEXT-STEPS. |
| Handshake | ✅ | 2026-05-19 | Passive 4-way captured end-to-end (M1 + M3 + M4 + PMKID-in-M1) from a real client reconnect. |
| PMKID | ✅ | 2026-05-19 | Active harvest via AUTH+ASSOC injection — instant. |
| WEP | ⚠️ | 2026-05-31 | ARP replay ✅. **ChopChop ✗** — stalled at 9/32 bytes; almost certainly the weak RX on the CH6 dd-wrt box (~2–3 beacons/s), since ChopChop works on 4 other cards (not a logic bug). Frag skipped (known sw-seq gap). |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC ✅. |
| Stress | ⬜ | — | Not run. |

→ `chips/rtl8188eus/RTL8188EUS.md`

### RTL8821AU — ALFA AWUS036ACS (2.4 / 5 GHz) — primary dev card

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Works; 2.4 GHz beacon rate ~7/s for a router ~2 ft away — a touch low (healthy ~10/s). Second card to show low-ish 2.4 GHz RX (8814au was worse), but this one scans fine. |
| Handshake | ✅ | 2026-05-31 | Full M1–M4 (all four captured) after the RX-poll reader-thread + ToDS-filter (`0xf410400f`) fixes. |
| PMKID | ✅ | 2026-05-31 | Passive capture + active extract. |
| WEP | ✅ | 2026-05-31 | Full suite: Replay ✅, ChopChop ✅, **Fragmentation ✅**. This is the one card with the `en_hwseq=0` sw-seq TX path — which is exactly why frag works here and nowhere else (WEP README § "Known issue"). |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC ✅. (PixieWPS is still a deferred project-wide feature, not a per-card gap; the multi-router lock matrix is still thin.) |
| Stress | ⬜ | — | Nothing adverse in normal use, but no dedicated 1-hour soak run yet. |

→ `chips/rtl8821au/RTL8821AU.md`, `engine/attacks/wep/README.md`, `engine/attacks/wps/README.md`

### RTL8812AU — ALFA AWUS036ACH (2.4 / 5 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-05-31 | Single-channel scan (via Channel Filter) is fine, but **channel hopping wedges RX** — the RF hop-death (see Stress). When it wedges there's **no UI feedback**: targets fade to dark, then the list empties, no message (the driver does log one warning). |
| Deauth | ✅ | 2026-05-31 | Deauthed multiple clients. |
| Handshake | ✅ | 2026-05-31 | Captured M1+M2 and M2+M3 (crackable pairs) after the ToDS-filter + RX-DMA-agg fixes. |
| PMKID | ✅ | 2026-05-31 | Both paths: passive capture and active extract. |
| WEP | ⚠️ | 2026-05-31 | Replay ✅; ChopChop ✅ (<2 min); **Fragmentation ✗** — couldn't forge a valid ARP after ~2 min of rounds. Frag also fails on RTL8822BU but works on RTL8821AU → likely a shared frag-TX sw-seq gap; see `chips/rtl8812au/RTL8812AU.md`. |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC ✅. |
| Stress | ⚠️ | 2026-05-31 | **RF-synth hop-death** (rtw88-inherited HW limit, in-tree driver has it too): single channel survives 30 min+, but sustained dual-band 0.25 s hopping wedges RX after seconds-to-minutes. Mitigation (`dynamic.py`: DIG + pwr-track + decoupled LCK) delays it ~2–4× but doesn't eliminate it; no userland recovery — replug (and no UI feedback when it happens — see Scan). |

→ `chips/rtl8812au/RTL8812AU.md`

### RTL8822BU — TP-Link Archer T3U Plus v1 (2.4 / 5 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-05-31 | 2.4 GHz RX looks weak: a 5 GHz AP read −50 dBm but a 2.4 GHz AP read −81 dBm at the same spot (~31 dB gap — backwards from physics; 2.4 GHz should carry *better*). Third rtw88 card to show this (with 8814au, 8821au) → cross-rtw88 2.4 GHz RX item in NEXT-STEPS. Could be real 2G sensitivity or a 2G RSSI-calc offset; the Linux A/B test disambiguates. Scanning still works. |
| Handshake | ✅ | 2026-05-31 | Captured via deauth (M2+M3 / full M1–M4; earlier 3/3-completion run 2026-05-25). |
| PMKID | ✅ | 2026-05-31 | Both paths: active extract (the "PMKID" button) and passive capture. |
| WEP | ⚠️ | 2026-05-31 | Replay ✅ and ChopChop ✅ (re-confirmed). **Fragmentation ✗** — the cross-card sw-seq gap (this card lacks `SUPPORTS_SW_SEQ`; see WEP README § "Known issue"), not a seed/oracle bug. Frag skipped from here on by decision. |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC auto-invade ✅, against a WPS test AP. |
| Stress | ⬜ | 2026-05-31 | No dedicated 1-hour soak yet (re-test: "nothing to report"). *Separately*, an earlier session hit a **warm-reattach wedge on restart** (bulk-IN wedged → replug) — a general Windows/WinUSB restart limit, not a soak result, tracked in the chip doc + NEXT-STEPS § Small bugs/QoL. That's why this cell was orange before; moved here since it isn't a stress result. |

→ `chips/rtl8822bu/RTL8822BU.md`

### RTL8814AU — ALFA AWUS1900 (2.4 / 5 GHz, 4T4R)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-05-31 | **2.4 GHz RX is weak/miscalibrated**: at one spot a 5 GHz AP read −54 dBm but a 2.4 GHz AP read −82 dBm, and 2.4 GHz beacon rate was 0.5–2/s vs ~10/s on 5 GHz. 5 GHz scanning is healthy → a 2G-specific RX path / AGC / gain-cal gap (see chip doc). |
| Deauth | ✅ | 2026-05-31 | Deauthed clients. |
| Handshake | ✅ | 2026-05-31 | M2+M3 captured on 2.4 GHz (earlier deauth→EAPOL re-capture confirmed 2026-05-26). |
| PMKID | ✅ | 2026-05-31 | Passive capture + active extract — but flaky on 2.4 GHz (~20 attempts to land an M1; lots of M2/M3/M4), consistent with the weak 2.4 GHz RX. |
| WEP | ⚠️ | 2026-05-31 | Replay ✅; ChopChop ✅; **Fragmentation ✗** (same cross-family frag bug — see `engine/attacks/wep/README.md`). All WEP tested on 2.4 GHz only (no 5 GHz WEP AP available), where this card's RX is weak — but ChopChop heard its relays, so frag's failure is still a valid data point. |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC ✅. |
| Stress | ⬜ | — | Not run. Note: TX runs at BB/AGC baseline power (not power/IQ-calibrated) → weaker at distance; fine close-range. |

→ `chips/rtw88_8814au/RTL8814AU.md`

### MT7612U — ALFA AWUS036ACM (2.4 / 5 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Works. |
| Deauth | ✅ | 2026-05-31 | Deauthed clients. |
| Handshake | ✅ | 2026-05-31 | Full M1+M2+M3+M4 captured (handshake auto-saves; the de-pad fix restored a crackable handshake). |
| PMKID | ✅ | 2026-05-31 | Captured passively + harvested (active extract). |
| WEP | ✅ | 2026-05-31 | ARP replay ✅ and ChopChop ✅. (Fragmentation tracked separately — the known sw-seq gap.) |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ (to M4) and PBC ✅. |
| Stress | ⬜ | — | Nothing adverse in brief use; no dedicated 1-hour soak yet. |

→ `chips/mt76x2u/MT76X2U.md`

### MT7610U — ALFA AWUS036ACHM (2.4 / 5 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-05-31 | **Focus-entry tune glitch**: entering Focus on a CH1 AP showed 0 beacons/s; Focus→Scanner→Focus on the same target then gave 8–9/s. Same symptom seen on RT3572 — so it's the **shared Focus→set_channel path**, not chip-specific (confirmed cross-family now). See NEXT-STEPS. |
| Handshake | ✅ | 2026-05-31 | Captured M1+M2 (crackable pair). |
| PMKID | ✅ | 2026-05-31 | Captured passively + active extract. |
| WEP | ✅ | 2026-05-31 | ARP replay ✅ and ChopChop ✅. (Fragmentation tracked separately — fails here, the known sw-seq gap; see WEP README § "Known issue".) |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC ✅. |
| Stress | ⬜ | — | Not run. |

→ `chips/mt76x0u/MT76X0U.md`

### RT2800USB family — driver `chips/rt2800usb/` (three silicons)

M1–M4 (RX / scan) are WIRE-verified on all three silicons. M5 (TX inject, deauth
→ EAPOL re-capture) was verified on the RT5372/PAU05.

**RT5372 — Panda PAU05 (2.4 GHz, 1T1R)** — family reference chip.

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-06-01 | **Weak/unstable RX — range is poor.** Beacons/s wander wildly: ~1–3/s for the first few seconds, drifting up toward 7–8/s, then sagging back to 4–5/s, with periodic ~zero gaps every ~10–15 s. Cross-AP pattern is the tell: a neighbour's AP holds a steady 6–7/s while the user's own router 15 ft away (strong signal) only manages ~3/s, and the WEP/WPS test routers ~2–3/s. That a *distant* AP comes in stronger than a *near* one points at a tuning/AGC offset (detuned toward one edge of CH1?), not pure distance. Adds to the cross-card weak-2.4 GHz-RX question. Next step: a Kali beacons/s A/B per channel for this card (see Scan-health idea in NEXT-STEPS). |
| Deauth | ✅ | — | M5 TX verification: deauth → EAPOL re-capture (family reference). |
| Handshake | ✅ | — | M5 TX verification: deauth → EAPOL re-capture. |
| PMKID | ✅ | 2026-06-01 | Captured passively + extracted manually. |
| WEP | ✅ | 2026-06-01 | ARP replay ✅ and ChopChop ✅. (Fragmentation ignored for now — the separately-tracked sw-seq gap.) |
| WPS | ⚠️ | 2026-06-01 | PIN brute ✅. **PBC ✗** — `assoc failed (Assoc rejected (status 12)); running EAPOL anyway`, then timed out. Mixed → ⚠️ (same PIN-ok / PBC-timeout shape as RT2500USB + RT3572). This card is firmware-based, so it's *not* the hard-MAC WPS gap; the PBC timeout plausibly traces to the weak RX above (status-12 = assoc denied, and a starved RX can miss the PBC walk-window), so it isn't asterisked separately — re-test once RX tuning is sorted. |
| Stress | ⬜ | — | Not run. |

**RT5572 — Panda PAU09 N600 (2.4 / 5 GHz, 2T2R)** — full-green, and the best-behaved
Ralink: snappy, excellent beacon rate across the whole spectrum, and **2.4 GHz and
5 GHz read the same power** (a clean counter-example to the rtw88 cards' weak 2.4 GHz RX).

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Snappy; excellent beacons/s across the band; NETGEAR2G (2.4) and NETGEAR (5) at the same power → balanced RX. |
| Deauth | ✅ | 2026-05-31 | Deauthed clients. |
| Handshake | ✅ | 2026-05-31 | Full M1+M2+M3+M4 captured. |
| PMKID | ✅ | 2026-05-31 | Captured passively + harvested. |
| WEP | ✅ | 2026-05-31 | ARP replay ✅ and ChopChop ✅. **Resolves the old "ARP replay was once dead here (zeroed-IV injection)" concern — ARP replay works.** (Fragmentation = the separately-tracked sw-seq gap.) |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC ✅. |
| Stress | ⬜ | — | Nothing adverse in brief use; no dedicated 1-hour soak yet. |

RT3572 (AWUS051NH v2) is covered in "Unsupported — pending genuine hardware" below.

### RT2500USB — Buffalo Nintendo Wi-Fi USB Connector / RT2570 (2.4 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-05-31 | Channel/RX inconsistency: a CH1 AP (NETGEAR2G) gave ~10 beacons/s, but another CH1 AP (TestAP1) gave **0 beacons/s consistently** at the same time. Same channel, so not a pure tune bug — points at weak RX for that AP or an RX address-filter issue. |
| Deauth | ✅ | 2026-05-31 | Deauthed clients. |
| Handshake | ❌ | 2026-05-31 | **Only M1+M3 (FromDS) captured — no M2/M4 (ToDS), so no crackable pair.** NOTE: *not* a ToDS filter gap — the session log shows `to_ds=True` data frames arriving (client→AP IS delivered), so the monitor filter is open. More likely the weak/unstable RX (see Scan) + the RF dying ~1 min in (see Stress) meant the client's M2/M4 weren't heard before the pipe wedged. Re-test once the RF-death/weak-RX is sorted. |
| PMKID | ✅ | 2026-05-31 | Captured passively + active extract. |
| WEP | ⚠️ | 2026-05-31 | ARP replay works but **very slowly (~1–3 IVs/s)**; **ChopChop ✗** — stuck at the 40 B cipher (~32 bytes still to recover). Both consistent with the weak/unstable RX this session. |
| WPS | ⚠️ | 2026-05-31 | **PIN ✅** — engine works: sent guesses, got valid first-half-wrong NACKs ×3 (no full crack this session, but the on-air exchange is sound). **PBC ✗** — timed out, plausibly the weak CH1 RX (see Scan). Mixed → ⚠️ (same pattern as RT3572). |
| Stress | ❌ | 2026-05-31 | **RF died after ~1 minute** — no beacons from *any* AP, with bulk-IN `[Errno 32] Pipe error` and `set_channel(N) failed: Pipe error` in the log. The USB pipe wedged under sustained load. Far short of the 1-hour soak bar; needs investigation (see chip doc). |

→ `chips/rt2500usb/RT2500USB.md`

---

## Unsupported — pending genuine hardware

### RT3572 — ALFA AWUS051NH v2 (COUNTERFEIT unit, blank EFUSE)

**Demoted from supported 2026-05-31.** The only test unit (bought 2015) is a
**counterfeit with a blank EFUSE** — no factory RF calibration. The kernel/our
driver run their normal init over uncalibrated silicon, which forces 1T1R, rails
the rx-filter cal loop to a non-physical value, and leaves TX power at the low
fallback → weak, unstable TX/RX. **The `chips/rt2800usb/` driver is verified
byte-for-byte faithful** (and is green/partial on its RT5372 + RT5572 siblings) —
this is the *unit*, not the port. We have no genuine burned RT3572 to validate
against, so the chipset can't be marked supported. Re-test and re-promote if a
real one is acquired.

Last results on the counterfeit unit (kept for reference — all ⚠️ trace to the
blank EFUSE, not driver bugs):

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Works but weak — ~8 beacons/s from an AP a few feet away (~10/s healthy). |
| Deauth | ⚠️ | 2026-05-31 | Deauthed *something*, but too weak to knock a phone right next to the radio off. |
| Handshake | ⚠️ | 2026-05-31 | Partial (M1+M4) capture, weak; low beacon count points at uncalibrated RX. |
| PMKID | ⚠️ | 2026-05-31 | Passive capture works; the active "PMKID" button does not (too weak to elicit M1). |
| WEP | ⚠️ | 2026-05-31 | ARP replay ✅; Fragmentation ✅ (slow, many failed rounds); ChopChop ✗ (stalled at 22/32 bytes). FakeAuth bounced Associated↔Idle. |
| WPS | ⚠️ | 2026-05-31 | PBC timed out; PIN got 2 NACKs + 1 no-response (talking, unreliable). |
| Stress | ⚠️ | 2026-05-31 | Warm boot fine. Focus-entry tune glitch observed (0 beacons until re-enter) — that one's a real shared bug, NOT the EFUSE (see NEXT-STEPS). |

→ `chips/rt2800usb/RT2800USB.md` § "RT3572 unburned-EFUSE behaviour"

---

## Stress / endurance

**Protocol:** every attack still works as expected after channel-hopping all
channels for 1 hour (driven on hardware by the longrun test script). A cell is
✅ when the post-soak attacks pass, ⚠️/❌ when a problem surfaces during or after
the hour.

Observed so far — both are robustness limits a soak run would expose, not clean
passes:
- **RTL8812AU ⚠️** — RF hop-death under sustained dual-band hopping (RF synth
  loses lock; mitigated ~2–4×, no userland recovery).
- **RTL8822BU ⚠️** — warm reattach on restart wedges the bulk-IN pipe → replug
  required (likely a general WinUSB warm-reattach limit, not 8822bu-specific).

## What "Fully supported" will mean

A chipset is **fully supported** when every attack column is ✅ *and* it clears
the (future) Stress soak. Today the wall is: bring-up + deauth solid everywhere,
handshake confirmed on most cards, PMKID confirmed on a few, WEP/WPS proven on
the RTL8821AU reference card, and Stress essentially unstarted.
