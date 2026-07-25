# Wifit3 — Hardware Verification

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
| [AR9271](#ar9271) | ✅ | ✅ | ✅ | ⬜ | ✅ | A |
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
| [MT7921AU](#mt7921au) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8188EUS](#rtl8188eus) | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8187L](#rtl8187l) | ✅ | ✅ | ❌ | ✅ | ✅ | C |
| [RT2500USB](#rt2500usb) | ⚠️ | ✅ | ❌ | ⬜ | ⚠️ | D |
| [RTL8814AU](#rtl8814au) | ❌ | ✅ | ✅ | ⚠️ | ✅ | D |

## Per-card notes

### AR9271
*ALFA AWUS036NHA · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **92% (A)** | 2026-07-06 | Consistency confirmed on a 2nd AR9271 unit. |
| RX | ✅ | 2026-07-06 | ref2g 8.0 vs linux 8.4 (95%); breadth 71 vs 65; RSSI +2.2 dB. |
| Port | ⬜ | - | *needs baseline* |
| Handshake | ✅ | 2026-07-12 | confirmed (M1–M4). |
| PMKID | ✅ | 2026-07-12 | confirmed (extraction). |
| WEP | ✅ | 2026-07-12 | ChopChop + ARP replay, ~200 IVs/s. |
| WPS | ✅ | 2026-07-12 | PIN + PBC. |
| ACKs | ✅ | 2026-07-12 | auto-RX-ACK: <30 EAPOL frames/PBC. |
| Stress | ⬜ | 2026-06-05 | 30-min 13-ch soak, flat. |

→ [AR9271_V2.md](src/wifit3/chips/ar9271_v2/AR9271_V2.md)

### RTL8187L
*ALFA AWUS036H · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **71% (C)** | 2026-07-09 | Kernel-parity RX (breadth ≥ linux) + full 2.4 attack suite; capped by the low 802.11g RX ceiling, no auto-ACK, flaky WPS, and ~175 IVs/s WEP. |
| RX | ✅ | 2026-07-09 | ref AP 5.9 vs linux 6.7/s (88%); breadth 59 vs 54 (≥ linux); RSSI −0.9 dB; 11/11 tune, 0 silent, 0 cross. |
| TX | ✅ | 2026-07-09 | Deauth + WEP inject; live-confirmed. |
| ACKs | ❌ | 2026-06-21 | Hard-MAC: cannot ACK a forged MAC. |
| Port | ✅ | 2026-07-09 | Matches linux (rtl8187): breadth 59 vs 54, RSSI −0.9 dB, 11/11 tune; beacon rate 88% (5.9 vs 6.7/s). |
| Handshake | ✅ | 2026-06-12 | Deauth → 4-way (~3/4 M1–M4). |
| PMKID | ✅ | 2026-06-12 | Passive + active. |
| WEP | ✅ | 2026-07-09 | FakeAuth + ARP replay + ChopChop; ~175 IVs/s. |
| WPS | ⚠️ | 2026-06-21 | Fails frequently: hard-MAC can't ACK. |
| Stress | ✅ | 2026-06-11 | 30-min 13-ch soak, flat. |

→ [RTL8187L.md](src/wifit3/chips/rtl8187/RTL8187L.md)

### RTL8188EUS
*TP-Link TL-WN722N v2/v3 · 2.4 GHz*

> Ported from the [aircrack-ng/rtl8188eus](https://github.com/aircrack-ng/rtl8188eus) vendor/DKMS
> port. There is a separate (weaker) port for the mainline kernel v6.18 driver (opt-in via
> `WIFIT3_RTL8188=mainline`), but the default DKMS port out-performs mainline (as expected).

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **90% (A)** | 2026-07-09 | Kernel-parity RX + full attack suite, all re-confirmed. |
| RX | ✅ | 2026-07-09 | DKMS port 7.7 vs linux-DKMS 7.29/s (matches); breadth 102 vs 91 (≥ linux); RSSI −0.6 dB; 11/11 tune, 0 silent, 0 cross. |
| Port | ✅ | 2026-07-09 | Matches linux-DKMS (8188eu): 98% total beacons (6570 vs 6693), breadth 102 ≥ 91, RSSI −0.6 dB, 11/11 tune. |
| Handshake | ✅ | 2026-07-09 | Deauth → 4-way. |
| PMKID | ✅ | 2026-07-09 | Active extract. |
| WEP | ✅ | 2026-07-09 | ChopChop + ARP replay ~175 IVs/s. |
| WPS | ✅ | 2026-07-09 | PBC (~20 EAPOL) + PIN. |
| ACKs | ✅ | 2026-07-09 | Auto-ACK forged MAC (WPS PBC). |
| Stress | ✅ | 2026-06-16 | 30-min soak flat (mainline degrades/collapses). |

→ [RTL8188EUS_DKMS.md](src/wifit3/chips/rtl8188eus_dkms/RTL8188EUS_DKMS.md) (default) · [RTL8188EUS.md](src/wifit3/chips/rtl8188eus/RTL8188EUS.md) (mainline)

### RTL8821AU
*ALFA AWUS036ACS · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** (table below). `WIFIT3_RTL8821=mainline` opts back.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **91% (A)** | 2026-07-07 | Clean dual-band on both variants, full attack suite, no wedge. |
| RX | ✅ | 2026-07-07 | DKMS ref2g 7.0/7.8 (90%), ref5g 9.3/9.6 (97%), breadth 66/31; mainline 91% too. |
| Port | ✅ | 2026-07-07 | Matches linux both bands, DKMS + mainline. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| ACKs | ✅ | 2026-06-05 | HW-ACK forged MAC (WPS/PMKID). |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, flat. |

→ [RTL8821AU.md](src/wifit3/chips/rtl8821au/RTL8821AU.md) (mainline) · [RTL8821AU_DKMS.md](src/wifit3/chips/rtl8821au_dkms/RTL8821AU_DKMS.md) (default)

### RTL8812AU
*ALFA AWUS036ACH · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** (table below). `WIFIT3_RTL8812=mainline` opts back. But mainline
> **wedges on 2.4↔5 GHz hopping** (RF synth loses lock; confirmed 2026-07-07, ch153/161 dropped), so
> it's fixed-channel only. DKMS hops clean. *(The DKMS driver won't compile on kernel 6.19, so the
> same-driver Port baseline couldn't be re-run fresh. Port ✅ is vs the prior linux-DKMS + a clean
> live dual-band hop.)*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **91% (A)** | 2026-07-07 | Clean dual-band DKMS default, full attack suite; mainline-wedge is opt-in only. |
| RX | ✅ | 2026-07-07 | DKMS ref2g 6.4/s, ref5g 9.2/s, breadth 91/40; no wedge on the dual-band hop. |
| Port | ✅ | 2026-07-07 | Clean dual-band hop; same-driver baseline stale (6.19 build fails). See note. |
| TX | ✅ | 2026-06-05 | Client drop + reconnect caught. |
| Handshake | ✅ | 2026-06-05 | M2/M4 (ToDS): crackable. |
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
| **Grade** | **90% (A)** | 2026-07-08 | Dual-band kernel-parity RX + full attack suite. |
| RX | ✅ | 2026-07-08 | 2.4 6.5 vs 6.4/s (102%), 5 GHz 9.5 vs 9.7/s (99%); breadth 78/36 (2.4 matches; 5 GHz 36 vs 39); RSSI −0.5 dB. |
| Port | ✅ | 2026-07-08 | Matches linux-DKMS (88x2bu) both bands. |
| TX | ✅ | 2026-06-16 | Deauth & PMKID extraction. |
| Handshake | ✅ | 2026-06-16 | Deauth → full M1–M4. |
| PMKID | ✅ | 2026-06-16 | Passive capture + extract. |
| WEP | ✅ | 2026-06-16 | ChopChop + ARP replay ~225 IVs/s. |
| WPS | ✅ | 2026-06-16 | PBC → PSK; PIN → M4. |
| ACKs | ✅ | 2026-06-16 | HW-ACK forged MAC (WPS PBC/PIN). |
| Stress | ✅ | 2026-07-08 | 30-min 38-ch soak, flat (active-AP trend 1.03, no death-detect). |

→ [RTL8822BU_DKMS.md](src/wifit3/chips/rtl8822bu_dkms/RTL8822BU_DKMS.md) (default) · [RTL8822BU.md](src/wifit3/chips/rtl8822bu/RTL8822BU.md) (mainline)

### RTL8814AU
*ALFA AWUS1900 · 2.4 / 5 GHz · 4T4R*

> The maintainer of the DKMS driver says Realtek's support for this driver is subpar, that the
> driver itself is not good, and advises not using cards that rely on this driver
> ([morrownr/8814au#37](https://github.com/morrownr/8814au/issues/37#issuecomment-900581613)).

> **Default = vendor/DKMS port.** `WIFIT3_RTL8814=mainline` opts back.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **60% (D)** | 2026-07-10 | RX-gated; steady + hopping RX and the full attack suite are fine. |
| RX | ❌ | 2026-07-10 | Steady + hopping match linux (ref2g 7.9/s, ref5g 9.7/s, breadth 65/50, RSSI +0.0 dB), but dwelling on a 2.4 channel after a 2.4↔5 hop goes dead ~15 s before it self-heals. |
| Port | ⚠️ | 2026-07-10 | Steady + round-robin RX match linux-DKMS (breadth/rate/RSSI parity), but the hop→dwell wedge is ours: the Linux driver handles the same hop→dwell cleanly, so it's a port gap. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active (2.4 + 5 GHz). |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| ACKs | ✅ | 2026-06-05 | WPS PIN/PBC completed → auto-ACK works. |
| Stress | ✅ | 2026-07-10 | 30-min 22-ch round-robin soak flat (2.4 active BSSIDs 57→67, trend 1.11). Continuous hopping swallows the dead-dwells. |

→ [RTL8814AU.md](src/wifit3/chips/rtw88_8814au/RTL8814AU.md) (mainline) · [RTL8814AU_DKMS.md](src/wifit3/chips/rtl8814au_dkms/RTL8814AU_DKMS.md) (default)

### MT7612U
*ALFA AWUS036ACM · 2.4 / 5 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **93% (A)** | 2026-07-06 | Faithful dual-band port, linux-parity RX, full suite. |
| RX | ✅ | 2026-07-06 | ref2g 7.6/8.3 (92%), ref5g 8.9/9.7 (92%); breadth 109/43 (matches, best 2.4); RSSI −1.5 dB. |
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
| RX | ✅ | 2026-07-07 | ref2g 7.1/7.3 (97%), ref5g 9.3/9.7 (96%); breadth 132/34 (best 2.4, matches). |
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

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **A** | 2026-07-25 | Best-in-batch RX + faithful dual-band port, full attack suite. |
| RX | ✅ | 2026-07-06 | ref2g 8.6/8.9 (97%), ref5g 9.3/9.6 (97%); breadth 112/52 (best 2.4, matches); RSSI −1.4 dB. |
| Port | ✅ | 2026-07-06 | Matches mt76 both bands. |
| TX | ✅ | 2026-07-25 | Inject 2.4 + 5 GHz. |
| Handshake | ✅ | 2026-07-25 | Deauth → 4-way (M1–M4). |
| PMKID | ✅ | 2026-07-25 | Passive capture + active extract. |
| WEP | ✅ | 2026-07-25 | ChopChop + ARP replay ~220 IVs/s. |
| WPS | ✅ | 2026-07-24 | PIN → M7 (5/5, auto-ACK). |
| ACKs | ✅ | 2026-07-24 | Auto-ACK forged MAC via active monitor (spoofed MAC). |
| Stress | ✅ | 2026-06-19 | 30-min 38-ch dual-band soak, flat. |

→ [MT7921AU.md](src/wifit3/chips/mt7921au/MT7921AU.md)

### RT5372
*Panda PAU05 + PAU06 · 2.4 GHz · 2T2R*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **92% (A)** | 2026-07-06 | Faithful port, linux-parity RX, full attack suite, flat soak. |
| RX | ✅ | 2026-07-06 | ref2g 6.6 vs linux 7.1 (93%); breadth 75 vs 79; RSSI +0.7 dB; 0 cross-channel. |
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

> **5 GHz injection is flaky on nearby APs**: deauth / PMKID / WPS can drop on a strong nearby
> 5 GHz AP. Distant 5 GHz and all of 2.4 GHz are unaffected.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **85% (B)** | 2026-07-09 | Kernel-parity dual-band RX, full 2.4 attack suite, auto-ACK; 5 GHz injection flaky on nearby APs. |
| RX | ✅ | 2026-07-09 | ref AP 2.4 7.7/7.7 (100%), 5 GHz 8.6/9.7 (89%); breadth 103 vs 109 / 38 vs 38 (5 GHz matches); RSSI +0.3 dB; 16/16 tune. |
| TX | ⚠️ | 2026-07-09 | 2.4 GHz clean; 5 GHz injection flaky on nearby APs (distant 5 GHz + all 2.4 fine). |
| Port | ✅ | 2026-07-09 | Matches linux (rt2800usb) both bands; accurate RSSI (+0.3 dB). |
| Handshake | ✅ | 2026-07-09 | 2.4 deauth → 4-way; 5 GHz passive capture. |
| PMKID | ✅ | 2026-07-09 | Passive + active (2.4 + distant 5 GHz); 5 GHz nearby harvest limited by TX. |
| WEP | ✅ | 2026-07-09 | 2.4 ChopChop + ARP replay ~210 IVs/s (no 5 GHz WEP target). |
| WPS | ✅ | 2026-07-09 | 2.4 PBC (14 EAPOL) + PIN, auto-ACK; 5 GHz nearby assoc limited by TX. |
| ACKs | ✅ | 2026-07-09 | Auto-ACK forged MAC (WPS PBC/PIN + active PMKID). |
| Stress | ✅ | 2026-07-09 | 30-min 22-ch soak: 5 GHz flat (34→39), 2.4 mild drift (trend 0.90). |

→ [RT5572.md](src/wifit3/chips/rt5572/RT5572.md)

### RT3070
*ALFA AWUS036NH · 2.4 GHz · 1T1R*

Excellent 2.4 GHz front-end (external LNA): strong range, signal, and TX rate.

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

> My first wireless card: It gets an "A" in my book simply for still working after 20 years!

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **D** | 2026-07-09 | Full attack suite works when the card is receiving, but RX is extremely flaky. No stable baseline is capturable and it's unreliable in normal use. |
| RX | ⚠️ | 2026-07-09 | Extremely intermittent: Drops to near-dead for long stretches; ~9–10 bcn/s in good spells. |
| TX | ✅ | 2026-07-09 | Deauth + PMKID + WEP inject; live-confirmed. |
| ACKs | ❌ | 2026-06-21 | Hard-MAC: cannot ACK a forged MAC. |
| Port | ⬜ | — | Card too flaky to capture a stable Linux baseline (0 frames on an unattended sweep). |
| Handshake | ✅ | 2026-06-11 | Deauth → reconnect; M1+M2+M3. |
| PMKID | ✅ | 2026-07-09 | Passive + active extract. |
| WEP | ✅ | 2026-07-09 | ChopChop + ARP replay; ~175 IVs/s. |
| WPS | ⚠️ | 2026-06-21 | Fails frequently: hard-MAC can't ACK. |
| Stress | ⚠️ | 2026-06-11 | 30-min 14-ch soak; mild late taper. |

→ [RT2500USB.md](src/wifit3/chips/rt2500usb/RT2500USB.md)

### RTL8821CU
*Auscoumer 600 Mbps · 2.4 / 5 GHz*

> **ZeroCD (Windows only).** The unit tested enumerates as a CD-ROM (ZeroCD) on Windows; eject the
> disk drive before Wifit3 can see the radio. Not an issue on Linux.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **90% (A)** | 2026-07-08 | Faithful dual-band port, full attack suite, flat soak. |
| RX | ✅ | 2026-07-06 | ref2g 7.2/8.0 (90%), ref5g 8.8/9.3 (95%); breadth 73/29 (matches); RSSI +1.1 dB. |
| Port | ✅ | 2026-07-06 | Matches linux-DKMS both bands. |
| Handshake | ✅ | 2026-06-24 | 4-way captured. |
| PMKID | ✅ | 2026-06-24 | Capture + active extract (2.4 + 5). |
| WEP | ✅ | 2026-07-06 | 2.4 ChopChop + ARP replay ~225 IVs/s (no 5 GHz WEP target). |
| WPS | ✅ | 2026-06-24 | PBC: ~25 EAPOLs (HW-ACK forged MAC). |
| ACKs | ✅ | 2026-06-24 | HW-ACK forged MAC (WPS + 5 GHz PMKID/deauth). |
| Stress | ✅ | 2026-07-08 | 30-min 22-ch soak, flat (trend 1.08, no death-detect). |

→ [RTL8821CU_DKMS.md](src/wifit3/chips/rtl8821cu_dkms/RTL8821CU_DKMS.md)

### RT5370
*LOTEKOO 150 Mbps · 2.4 GHz · 1T1R*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| **Grade** | **90% (A)** | 2026-07-08 | 2.4 GHz near-kernel RX + full clean attack suite; 1T1R breadth is the only real gap. |
| RX | ✅ | 2026-07-08 | ref AP 8.6 vs 9.1/s (95%); breadth 50 vs 57; RSSI +0.8 dB (accurate); 9/9 tune, 0 silent. |
| Port | ✅ | 2026-07-08 | Matches rt2800usb: beacon rate + RSSI parity; accurate RSSI (+0.8 dB). |
| Handshake | ✅ | 2026-06-24 | 4-way captured. |
| PMKID | ✅ | 2026-06-24 | Capture + active extract. |
| WEP | ✅ | 2026-07-08 | 2.4 GHz ChopChop + ARP replay ~200 IVs/s. |
| WPS | ✅ | 2026-06-24 | PBC: 13 EAPOLs (HW-ACK forged MAC). |
| ACKs | ✅ | 2026-07-08 | HW-ACK forged MAC (re-confirmed). |
| Stress | ✅ | 2026-07-08 | 30-min 14-ch soak, flat (trend 1.22, no death-detect). |

→ [RT5370.md](src/wifit3/chips/rt5370/RT5370.md)

## Unsupported

### RT3572 (ALFA AWUS051NH v2) — untested

Our only unit (bought 2015) is has a blank EFUSE (no factory RF calibration),
so it can't validate the chip. The `rt2800usb` driver is shared with the working RT5372/RT5572,
so the port itself stays supported; re-test if a genuine unit turns up.
→ [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md)

## Stress soak

A **30-minute** sustained-hop soak: `scripts/diag/sweep.py --skip-baseline --longrun-min 30`,
hopping all channels. ✅ = no degradation trend across the 60 s buckets *and* post-soak attacks pass.

*Why 30 min, not an hour:* across a dozen cards a 1-hour bar is a full day of hands-on
scanning, and 30 min already resolves the degradation curve.

## Hardware queue

*"Will you support card X?"* Maybe. A chipset gets added when we have the adapter in
hand **and** a clean cold-boot USB capture to port against (the process is
`docs/porting/METHODOLOGY.md`). Good candidates are the USB adapters morrownr recommends for
Kali: <https://github.com/morrownr/USB-WiFi/blob/main/home/Recommended_Adapters_for_Kali_Linux.md>

**Wishlist (not yet bought):**

- **TP-Link Archer T2U Plus** — RTL8821AU / RTL8811AU.
- **Generic MT7601U** — cheapest dongle; known for awkward packet injection.
