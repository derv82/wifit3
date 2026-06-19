# Wifit3 — Hardware Verification

Wifit3 drives these USB radios directly and (mostly) correctly by imitating Linux drivers.

- Some drivers are a complete byte-perfect port of a known-good driver.
- Other drivers are merely imitating a driver, performing only the bare minimum hardware operations to achieve a functioning wireless device.

The matrix below captures *how well wifit3 drives these wireless cards* -- Every blemish is either a documented bug in Wifit3 or a severe hardware limitation.

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

Scan + Deauth work on every supported card unless a note says otherwise, so the
tables below lead with the attack columns and any caveats.

### AR9271
*ALFA AWUS036NHA · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-25 | Full M1–M4, warm + cold. |
| PMKID | ✅ | 2026-05-25 | First-try on cold boot, vs real APs. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-05 | 30-min soak (2.4 GHz, 13-ch hop), no degradation. |

→ [AR9271.md](src/wifit3/chips/ar9271/AR9271.md)

### RTL8187L
*ALFA AWUS036H · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-12 | Healthy — 215–323 frames/s, real RSSI spread. Always cold-inits (the radio doesn't survive a handle reopen). |
| Handshake | ✅ | 2026-06-12 | Deauth → 4-way; ~3/4 M1–M4. |
| PMKID | ✅ | 2026-06-12 | Passive + active extract. |
| WEP | ✅ | 2026-06-12 | FakeAuth + ARP replay (~150–200 IVs/s) + ChopChop. |
| WPS | ✅ | 2026-06-12 | PIN + PBC. (The L-path has no hardware sequence assignment, so inject stamps the 802.11 seq in software like the kernel; without it the AP deduped the multi-frame EAP exchange.) |
| Stress | ✅ | 2026-06-11 | 30-min 13-ch soak (0.25s hops): no wedge, no degradation trend. |

→ [RTL8187L.md](src/wifit3/chips/rtl8187/RTL8187L.md)

### RTL8188EUS
*TP-Link TL-WN722N v2/v3 · 2.4 GHz*

> **Default = vendor/DKMS port** ([RTL8188EUS_DKMS.md](src/wifit3/chips/rtl8188eus_dkms/RTL8188EUS_DKMS.md));
> the table below is that port. RX ties mainline (~6.6 bcn/s both, post-`dc621ce`); the 30-min soak is the
> tie-break — **DKMS holds (1.07) while mainline degrades (0.84)**, the long-session stability the re-port
> was for. `WIFIT3_RTL8188=mainline` opts back.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-06-16 | ~6.6 bcn/s best-AP average (both ports, no dead seconds) — below the 8/s bar. (Old ~1–3/s was the pre-`dc621ce` RX-loop drop, fixed.) Severe audit (`SEVERE-AUDIT.md`) found no port-side divergence in scope — the ceiling is 1T1R silicon + airtime. |
| Handshake | ✅ | 2026-05-19 | Passive 4-way, end-to-end. |
| PMKID | ✅ | 2026-05-19 | Active harvest — instant. |
| WEP | ✅ | 2026-06-16 | ChopChop 32/32 bytes first try; ARP replay 200+ IVs/s. (The old 9/32 stall was the pre-`dc621ce` weak RX.) |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-16 | 30-min 13-ch soak: no degradation (76→81, ratio 1.07), ~71–88 APs/bucket steady, no wedge. Mainline degrades on the same soak (83→70, 0.84) — the reason DKMS is the default. |

→ [RTL8188EUS_DKMS.md](src/wifit3/chips/rtl8188eus_dkms/RTL8188EUS_DKMS.md) (default) · [RTL8188EUS.md](src/wifit3/chips/rtl8188eus/RTL8188EUS.md) (mainline)

### RTL8821AU
*ALFA AWUS036ACS · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** for `0bda:0811` (hotter 2.4 GHz RX, ties 5 GHz;
> set `WIFIT3_RTL8821=mainline` to fall back). A/B + why-it-wins:
> [RTL8821AU_DKMS.md](src/wifit3/chips/rtl8821au_dkms/RTL8821AU_DKMS.md). The table
> below is that port.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-05 | 2.4 + 5 GHz; beacon-watch steady ~9/s, no gaps. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active extract. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, no degradation. |

→ [RTL8821AU.md](src/wifit3/chips/rtl8821au/RTL8821AU.md) (mainline) · [RTL8821AU_DKMS.md](src/wifit3/chips/rtl8821au_dkms/RTL8821AU_DKMS.md) (default)

### RTL8812AU
*ALFA AWUS036ACH · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** ([RTL8812AU_DKMS.md](src/wifit3/chips/rtl8812au_dkms/RTL8812AU_DKMS.md));
> the table below is that port. It survives dual-band channel hopping; the mainline
> driver (`WIFIT3_RTL8812=mainline`) RF-wedges at ~110 s on the same hop and stays a
> fixed-channel fallback.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-05 | 2.4 + 5 GHz; survives the dual-band hop (incl. DFS). |
| Deauth | ✅ | 2026-06-05 | 2.4 + 5 GHz; client dropped + reconnect caught. |
| Handshake | ✅ | 2026-06-05 | M2/M4 (ToDS) — crackable. |
| PMKID | ✅ | 2026-06-05 | Capture + active extract. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| Stress | ✅ | 2026-06-05 | 30-min dual-band soak, no degradation. |

→ [RTL8812AU_DKMS.md](src/wifit3/chips/rtl8812au_dkms/RTL8812AU_DKMS.md) (default) · [RTL8812AU.md](src/wifit3/chips/rtl8812au/RTL8812AU.md) (mainline)

### RTL8822BU
*TP-Link Archer T3U Plus v1 · 2.4 / 5 GHz*

> **Default = vendor/DKMS port** ([RTL8822BU_DKMS.md](src/wifit3/chips/rtl8822bu_dkms/RTL8822BU_DKMS.md);
> `WIFIT3_RTL8822=mainline` opts back). The table below is that port. DKMS earns the default on
> *breadth* — it hears ~2× the 2.4 GHz APs of mainline (a wrong-RX-antenna-mux fix), 5 GHz tied.
> But neither port handles a strong near AP: both saturate on it and tie at ~2.6 bcn/s (see Scan).

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ❌ | 2026-06-17 | Fails the reference bar. Against the strongest near AP (a known-good MT7921AU holds it at 9.6 bcn/s flat, #1-ranked, same spot) the 8822bu hears it *worst* at ~2.6 bcn/s — **both ports tied** (DKMS 2.6 / mainline 2.7). mainline's phy_status logged the cause: `pwdb=253 … chip is saturating` — RX front-end overload on the strong signal. DKMS does hear ~2× mainline's breadth (~56 vs ~27/s, 10 vs 5 APs) — why it's the default, and why the old best-AP "6.5 vs 4.9" read ⚠️ — but breadth ≠ the reference AP. 5 GHz was solid at the ceiling (not re-tested this round). |
| Deauth | ✅ | 2026-06-16 | Dropped a real laptop + phone off the AP. |
| Handshake | ✅ | 2026-06-16 | Deauth → 4-way; full M1–M4. |
| PMKID | ✅ | 2026-06-16 | Passive capture + extract. |
| WEP | ✅ | 2026-06-16 | ChopChop + ARP replay (~225 IVs/s avg). |
| WPS | ✅ | 2026-06-16 | PBC → PSK; PIN → M4 (first-half-wrong). |
| Stress | ✅ | 2026-06-16 | Breadth-stability: 30-min 38-ch soak, no degradation (106→110 active BSSIDs/bucket), 2.4 GHz held 58–72 APs/bucket (~2× mainline), no dropout or wedge. (Measures discovery breadth over time, not the strong-near-AP weakness — see Scan.) |

→ [RTL8822BU_DKMS.md](src/wifit3/chips/rtl8822bu_dkms/RTL8822BU_DKMS.md) (default) · [RTL8822BU.md](src/wifit3/chips/rtl8822bu/RTL8822BU.md) (mainline)

### RTL8814AU
*ALFA AWUS1900 · 2.4 / 5 GHz · 4T4R*

> **Default = vendor/DKMS port** ([RTL8814AU_DKMS.md](src/wifit3/chips/rtl8814au_dkms/RTL8814AU_DKMS.md);
> `WIFIT3_RTL8814=mainline` falls back) — but the port choice is moot here: both behave
> identically at the reference (see Scan). The earlier flip rationale (DKMS reads −45 dBm
> vs mainline's −81) was an RSSI-*readout* difference, not a reception advantage — mainline
> receives the reference AP no worse. The table below is the DKMS port.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ❌ | 2026-06-17 | Fails the reference bar. Against the strongest near AP — a known-good MT7921AU holds it at 9.6 bcn/s flat, top-ranked, same spot + minute — the 8814au hears it *worst* (last of ~10 APs) and decays ~5→2 bcn/s over a 60s fixed-channel soak. Identical on both ports (mainline 2.7 / DKMS 3.2 mean). Aggregate is fine (~63/s flat across all APs) — only the strongest signal rots: the signature of RX front-end overload. The old ⚠️ was the all-APs metric masking it. 5 GHz not re-tested this round. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active extract. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| Stress | ❌ | 2026-06-17 | The strong-near-AP decay appears within a single 60s fixed-channel window (see Scan), so sustained RX on a target AP isn't trustworthy. The old 30-min hop soak read flat (98→98) only because it scored aggregate breadth — the metric, not the card, hid the rot. |

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
| Scan | ✅ | 2026-06-16 | 2.4 + 5 GHz, healthy both bands. The Focus-entry tune glitch (0 beacons until exit+re-enter) is fixed by synchronous hopping (`b2bf17c`) — confirmed gone, couldn't repro. |
| Handshake | ✅ | 2026-05-31 | M1+M2. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-16 | 30-min 22-ch dual-band soak: no degradation (88→100, ratio 1.14), both bands steady (2.4 ~50/bucket, 5 ~40), no wedge. |

→ [MT76X0U.md](src/wifit3/chips/mt76x0u/MT76X0U.md)

### RT5372
*Panda PAU05 + PAU06 · 2.4 GHz · 2T2R*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-10 | Healthy — beacon-watch mean ~8.5/s (median 9, max 10) on the nearby AP, top-ranked, no gaps; ~2× the rt2800usb imitation's breadth on the same card. Warm reattach implemented: a re-run without a replug skips FW + init and resumes RX, staying healthy (8.7/s, steadier than cold) instead of the old re-init-on-warm dip. |
| Deauth | ✅ | 2026-06-10 | Live targeted deauth dropped a real client → reconnect EAPOL. TX frame **byte-matches the kernel's wire deauth** from the capture (TXINFO/TXWI/MPDU/+4-pad; only the per-frame seqctl differs, stamped at inject). |
| Handshake | ✅ | 2026-06-10 | Deauth → 4-way; ~27 EAPOL in 30 s, M2/M4 (ToDS) + M1/M3 (FromDS). |
| PMKID | ✅ | 2026-06-10 | Capture + active extract. |
| WEP | ✅ | 2026-06-10 | ARP replay + ChopChop. |
| WPS | ✅ | 2026-06-10 | PIN + PBC — PBC now works (the imitation's weak RX that failed it is gone). |
| Stress | ✅ | 2026-06-10 | 30-min 14-ch soak (0.25s hops) on **both PAU05 + PAU06**: no wedge, breadth flat within the ±15 bucket swing (no decay trend), ~1% beacon channel-mismatch = hop-boundary only (hopping never stuck). Deauth → handshake still works post-soak with no replug (TX survives the hop marathon + warm state). |

→ [RT5372.md](src/wifit3/chips/rt5372/RT5372.md) (default) · [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md) (rt2800usb fallback)

### RT5572
*Panda PAU09 N600 · 2.4 / 5 GHz · 2T2R*

The best-behaved Ralink — snappy, great beacon rate, and balanced 2.4/5 GHz RX (both
bands read the same power).

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-05-31 | Balanced RX, both bands. |
| Handshake | ✅ | 2026-05-31 | Full M1–M4. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ✅ | 2026-06-10 | 30-min 22-ch dual-band soak (0.25s hops): no wedge, breadth flat (82→78, ratio 0.95), **both bands held the whole run** (5 GHz ~30 APs/bucket, no dropout). Deauth → handshake still works post-soak. |

→ [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md)

### RT3070
*ALFA AWUS036NH · 2.4 GHz · 1T1R*

Excellent 2.4 GHz front-end (external LNA) — strong range, signal, and TX rate.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-09 | Kernel-parity: 8.4 beacons/s live vs the kernel's 8.9 from the same usbmon capture (~86–91% of the 9.77/s single-AP ceiling), zero gaps. An earlier sustained-attack-use falloff (a solid 8–10/s AP decaying to ~2/s, unrecovered by a retune — only by a fresh bring-up) read like AGC drift but was the **RX-DMA wedge**: a UI view switch cancels a channel-hop mid-`set_channel`, the executor thread keeps running `config_channel` after the `asyncio` lock releases, and the next tune's thread collides on the control endpoint → `WPDMA_GLO_CFG`→0 (control alive, RX dead; only re-init recovers, hence the retune not helping). Fixed by serializing device ops under a `threading.Lock` (d425550) + regression test; not reproducible under extended TUI stress since. |
| Deauth | ✅ | 2026-06-09 | Live targeted deauth dropped a real client. TX frame **byte-matches aireplay-ng's wire deauth** (duration `0x013a` + per-frame incrementing seqctl; the constant-seq bug would otherwise let a receiver's dup-filter drop every deauth after the first). |
| Handshake | ✅ | 2026-06-09 | Deauth → reconnect → **39 EAPOL frames** captured in 30s, M2/M4 (ToDS) + M1/M3 (FromDS). |
| PMKID | ✅ | 2026-06-09 | Passive capture + active extract. |
| WEP | ✅ | 2026-06-09 | Replay + ChopChop at **~300 injections/s** — ChopChop → cracked with 20k IVs in **<90s**. Best WEP throughput of any card to date. |
| WPS | ✅ | 2026-06-09 | PIN → M4; PBC → PSK extracted. The protocol path is byte-clean — forged-MAC auto-ACK works (AP unicasts EAPOL back to our `02:..` supplicant MAC) and the EAPOL TX is correct (LLC/SNAP + 0x888E, incrementing seqctl). An earlier run failed purely on the medium — a degraded/contended RX starves the real-time M1–M4 exchange (WPS is the most RX-fragile attack); that was the RX-DMA wedge (Scan above), since fixed — not a WPS bug. |
| Stress | ✅ | 2026-06-09 | 30-min 14-ch soak (`sweep.py --longrun-min 30`, 0.25s hops): flat 54–69 active BSSIDs/bucket, no degradation trend (median 57→62), attacks pass. Ran pre-fix, but linear hops never cancel a tune so the soak doesn't exercise — or threaten — the RX-DMA wedge; that's validated separately (regression test + post-fix TUI stress). |

→ [RT3070.md](src/wifit3/chips/rt3070/RT3070.md)

### RT2500USB
*Buffalo Nintendo Wi-Fi / RT2570 · 2.4 GHz*

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ✅ | 2026-06-11 | ~9 beacons/s on the best AP, 10+ APs, no dead seconds. |
| Handshake | ✅ | 2026-06-11 | Deauth → reconnect; M1+M2+M3 captured (crackable pair). |
| PMKID | ✅ | 2026-06-11 | Passive + active extract. |
| WEP | ✅ | 2026-06-11 | ChopChop forged a packet (4 tries); ARP replay works, slow TX (~60 IVs/s). |
| WPS | ✅ | 2026-06-11 | PBC extracted the PSK; PIN → M4 (first-half-wrong). |
| Stress | ✅ | 2026-06-11 | 30-min 14-ch soak: no wedge, no RF death. Mild breadth taper late in the run. |

→ [RT2500USB.md](src/wifit3/chips/rt2500usb/RT2500USB.md)

## Unsupported

### RT3572 — ALFA AWUS051NH v2 — untested

Our only unit (bought 2015) is a counterfeit with a blank EFUSE — no factory RF
calibration — so it can't validate the chip. The `rt2800usb` driver is shared with
the working RT5372/RT5572, so the port itself stays supported; re-test if a genuine
unit turns up. → [RT2800USB.md](src/wifit3/chips/rt2800usb/RT2800USB.md)

## Stress soak

A **30-minute** sustained-hop soak — `scripts/diag/sweep.py --skip-baseline
--longrun-min 30`, hopping all channels — with the attacks still working afterward.
✅ = no degradation trend across the 60 s buckets *and* post-soak attacks pass.

*Why 30 min, not an hour:* across a dozen cards a 1-hour bar is a full day of
hands-on scanning, and 30 min already resolves the degradation curve — clean runs
stay flat the whole time, and the failures (RT2500USB) show within the first minute.

## Fully supported

Every column ✅ *plus* a clean Stress soak. **Ten cards are there: RTL8812AU (DKMS),
AR9271, RTL8821AU (DKMS), MT7612U, MT7610U, RT3070, RT5372, RT5572, RT2500USB, and RTL8187L** — every
Ralink we have (RT2500USB / RT3070 / RT5372 / RT5572) now at full marks.

## Hardware queue

*"Will you support card X?"* — maybe. A chipset gets added when we have the adapter in
hand **and** a clean cold-boot USB capture to port against (the process is
`planning/PORTING.md`). Good candidates are the USB adapters morrownr recommends for
Kali: <https://github.com/morrownr/USB-WiFi/blob/main/home/Recommended_Adapters_for_Kali_Linux.md>

**En route:** nothing right now — every adapter we have is delivered and ported.

**Wishlist (not yet bought):**

- **TP-Link Archer T2U Plus** — RTL8821AU / RTL8811AU.
- **Generic MT7601U** — cheapest dongle; known for awkward packet injection.
