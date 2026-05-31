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

## Matrix

| Chipset | Scan | Deauth | Handshake | PMKID | WEP | WPS | Stress |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| AR9271 | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⬜ |
| RTL8187L | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| RTL8188EUS | ✅ | ✅ | ✅ | ✅ | ⬜ | ⬜ | ⬜ |
| RTL8821AU | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ |
| RTL8812AU | ⚠️ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ |
| RTL8822BU | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ |
| RTL8814AU | ⚠️ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⬜ |
| MT7612U | ✅ | ✅ | ✅ | ⬜ | ⚠️ | ⬜ | ⬜ |
| MT7610U | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| RT5372 (PAU05) | ✅ | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⬜ |
| RT5572 (PAU09) | ✅ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| RT3572 † | ✅ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| RT2500USB | ✅ | ✅ | ⚠️ | ✅ | ⬜ | ⬜ | ⬜ |

† **RT3572** here = the ALFA AWUS051NH v2 test unit with an **erased EFUSE** (no
factory RF calibration). That single fault explains every ⚠️ in its row — weak
TX/RX, runs 1T1R. The driver is verified byte-for-byte faithful; a properly-burned
RT3572 should be unaffected, but we have no burned unit to confirm. RT5372 /
RT5572 / RT3572 all share the `chips/rt2800usb/` driver.

### README asterisk rule

A chipset carries a `*` in the README's supported-hardware table when it has a
**❌, or a ⚠️ that is a genuine hardware/driver limitation of that card** (RF
hop-death, an unburned-EFUSE test unit, partial-direction capture). A ⚠️ that
only reflects **incomplete attack-suite coverage** (the attack works where it
was run, just not fully exercised on that card) does **not** asterisk — the card
is fine, the testing is simply unfinished.

Asterisked today: **RTL8812AU, RTL8822BU, RT2800USB, RT2500USB**.

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
| Handshake | ⬜ | — | A `--phase handshake` test exists; no end-to-end capture recorded. RX-poll reader-thread port is "awaiting HW verify." |
| PMKID | ⬜ | — | Not run. |
| WEP | ⬜ | — | Not run. |
| WPS | ⬜ | — | Not run. |
| Stress | ⬜ | — | Not run. |

→ `chips/rtl8187/RTL8187L.md`

### RTL8188EUS — TP-Link TL-WN722N v2/v3 (2.4 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-19 | Passive 4-way captured end-to-end (M1 + M3 + M4 + PMKID-in-M1) from a real client reconnect. |
| PMKID | ✅ | 2026-05-19 | Active harvest via AUTH+ASSOC injection — instant. |
| WEP | ⬜ | — | Not run. |
| WPS | ⬜ | — | Not run. |
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
| Handshake | ✅ | 2026-05-31 | Captured via deauth (full M1–M4; earlier 3/3-completion run 2026-05-25). |
| PMKID | ✅ | 2026-05-31 | Both paths: active extract (the "PMKID" button) **and** passive capture (alongside the deauth handshake). |
| WEP | ⚠️ | 2026-05-31 | ARP replay ✅ and ChopChop ✅ tested working. **Fragmentation ❌ — does not successfully forge/seed the ARP** (suspected bug). Notable: frag was verified on RTL8821AU, so this is likely a chip-specific sw-seq/oracle divergence on the 8822bu. |
| WPS | ✅ | 2026-05-31 | PIN brute ✅ and PBC auto-invade ✅, against a WPS test AP. |
| Stress | ⚠️ | 2026-05-31 | Attacks worked in-session, but **warm reattach on restart fails**: bulk-IN wedges (no frames in 1500 ms), unrecoverable in userland on Windows/WinUSB → user must unplug/replug. This is the *designed* warm-reattach fallback firing (it detects the wedge and asks for a replug), and is likely not 8822bu-specific. Not a clean 1-hour soak result. |

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
| Handshake | ✅ | 2026-05-29 | Handshake auto-saves; the de-pad fix restored a crackable handshake. |
| PMKID | ⬜ | — | Rides the same EAPOL path; not separately recorded. |
| WEP | ⚠️ | 2026-05-29 | ARP replay works first-try; the rest of the suite (frag / chopchop / crack) not exercised on this card. |
| WPS | ⬜ | — | Not run. |
| Stress | ⬜ | — | Not run. |

→ `chips/mt76x2u/MT76X2U.md`

### MT7610U — ALFA AWUS036ACHM (2.4 / 5 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ⬜ | — | ToDS (client→AP, M2/M4) capture is designed-for but documented as "should work" — unverified. |
| PMKID | ⬜ | — | Not run. |
| WEP | ⬜ | — | Not run. |
| WPS | ⬜ | — | Not run. |
| Stress | ⬜ | — | Not run. |

→ `chips/mt76x0u/MT76X0U.md`

### RT2800USB family — driver `chips/rt2800usb/` (three silicons)

M1–M4 (RX / scan) are WIRE-verified on all three silicons. M5 (TX inject, deauth
→ EAPOL re-capture) was verified on the RT5372/PAU05.

**RT5372 — Panda PAU05 (2.4 GHz, 1T1R)** — family reference chip.

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ✅ | — | M5 TX verification: deauth → EAPOL re-capture. |
| PMKID / WEP / WPS / Stress | ⬜ | — | Not run on this card. |

**RT5572 — Panda PAU09 N600 (2.4 / 5 GHz, 2T2R)**

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Deauth / Handshake | ⬜ | — | TX inject not separately verified on this silicon. |
| WEP | ⬜ | — | ARP replay was once dead here (zeroed-IV injection); fix status unconfirmed. |
| PMKID / WPS / Stress | ⬜ | — | Not run. |

**RT3572 — ALFA AWUS051NH v2 — ERASED EFUSE (no RF cal; runs 1T1R)**

The unit's EFUSE is erased, so it runs one chain with no factory RF calibration:
the rx-filter cal loop rails to a non-physical value and TX power sits at the low
fallback → weak TX/RX, high run-to-run variance. **The driver is verified
faithful; this is the unit, not the port** — a burned RT3572 should be
unaffected (no burned unit on hand to confirm). So the row below is "works even
on a miscalibrated unit," not a clean verification.

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Works but weak — ~8 beacons/s from an AP a few feet away (~10/s on healthy cards). |
| Deauth | ⚠️ | 2026-05-31 | Deauthed *something*, but too weak to knock a phone right next to the radio off. |
| Handshake | ⚠️ | 2026-05-31 | Partial (M1+M4) capture, weak; low beacon count points at uncalibrated RX. |
| PMKID | ⚠️ | 2026-05-31 | Passive capture works (with the handshake); the active "PMKID" button does not (too weak to elicit M1). |
| WEP | ⚠️ | 2026-05-31 | ARP replay ✅; Fragmentation ✅ (slow, many failed rounds); ChopChop ✗ (stalled at 22/32 bytes). FakeAuth bounced Associated↔Idle with errors — weak-TX/spotty-RX. |
| WPS | ⚠️ | 2026-05-31 | PBC timed out; PIN got 2 NACKs + 1 no-response (it *is* talking, just unreliable). |
| Stress | ⚠️ | 2026-05-31 | Warm boot fine. Once a channel "detuned": Focus on a CH1 AP showed 0 beacons, exit→re-enter Focus → 8 beacons/s. Looks like a Focus-entry tune that didn't take first time (separate from the EFUSE RF weakness). |

→ `chips/rt2800usb/RT2800USB.md` § "RT3572 unburned-EFUSE behaviour"

### RT2500USB — Buffalo Nintendo Wi-Fi USB Connector / RT2570 (2.4 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ⚠️ | — | Deauth burst kicked the client and recaptured EAPOL **M1+M3 (FromDS only)** live; the ToDS half (M2/M4) — i.e. a guaranteed crackable pair — isn't confirmed. |
| PMKID | ✅ | — | A PMKID was recaptured live on the same radio alongside M1+M3. |
| WEP | ⬜ | — | Not run. |
| WPS | ⬜ | — | Not run. |
| Stress | ⬜ | — | Not run. |

→ `chips/rt2500usb/RT2500USB.md`

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
