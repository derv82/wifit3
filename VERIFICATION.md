# Wifit3 — Hardware Verification

Wifit3 drives these USB radios directly and (mostly) correctly by imitating Linux drivers.

- Some drivers are a complete byte-perfect port of a known-good driver.
- Others merely imitate one — the bare-minimum hardware operations for a working radio.

The matrix below captures *how well wifit3 drives each card*. Every blemish is either a
documented Wifit3 bug or a hardware limitation; the deep per-card detail lives in each
chip's `<CHIP>.md` (linked under its table).

**✅** works · **⚠️** works, with a caveat · **❌** tried, broken · **⬜** not run yet — *not* a failure, just unconfirmed

## Matrix

| Chipset | Scan | Deauth | Hand-<br>shake | PMKID | WEP | WPS | Stress | Grade |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| [AR9271](#ar9271) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [MT7612U](#mt7612u) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8821AU](#rtl8821au) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8812AU](#rtl8812au) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RT3070](#rt3070) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RT5372](#rt5372) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RT5572](#rt5572) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8187L](#rtl8187l) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RT2500USB](#rt2500usb) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [MT7610U](#mt7610u) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | A |
| [RTL8822BU](#rtl8822bu) | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | C |
| [RTL8188EUS](#rtl8188eus) | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | B |
| [RTL8814AU](#rtl8814au) | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | D |

## Per-card notes

Scan + Deauth work on every supported card unless a note says otherwise. Cells are kept
terse — the full evidence and mechanics are in each `<CHIP>.md`.

### AR9271
*ALFA AWUS036NHA · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-25 | Full M1–M4, warm + cold. |
| PMKID | ✅ | 2026-05-25 | First-try, cold boot, real APs. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-05 | 30-min 13-ch soak, no degradation. |

→ [AR9271.md](src/wifit3/chips/ar9271/AR9271.md)

### RTL8187L
*ALFA AWUS036H · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-12 | Healthy, 215–323 frames/s; always cold-inits (radio doesn't survive a handle reopen). |
| Handshake | ✅ | 2026-06-12 | Deauth → 4-way (~3/4 M1–M4). |
| PMKID | ✅ | 2026-06-12 | Passive + active. |
| WEP | ✅ | 2026-06-12 | FakeAuth + ARP replay (~150–200 IVs/s) + ChopChop. |
| WPS | ✅ | 2026-06-12 | PIN + PBC. |
| Stress | ✅ | 2026-06-11 | 30-min 13-ch soak, no wedge or degradation. |

→ [RTL8187L.md](src/wifit3/chips/rtl8187/RTL8187L.md)

### RTL8188EUS
*TP-Link TL-WN722N v2/v3 · 2.4 GHz*

> **Default = vendor/DKMS port.** RX ties mainline (~6.6 bcn/s); DKMS wins the soak
> tie-break (holds 1.07 vs mainline's 0.84 degradation). `WIFIT3_RTL8188=mainline` opts back.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-06-16 | ~6.6 bcn/s best-AP (both ports) — below the 8/s bar. Ceiling is 1T1R silicon + airtime, not a port bug (severe audit found no divergence). |
| Handshake | ✅ | 2026-05-19 | Passive 4-way, end-to-end. |
| PMKID | ✅ | 2026-05-19 | Active harvest — instant. |
| WEP | ✅ | 2026-06-16 | ChopChop 32/32 first try; ARP replay 200+ IVs/s. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-16 | 30-min 13-ch soak: no degradation (1.07); mainline degrades (0.84) — why DKMS is default. |

→ [RTL8188EUS_DKMS.md](src/wifit3/chips/rtl8188eus_dkms/RTL8188EUS_DKMS.md) (default) · [RTL8188EUS.md](src/wifit3/chips/rtl8188eus/RTL8188EUS.md) (mainline)

### RTL8821AU
*ALFA AWUS036ACS · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** for `0bda:0811` (hotter 2.4 GHz RX, 5 GHz tied;
> `WIFIT3_RTL8821=mainline` falls back). Table below is that port.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-05 | 2.4 + 5 GHz; beacon-watch steady ~9/s, no gaps. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, no degradation. |

→ [RTL8821AU.md](src/wifit3/chips/rtl8821au/RTL8821AU.md) (mainline) · [RTL8821AU_DKMS.md](src/wifit3/chips/rtl8821au_dkms/RTL8821AU_DKMS.md) (default)

### RTL8812AU
*ALFA AWUS036ACH · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** (table below). Survives dual-band hopping; mainline
> (`WIFIT3_RTL8812=mainline`) RF-wedges at ~110 s on the same hop → fixed-channel fallback.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-05 | 2.4 + 5 GHz; survives the dual-band hop (incl. DFS). |
| Deauth | ✅ | 2026-06-05 | 2.4 + 5 GHz; client drop + reconnect caught. |
| Handshake | ✅ | 2026-06-05 | M2/M4 (ToDS) — crackable. |
| PMKID | ✅ | 2026-06-05 | Capture + active extract. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, no degradation. |

→ [RTL8812AU_DKMS.md](src/wifit3/chips/rtl8812au_dkms/RTL8812AU_DKMS.md) (default) · [RTL8812AU.md](src/wifit3/chips/rtl8812au/RTL8812AU.md) (mainline)

### RTL8822BU
*TP-Link Archer T3U Plus v1 · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** (`WIFIT3_RTL8822=mainline` opts back). DKMS earns it on
> *breadth* — ~2× the 2.4 GHz APs of mainline (antenna-mux fix), 5 GHz tied — but neither
> port handles a strong near AP (both saturate; see Scan).

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ❌ | 2026-06-17 | Fails the reference bar — saturates on the strongest near AP (~2.6 bcn/s, both ports): RX front-end overload (`pwdb=253`). DKMS hears ~2× mainline's breadth, but breadth ≠ the reference AP. 5 GHz fine (not re-tested). |
| Deauth | ✅ | 2026-06-16 | Dropped a real laptop + phone off the AP. |
| Handshake | ✅ | 2026-06-16 | Deauth → full M1–M4. |
| PMKID | ✅ | 2026-06-16 | Passive capture + extract. |
| WEP | ✅ | 2026-06-16 | ChopChop + ARP replay (~225 IVs/s avg). |
| WPS | ✅ | 2026-06-16 | PBC → PSK; PIN → M4. |
| Stress | ✅ | 2026-06-16 | 30-min 38-ch soak: breadth flat (~2× mainline), no wedge. (Measures breadth, not the strong-AP weakness — see Scan.) |

→ [RTL8822BU_DKMS.md](src/wifit3/chips/rtl8822bu_dkms/RTL8822BU_DKMS.md) (default) · [RTL8822BU.md](src/wifit3/chips/rtl8822bu/RTL8822BU.md) (mainline)

### RTL8814AU
*ALFA AWUS1900 · 2.4 / 5 GHz · 4T4R*

> **Default = vendor/DKMS port** (`WIFIT3_RTL8814=mainline` falls back) — but moot here:
> both ports behave identically at the reference (see Scan). The earlier flip rationale was
> an RSSI-*readout* difference, not better reception.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ❌ | 2026-06-17 | Fails the reference bar — strongest near AP decays ~5→2 bcn/s over 60 s (RX front-end overload), both ports. Aggregate fine (~63/s). 5 GHz not re-tested. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| Stress | ❌ | 2026-06-17 | Strong-near-AP decay shows within a single 60 s window (see Scan) — sustained target RX isn't trustworthy. |

→ [RTL8814AU.md](src/wifit3/chips/rtw88_8814au/RTL8814AU.md) (mainline) · [RTL8814AU_DKMS.md](src/wifit3/chips/rtl8814au_dkms/RTL8814AU_DKMS.md) (default)

### MT7612U
*ALFA AWUS036ACM · 2.4 / 5 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-31 | Full M1–M4. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, no degradation. |

→ [MT76X2U.md](src/wifit3/chips/mt76x2u/MT76X2U.md)

### MT7610U
*ALFA AWUS036ACHM · 2.4 / 5 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-16 | 2.4 + 5 GHz healthy; the Focus-entry tune glitch (0 beacons until re-enter) fixed by synchronous hopping (`b2bf17c`). |
| Handshake | ✅ | 2026-05-31 | M1+M2. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-16 | 30-min 22-ch dual-band soak, no degradation. |

→ [MT76X0U.md](src/wifit3/chips/mt76x0u/MT76X0U.md)

### RT5372
*Panda PAU05 + PAU06 · 2.4 GHz · 2T2R*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-10 | Healthy ~8.5/s (max 10), top-ranked; ~2× the rt2800usb imitation's breadth. Warm reattach works (skips FW/init, stays healthy). |
| Deauth | ✅ | 2026-06-10 | Live deauth → reconnect EAPOL; TX byte-matches the kernel's wire deauth (only seqctl differs). |
| Handshake | ✅ | 2026-06-10 | Deauth → 4-way (~27 EAPOL/30 s). |
| PMKID | ✅ | 2026-06-10 | Capture + active extract. |
| WEP | ✅ | 2026-06-10 | ARP replay + ChopChop. |
| WPS | ✅ | 2026-06-10 | PIN + PBC. |
| Stress | ✅ | 2026-06-10 | 30-min 14-ch soak on both PAU05 + PAU06: no wedge, flat; attacks pass post-soak. |

→ [RT5372.md](src/wifit3/chips/rt5372/RT5372.md) (default) · [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md) (rt2800usb fallback)

### RT5572
*Panda PAU09 N600 · 2.4 / 5 GHz · 2T2R*

The best-behaved Ralink — snappy, great beacon rate, balanced 2.4/5 GHz RX.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Balanced RX, both bands. |
| Handshake | ✅ | 2026-05-31 | Full M1–M4. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-10 | 30-min 22-ch dual-band soak: no wedge, flat (0.95), both bands held; attacks pass post-soak. |

→ [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md)

### RT3070
*ALFA AWUS036NH · 2.4 GHz · 1T1R*

Excellent 2.4 GHz front-end (external LNA) — strong range, signal, and TX rate.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-09 | Kernel-parity ~8.4 bcn/s (vs kernel's 8.9), zero gaps. An earlier sustained-attack RX-DMA wedge is fixed (`d425550` + regression test). |
| Deauth | ✅ | 2026-06-09 | Live deauth dropped a client; TX byte-matches aireplay-ng's wire deauth (incrementing seqctl). |
| Handshake | ✅ | 2026-06-09 | Deauth → reconnect → 39 EAPOL/30 s, M1–M4. |
| PMKID | ✅ | 2026-06-09 | Passive capture + active extract. |
| WEP | ✅ | 2026-06-09 | Replay + ChopChop ~300 inj/s — 20k IVs cracked in <90 s (best WEP throughput so far). |
| WPS | ✅ | 2026-06-09 | PIN → M4; PBC → PSK. |
| Stress | ✅ | 2026-06-09 | 30-min 14-ch soak: flat (median 57→62), attacks pass. |

→ [RT3070.md](src/wifit3/chips/rt3070/RT3070.md)

### RT2500USB
*Buffalo Nintendo Wi-Fi / RT2570 · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-11 | ~9 beacons/s on the best AP, 10+ APs, no dead seconds. |
| Handshake | ✅ | 2026-06-11 | Deauth → reconnect; M1+M2+M3 (crackable pair). |
| PMKID | ✅ | 2026-06-11 | Passive + active extract. |
| WEP | ✅ | 2026-06-11 | ChopChop (4 tries); ARP replay works, slow TX (~60 IVs/s). |
| WPS | ✅ | 2026-06-11 | PBC → PSK; PIN → M4. |
| Stress | ✅ | 2026-06-11 | 30-min 14-ch soak: no wedge, no RF death. Mild breadth taper late. |

→ [RT2500USB.md](src/wifit3/chips/rt2500usb/RT2500USB.md)

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

## Fully supported

Every column ✅ *plus* a clean Stress soak. **Ten cards: RTL8812AU (DKMS), AR9271,
RTL8821AU (DKMS), MT7612U, MT7610U, RT3070, RT5372, RT5572, RT2500USB, and RTL8187L** —
every Ralink we have (RT2500USB / RT3070 / RT5372 / RT5572) now at full marks.

## Hardware queue

*"Will you support card X?"* — maybe. A chipset gets added when we have the adapter in
hand **and** a clean cold-boot USB capture to port against (the process is
`planning/PORTING.md`). Good candidates are the USB adapters morrownr recommends for
Kali: <https://github.com/morrownr/USB-WiFi/blob/main/home/Recommended_Adapters_for_Kali_Linux.md>

**En route:** nothing right now — every adapter we have is delivered and ported.

**Wishlist (not yet bought):**

- **TP-Link Archer T2U Plus** — RTL8821AU / RTL8811AU.
- **Generic MT7601U** — cheapest dongle; known for awkward packet injection.
