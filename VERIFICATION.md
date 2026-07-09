# Wifit3 — Hardware Verification

Wifit3 drives these USB radios directly and (mostly) correctly by imitating Linux drivers.

- Some drivers are a complete byte-perfect port of a known-good driver.
- Others merely imitate one — the bare-minimum hardware operations for a working radio.

The matrix below captures *how well wifit3 drives each card* right now. Every blemish is
either a documented Wifit3 bug or a hardware limitation; the deep per-card detail and history
live in each chip's `<CHIP>.md` (linked under its table).

**✅** works · **⚠️** works, with a caveat · **❌** tried, broken · **⬜** not run yet

- **RX** — receive: range, reception quality, channel tune. *Passive* captures rely on RX.
- **TX** — frame injection: Deauths, WPS, WEP, PMKID extraction, etc all rely on TX.
- **ACKs** — radio HW-ACKs a forged MAC. WPS relies heavily on Auto-ACKing, PMKID less so, Deauth/WEP not at all.
- **Port** — Performance comparison to the Linux driver port (RX breadth & quality).
- **Stress** — 30-min channel-hopping soak, only tracks RX degradation over time.

## Matrix

| Chipset | RX | TX | ACKs | Port | Stress | Grade |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| [AR9271](#ar9271) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [MT7612U](#mt7612u) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8821AU](#rtl8821au) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8812AU](#rtl8812au) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8822BU](#rtl8822bu) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8821CU](#rtl8821cu) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RT3070](#rt3070) | ✅ | ✅ | ✅ | ⬜ | ✅ | A |
| [RT5370](#rt5370) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RT5372](#rt5372) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [MT7610U](#mt7610u) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RT5572](#rt5572) | ✅ | ⚠️ | ✅ | ✅ | ✅ | B |
| [MT7921AU](#mt7921au) | ✅ | ✅ | ❌ | ✅ | ✅ | B |
| [RTL8188EUS](#rtl8188eus) | ⚠️ | ✅ | ✅ | ⚠️ | ✅ | B |
| [RTL8187L](#rtl8187l) | ✅ | ✅ | ❌ | ✅ | ✅ | C |
| [RT2500USB](#rt2500usb) | ✅ | ✅ | ❌ | ⬜ | ⚠️ | B |
| [RTL8814AU](#rtl8814au) | ❌ | ✅ | ✅ | ⚠️ | ❌ | D |

## Per-card notes

### AR9271
*ALFA AWUS036NHA · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **92% (A)** | 2026-07-06 | v2 faithful; consistency confirmed on a 2nd AR9271 unit. |
| RX | ✅ | 2026-07-06 | v2 mud2g 8.0 vs linux 8.4 (95%); breadth 71 vs 65; RSSI +2.2 dB. |
| Port | ✅ | 2026-07-06 | v2 matches ath9k_htc. v1 is broken (1 AP, dead hopping) — slated for removal (see BUGS). |
| Handshake | ✅ | 2026-05-25 | Full M1–M4, warm + cold. |
| PMKID | ✅ | 2026-05-25 | First-try, cold boot, real APs. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| ACKs | ✅ | 2026-05-31 | WPS PIN/PBC → auto-ACK. |
| Stress | ✅ | 2026-06-05 | 30-min 13-ch soak, flat. |

→ [AR9271.md](src/wifit3/chips/ar9271/AR9271.md)

### RTL8187L
*ALFA AWUS036H · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **71% (C)** | 2026-07-09 | Kernel-parity RX (breadth ≥ linux) + full 2.4 attack suite; capped by the low 802.11g RX ceiling, no auto-ACK, flaky WPS, and ~175 IVs/s WEP. |
| RX | ✅ | 2026-07-09 | ref AP 5.9 vs linux 6.7/s (88%); breadth 59 vs 54 (≥ linux); RSSI −0.9 dB; 11/11 tune, 0 silent, 0 cross. |
| TX | ✅ | 2026-07-09 | Deauth (byte-match aireplay incl. ACK-NAV) + WEP inject; live-confirmed. |
| ACKs | ❌ | 2026-06-21 | hard-MAC — cannot ACK a forged MAC. |
| Port | ✅ | 2026-07-09 | Matches linux (rtl8187): breadth 59 vs 54, RSSI −0.9 dB, 11/11 tune; beacon rate 88% (5.9 vs 6.7/s — single-AP variance on this low-rate card). |
| Handshake | ✅ | 2026-06-12 | Deauth → 4-way (~3/4 M1–M4). |
| PMKID | ✅ | 2026-06-12 | Passive + active. |
| WEP | ✅ | 2026-07-09 | FakeAuth + ARP replay + ChopChop; ~175 IVs/s. |
| WPS | ⚠️ | 2026-06-21 | Fails frequently — hard-MAC can't ACK. |
| Stress | ✅ | 2026-06-11 | 30-min 13-ch soak, flat. |

→ [RTL8187L.md](src/wifit3/chips/rtl8187/RTL8187L.md)

### RTL8188EUS
*TP-Link TL-WN722N v2/v3 · 2.4 GHz*

> **Default = vendor/DKMS port.** `WIFIT3_RTL8188=mainline` opts back. DKMS is the default for
> *stability*, not raw rate: the mainline `rtl8xxxu` RX collapsed under test (needed an airmon
> re-kick), while DKMS stayed steady. Same-driver baselines: DKMS port trails its driver ~24%
> (5.3 vs 7.0 bcn/s), mainline port is closer (6.1 vs 6.9) — so the DKMS port is where the RX work is.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **80% (B)** | 2026-07-06 | All attacks work; DKMS-default port leaves ~24% RX on the table. |
| RX | ⚠️ | 2026-07-06 | DKMS default 5.3 vs linux-DKMS 7.0 bcn/s (76%); breadth 78 vs 71 (matches). |
| Port | ⚠️ | 2026-07-06 | DKMS port trails its driver ~24% on beacon rate (RSSI +0.2 dB, breadth fine — RX throughput only). |
| Handshake | ✅ | 2026-05-19 | Passive 4-way. |
| PMKID | ✅ | 2026-05-19 | Active harvest — instant. |
| WEP | ✅ | 2026-06-16 | ChopChop 32/32; ARP replay 200+ IVs/s. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| ACKs | ✅ | 2026-05-31 | WPS PIN/PBC completed → auto-ACK works. |
| Stress | ✅ | 2026-06-16 | 30-min soak flat (mainline degrades/collapses). |

→ [RTL8188EUS_DKMS.md](src/wifit3/chips/rtl8188eus_dkms/RTL8188EUS_DKMS.md) (default) · [RTL8188EUS.md](src/wifit3/chips/rtl8188eus/RTL8188EUS.md) (mainline)

### RTL8821AU
*ALFA AWUS036ACS · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** (table below). `WIFIT3_RTL8821=mainline` opts back.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **91% (A)** | 2026-07-07 | Clean dual-band on both variants, full attack suite, no wedge. |
| RX | ✅ | 2026-07-07 | DKMS mud2g 7.0/7.8 (90%), mud 9.3/9.6 (97%), breadth 66/31; mainline 91% too. |
| Port | ✅ | 2026-07-07 | Matches linux both bands, DKMS + mainline (unlike 8812au, mainline doesn't wedge). |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| ACKs | ✅ | 2026-06-05 | HW-ACK forged MAC (WPS/PMKID). |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, flat. |

→ [RTL8821AU.md](src/wifit3/chips/rtl8821au/RTL8821AU.md) (mainline) · [RTL8821AU_DKMS.md](src/wifit3/chips/rtl8821au_dkms/RTL8821AU_DKMS.md) (default)

### RTL8812AU
*ALFA AWUS036ACH · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** (table below). `WIFIT3_RTL8812=mainline` opts back — but mainline
> **wedges on 2.4↔5 GHz hopping** (RF synth loses lock; confirmed 2026-07-07, ch153/161 dropped), so
> it's fixed-channel only. DKMS hops clean. *(The DKMS driver won't compile on kernel 6.19, so the
> same-driver Port baseline couldn't be re-run fresh — Port ✅ is vs the prior linux-DKMS + a clean
> live dual-band hop.)*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **91% (A)** | 2026-07-07 | Clean dual-band DKMS default, full attack suite; mainline-wedge is opt-in only. |
| RX | ✅ | 2026-07-07 | DKMS mud2g 6.4/s, mud 9.2/s, breadth 91/40; no wedge on the dual-band hop. |
| Port | ✅ | 2026-07-07 | Clean dual-band hop; same-driver baseline stale (6.19 build fails) — see note. |
| TX | ✅ | 2026-06-05 | Client drop + reconnect caught. |
| Handshake | ✅ | 2026-06-05 | M2/M4 (ToDS) — crackable. |
| PMKID | ✅ | 2026-06-05 | Capture + active extract. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| ACKs | ✅ | 2026-06-05 | HW-ACK forged MAC (WPS/PMKID). |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, flat. |

→ [RTL8812AU_DKMS.md](src/wifit3/chips/rtl8812au_dkms/RTL8812AU_DKMS.md) (default) · [RTL8812AU.md](src/wifit3/chips/rtl8812au/RTL8812AU.md) (mainline)

### RTL8822BU
*TP-Link Archer T3U Plus v1 · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** (table below). `WIFIT3_RTL8822=mainline` opts back.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **90% (A)** | 2026-07-08 | Dual-band kernel-parity RX + full attack suite; supersedes the stale 2026-06-17 Scan ❌. |
| RX | ✅ | 2026-07-08 | 2.4 6.5 vs 6.4/s (102%), 5 GHz 9.5 vs 9.7/s (99%); breadth 78/36 (2.4 matches; 5 GHz 36 vs 39); RSSI −0.5 dB; 0 silent. |
| Port | ✅ | 2026-07-08 | Matches linux-DKMS (88x2bu) both bands — beacon rate, breadth, RSSI parity on the same-card A/B. |
| TX | ✅ | 2026-06-16 | Dropped a real laptop + phone. |
| Handshake | ✅ | 2026-06-16 | Deauth → full M1–M4. |
| PMKID | ✅ | 2026-06-16 | Passive capture + extract. |
| WEP | ✅ | 2026-06-16 | ChopChop + ARP replay ~225 IVs/s. |
| WPS | ✅ | 2026-06-16 | PBC → PSK; PIN → M4. |
| ACKs | ✅ | 2026-06-16 | HW-ACK forged MAC (WPS PBC/PIN). |
| Stress | ✅ | 2026-07-08 | 30-min 38-ch soak, flat (active-AP trend 1.03, no death-detect). |

→ [RTL8822BU_DKMS.md](src/wifit3/chips/rtl8822bu_dkms/RTL8822BU_DKMS.md) (default) · [RTL8822BU.md](src/wifit3/chips/rtl8822bu/RTL8822BU.md) (mainline)

### RTL8814AU
*ALFA AWUS1900 · 2.4 / 5 GHz · 4T4R*

> **Good card, our port wedges it — the D is on us, not the silicon.** Driven directly by the
> out-of-tree `rtl8814au` (DKMS) driver, the AWUS1900 runs deauth + 2.4/5 GHz hopping cleanly; under
> wifit3 it wedges (2.4 RX drops, 5 GHz intermittent) once TX + hopping mix. Steady-dwell RX, breadth,
> and RSSI all match the OOT driver (mud2g 7.1 vs 8.4 bcn/s, RSSI +0.2 dB), so the bug is in our port's
> TX/hop state handling — fixable, not hardware. 5 GHz steady is strong.
> *(morrownr, the OOT maintainer, separately advises against the AWUS1900 over his mainline driver's
> RX shortfall — a different, driver-specific concern from our wedge.)*

> **Default = vendor/DKMS port.** `WIFIT3_RTL8814=mainline` opts back.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **60% (D)** | 2026-07-06 | Port wedges under TX + 2.4/5 GHz hopping; hardware, steady RX, and attacks are fine. |
| RX | ❌ | 2026-07-06 | Steady dwell strong (mud2g 7.1 vs OOT 8.4/s; breadth 82/48) but 2.4 RX drops under channel-hopping — unusable in normal (hopping) operation. |
| Port | ⚠️ | 2026-07-06 | Steady RX + RSSI (+0.2 dB) match OOT; wedges under TX+hop where OOT stays clean — dynamic-path bug. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| ACKs | ✅ | 2026-06-05 | WPS PIN/PBC completed → auto-ACK works. |
| Stress | ❌ | 2026-06-17 | Soak decays; wedges under sustained TX+hop. |

→ [RTL8814AU.md](src/wifit3/chips/rtw88_8814au/RTL8814AU.md) (mainline) · [RTL8814AU_DKMS.md](src/wifit3/chips/rtl8814au_dkms/RTL8814AU_DKMS.md) (default)

### MT7612U
*ALFA AWUS036ACM · 2.4 / 5 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **93% (A)** | 2026-07-06 | Faithful dual-band port, linux-parity RX, full suite. |
| RX | ✅ | 2026-07-06 | mud2g 7.6/8.3 (92%), mud 8.9/9.7 (92%); breadth 109/43 (matches, best 2.4); RSSI −1.5 dB. |
| Port | ✅ | 2026-07-06 | Matches mt76x2u both bands. |
| Handshake | ✅ | 2026-05-31 | Full M1–M4. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| ACKs | ✅ | 2026-05-31 | WPS PIN/PBC → auto-ACK. |
| Stress | ✅ | 2026-07-08 | 30-min 22-ch dual-band soak, flat (TSSI-on default). |

→ [MT76X2U.md](src/wifit3/chips/mt76x2u/MT76X2U.md)

### MT7610U
*ALFA AWUS036ACHM · 2.4 / 5 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **93% (A)** | 2026-07-07 | Faithful dual-band port, linux-parity RX, full suite. |
| RX | ✅ | 2026-07-07 | mud2g 7.1/7.3 (97%), mud 9.3/9.7 (96%); breadth 132/34 (best 2.4, matches). |
| Port | ✅ | 2026-07-07 | Matches mt76x0u both bands. |
| Handshake | ✅ | 2026-05-31 | M1+M2. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| ACKs | ✅ | 2026-05-31 | WPS PIN/PBC → auto-ACK. |
| Stress | ✅ | 2026-06-16 | 30-min 22-ch dual-band soak, flat. |

→ [MT76X0U.md](src/wifit3/chips/mt76x0u/MT76X0U.md)

### MT7921AU
*ALFA AWUS036AXML / Panda PAU0F · 2.4 / 5 GHz*

> **No active-monitor / auto-ACK.** The mt76 driver we ported doesn't appear to support
> Active Monitor Mode, so the card won't auto-ACK frames addressed to its MAC. That makes
> conversational attacks — PMKID and WPS — chatty and prone to timeout/failure. Corroborated
> upstream: [openwrt/mt76#839](https://github.com/openwrt/mt76/issues/839) ·
> [morrownr/USB-WiFi#107](https://github.com/morrownr/USB-WiFi/issues/107).

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **80% (B)** | 2026-07-06 | Best-in-batch RX + faithful port; capped by no-auto-ACK (conversational attacks). |
| RX | ✅ | 2026-07-06 | mud2g 8.6/8.9 (97%), mud 9.3/9.6 (97%); breadth 112/52 (best 2.4, matches); RSSI −1.4 dB. |
| Port | ✅ | 2026-07-06 | Matches mt76 both bands — the B is the driver's ACK limit, not the port. |
| TX | ✅ | 2026-06-12 | Live deauth dropped client. |
| Handshake | ✅ | 2026-06-12 | Deauth → 4-way (28 EAPOL, M1–M4). |
| PMKID | ⚠️ | 2026-06-23 | Auto-ACKing not supported. |
| WEP | ✅ | 2026-06-12 | ChopChop + ARP replay ~350 IVs/s. |
| WPS | ⚠️ | 2026-06-23 | Auto-ACKing not supported. |
| ACKs | ❌ | 2026-06-23 | mt76 has no active-monitor auto-ACK (see note above). |
| Stress | ✅ | 2026-06-19 | 30-min 38-ch dual-band soak, flat. |

→ [MT7921AU.md](src/wifit3/chips/mt7921au/MT7921AU.md)

### RT5372
*Panda PAU05 + PAU06 · 2.4 GHz · 2T2R*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **92% (A)** | 2026-07-06 | Faithful port, linux-parity RX, full attack suite, flat soak. |
| RX | ✅ | 2026-07-06 | mud2g 6.6 vs linux 7.1 (93%); breadth 75 vs 79; RSSI +0.7 dB; 0 cross-channel. |
| Port | ✅ | 2026-07-06 | Matches linux (rt2800usb); accurate RSSI (+0.7 dB). |
| TX | ✅ | 2026-06-10 | Live → reconnect; byte-match w/ aireplay-ng. |
| Handshake | ✅ | 2026-06-10 | Deauth → 4-way (~27 EAPOL/30 s). |
| PMKID | ✅ | 2026-06-10 | Capture + active extract. |
| WEP | ✅ | 2026-06-10 | ARP replay + ChopChop. |
| WPS | ✅ | 2026-06-10 | PIN + PBC. |
| ACKs | ✅ | 2026-06-10 | WPS PIN/PBC → auto-ACK. |
| Stress | ✅ | 2026-06-10 | 30-min 14-ch soak (PAU05 + PAU06), flat. |

→ [RT5372.md](src/wifit3/chips/rt5372/RT5372.md) (default) · [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md) (rt2800usb fallback)

### RT5572
*Panda PAU09 N600 · 2.4 / 5 GHz · 2T2R*

> **5 GHz injection is flaky on nearby APs** — deauth / PMKID / WPS can drop on a strong nearby
> 5 GHz AP. Distant 5 GHz and all of 2.4 GHz are unaffected.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **85% (B)** | 2026-07-09 | Kernel-parity dual-band RX, full 2.4 attack suite, auto-ACK; 5 GHz injection flaky on nearby APs. |
| RX | ✅ | 2026-07-09 | ref AP 2.4 7.7/7.7 (100%), 5 GHz 8.6/9.7 (89%); breadth 103 vs 109 / 38 vs 38 (5 GHz matches); RSSI +0.3 dB; 16/16 tune, 0 silent. |
| TX | ⚠️ | 2026-07-09 | 2.4 GHz clean; 5 GHz injection flaky on nearby APs (distant 5 GHz + all 2.4 fine). |
| Port | ✅ | 2026-07-09 | Matches linux (rt2800usb) both bands; accurate RSSI (+0.3 dB — the old +8/+11 over-read is fixed: rx.py applies the per-band EEPROM offset + lna_gain). |
| Handshake | ✅ | 2026-07-09 | 2.4 deauth → 4-way; 5 GHz passive capture. |
| PMKID | ✅ | 2026-07-09 | Passive + active (2.4 + distant 5 GHz); 5 GHz nearby harvest limited by TX. |
| WEP | ✅ | 2026-07-09 | 2.4 ChopChop + ARP replay ~210 IVs/s (no 5 GHz WEP target). |
| WPS | ✅ | 2026-07-09 | 2.4 PBC (14 EAPOL) + PIN, auto-ACK; 5 GHz nearby assoc limited by TX. |
| ACKs | ✅ | 2026-07-09 | Auto-ACK forged MAC (WPS PBC/PIN + active PMKID). |
| Stress | ✅ | 2026-07-09 | 30-min 22-ch soak: 5 GHz flat (34→39), 2.4 mild drift (trend 0.90) but frames/bucket flat → environmental; no death-detect. |

→ [RT5572.md](src/wifit3/chips/rt5572/RT5572.md)

### RT3070
*ALFA AWUS036NH · 2.4 GHz · 1T1R*

Excellent 2.4 GHz front-end (external LNA) — strong range, signal, and TX rate.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **A** | 2026-06-09 | Full 2.4 GHz attack suite, flat soak. |
| RX | ✅ | 2026-06-09 | Kernel parity (~8.4 bcn/s vs kernel's 8.9). |
| TX | ✅ | 2026-06-09 | Deauth byte-match w/ aireplay-ng. |
| ACKs | ✅ | 2026-06-09 | WPS PIN → M4, PBC → PSK (auto-ACK works). |
| Port | ⬜ | — | No Linux same-card baseline captured. |
| Handshake | ✅ | 2026-06-09 | Deauth → reconnect; 39 EAPOL/30 s. |
| PMKID | ✅ | 2026-06-09 | Passive + active extract. |
| WEP | ✅ | 2026-06-09 | Replay + ChopChop ~300 inj/s (20k IVs <90 s). |
| WPS | ✅ | 2026-06-09 | PIN → M4; PBC → PSK. |
| Stress | ✅ | 2026-06-09 | 30-min 14-ch soak, flat. |

→ [RT3070.md](src/wifit3/chips/rt3070/RT3070.md)

### RT2500USB
*Buffalo Nintendo Wi-Fi / RT2570 · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **B** | 2026-06-21 | Passive capture + WEP work (slow); WPS flaky (hard-MAC, no auto-ACK). |
| RX | ✅ | 2026-06-11 | ~9 bcn/s, 10+ APs. |
| TX | ✅ | 2026-06-11 | Deauth + WEP inject; slow (~60 IVs/s). |
| ACKs | ❌ | 2026-06-21 | hard-MAC — cannot ACK a forged MAC. |
| Port | ⬜ | — | No Linux same-card baseline captured. |
| Handshake | ✅ | 2026-06-11 | Deauth → reconnect; M1+M2+M3. |
| PMKID | ✅ | 2026-06-11 | Passive + active extract. |
| WEP | ✅ | 2026-06-11 | ChopChop + ARP replay (slow, ~60 IVs/s). |
| WPS | ⚠️ | 2026-06-21 | Fails frequently — hard-MAC can't ACK. |
| Stress | ⚠️ | 2026-06-11 | 30-min 14-ch soak; mild late taper. |

→ [RT2500USB.md](src/wifit3/chips/rt2500usb/RT2500USB.md)

### RTL8821CU
*Auscoumer 600 Mbps · 2.4 / 5 GHz*

> **ZeroCD (Windows only).** The unit tested enumerates as a CD-ROM (ZeroCD) on Windows; eject the
> disk drive before Wifit3 can see the radio. Not an issue on Linux.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **90% (A)** | 2026-07-08 | Faithful dual-band port, full attack suite, flat soak. |
| RX | ✅ | 2026-07-06 | mud2g 7.2/8.0 (90%), mud 8.8/9.3 (95%); breadth 73/29 (matches); RSSI +1.1 dB. |
| Port | ✅ | 2026-07-06 | Matches linux-DKMS both bands. |
| Handshake | ✅ | 2026-06-24 | 4-way captured. |
| PMKID | ✅ | 2026-06-24 | Capture + active extract (2.4 + 5). |
| WEP | ✅ | 2026-07-06 | 2.4 ChopChop + ARP replay ~225 IVs/s (no 5 GHz WEP target). |
| WPS | ✅ | 2026-06-24 | PBC — ~25 EAPOLs (HW-ACK forged MAC). |
| ACKs | ✅ | 2026-06-24 | HW-ACK forged MAC (WPS + 5 GHz PMKID/deauth). |
| Stress | ✅ | 2026-07-08 | 30-min 22-ch soak, flat (trend 1.08, no death-detect). |

→ [RTL8821CU_DKMS.md](src/wifit3/chips/rtl8821cu_dkms/RTL8821CU_DKMS.md)

### RT5370
*LOTEKOO 150 Mbps · 2.4 GHz · 1T1R*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **90% (A)** | 2026-07-08 | 2.4 GHz near-kernel RX + full clean attack suite; 1T1R breadth is the only real gap. |
| RX | ✅ | 2026-07-08 | ref AP 8.6 vs 9.1/s (95%); breadth 50 vs 57; RSSI +0.8 dB (accurate); 9/9 tune, 0 silent. |
| Port | ✅ | 2026-07-08 | Matches rt2800usb — beacon rate + RSSI parity; accurate RSSI (+0.8 dB). |
| Handshake | ✅ | 2026-06-24 | 4-way captured. |
| PMKID | ✅ | 2026-06-24 | Capture + active extract. |
| WEP | ✅ | 2026-07-08 | 2.4 GHz ChopChop + ARP replay ~200 IVs/s. |
| WPS | ✅ | 2026-06-24 | PBC — 13 EAPOLs (HW-ACK forged MAC). |
| ACKs | ✅ | 2026-07-08 | HW-ACK forged MAC (re-confirmed). |
| Stress | ✅ | 2026-07-08 | 30-min 14-ch soak, flat (trend 1.22, no death-detect). |

→ [RT5370.md](src/wifit3/chips/rt5370/RT5370.md)

## Unsupported

### RT3572 — ALFA AWUS051NH v2 — untested

Our only unit (bought 2015) is a counterfeit with a blank EFUSE — no factory RF
calibration — so it can't validate the chip. The `rt2800usb` driver is shared with the
working RT5372/RT5572, so the port itself stays supported; re-test if a genuine unit turns
up. → [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md)

## Stress soak

A **30-minute** sustained-hop soak — `scripts/diag/sweep.py --skip-baseline --longrun-min 30`,
hopping all channels — with the attacks still working afterward. ✅ = no degradation trend
across the 60 s buckets *and* post-soak attacks pass.

*Why 30 min, not an hour:* across a dozen cards a 1-hour bar is a full day of hands-on
scanning, and 30 min already resolves the degradation curve — clean runs stay flat the whole
time, and the failures (RT2500USB) show within the first minute.

## Hardware queue

*"Will you support card X?"* — maybe. A chipset gets added when we have the adapter in
hand **and** a clean cold-boot USB capture to port against (the process is
`docs/porting/METHODOLOGY.md`). Good candidates are the USB adapters morrownr recommends for
Kali: <https://github.com/morrownr/USB-WiFi/blob/main/home/Recommended_Adapters_for_Kali_Linux.md>

**En route (ordered, awaiting delivery):**

- **Deal4Go K2-544DW** — **AR9271** · *consistency test* (already supported on the AWUS036NHA).
  Confirms a second AR9271 card behaves identically — the "any AR9271 works" claim.

**Wishlist (not yet bought):**

- **TP-Link Archer T2U Plus** — RTL8821AU / RTL8811AU.
- **Generic MT7601U** — cheapest dongle; known for awkward packet injection.
