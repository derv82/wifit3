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
| [RTL8822BU](#rtl8822bu) | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ | B |
| [MT7610U](#mt7610u) | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ⬜ | B |
| [RTL8188EUS](#rtl8188eus) | ⚠️ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⬜ | C |
| [RTL8814AU](#rtl8814au) | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | C |

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

> Default = the mainline-derived port; `WIFIT3_RTL8188=dkms` switches to the vendor
> DKMS port (stronger 2.4 GHz RX in A/B — 86–89% vs ~77%), worth trying given the
> weak Scan below.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-05-31 | Weak 2.4 GHz RX — ~1–3 beacons/s from a router *inches* away (healthy ~10/s). Smells like a gain/DIG bug; worth chasing. |
| Handshake | ✅ | 2026-05-19 | Passive 4-way, end-to-end. |
| PMKID | ✅ | 2026-05-19 | Active harvest — instant. |
| WEP | ⚠️ | 2026-05-31 | Replay ✅. ChopChop stalled at 9/32 bytes — same weak RX (TX side is fine, so kept ⚠️ not ❌). |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ⬜ | — | Not run. |

→ [RTL8188EUS.md](src/wifit3/chips/rtl8188eus/RTL8188EUS.md)

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

> **Vendor/DKMS port** ([RTL8822BU_DKMS.md](src/wifit3/chips/rtl8822bu_dkms/RTL8822BU_DKMS.md)) —
> the table below is that port. It fixes mainline's weak 2.4 GHz monitor RX (a wrong RX antenna
> mux): same-spot A/B is +32% frames / +33% beacon rate on 2.4 GHz, tied on 5 GHz. Opt in with
> `WIFIT3_RTL8822=dkms`; the default flips from mainline once the 30-min stress soak ties/beats it.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-06-16 | 5 GHz rock-solid at the ~9.8/s ceiling. 2.4 GHz much improved over mainline (6.5 vs 4.9 bcn/s, no dead seconds) but still below the 8–10/s bar — feels weak on 2.4. |
| Deauth | ✅ | 2026-06-16 | Dropped a real laptop + phone off the AP. |
| Handshake | ✅ | 2026-06-16 | Deauth → 4-way; full M1–M4. |
| PMKID | ✅ | 2026-06-16 | Passive capture + extract. |
| WEP | ✅ | 2026-06-16 | ChopChop + ARP replay (~225 IVs/s avg). |
| WPS | ✅ | 2026-06-16 | PBC → PSK; PIN → M4 (first-half-wrong). |
| Stress | ⬜ | — | Soak not yet run (the card soaks clean on mainline — no degradation over 30 min). |

→ [RTL8822BU_DKMS.md](src/wifit3/chips/rtl8822bu_dkms/RTL8822BU_DKMS.md) (vendor) · [RTL8822BU.md](src/wifit3/chips/rtl8822bu/RTL8822BU.md) (mainline)

### RTL8814AU
*ALFA AWUS1900 · 2.4 / 5 GHz · 4T4R*

> **Default = vendor/DKMS port** ([RTL8814AU_DKMS.md](src/wifit3/chips/rtl8814au_dkms/RTL8814AU_DKMS.md);
> `WIFIT3_RTL8814=mainline` falls back). DKMS fixes the mainline 2.4 GHz signal
> miscalibration (−45 dBm vs mainline's −81), but 2.4 GHz RX still drops out
> intermittently under sustained hopping (see Scan/Stress). The table below is that port.

| Capability | Status | Date | Notes |
|---|:--:|---|---|
| Scan | ⚠️ | 2026-06-05 | 5 GHz solid. 2.4 GHz RX intermittent under sustained hopping — a 30-min soak hit a full 60s with zero 2.4 GHz APs (5 GHz fine); OK in short / fixed-channel use. |
| Handshake | ✅ | 2026-06-05 | Deauth → 4-way. |
| PMKID | ✅ | 2026-06-05 | Passive + active extract. |
| WEP | ✅ | 2026-06-05 | Replay + ChopChop. |
| WPS | ✅ | 2026-06-05 | PIN + PBC. |
| Stress | ⚠️ | 2026-06-05 | Survives 30 min (no progressive degradation, 98→98; 5 GHz flat), but 2.4 GHz RX intermittently drops out under sustained hop — one full 60s bucket of zero 2.4 GHz APs, periodic dips, lowest/jitteriest frame rate of the soaked cards. |

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
| Scan | ⚠️ | 2026-05-31 | Focus-entry tune glitch: 0 beacons/s on entering Focus until you exit + re-enter. |
| Handshake | ✅ | 2026-05-31 | M1+M2. |
| PMKID | ✅ | 2026-05-31 | Passive + active. |
| WEP | ✅ | 2026-05-31 | Replay + ChopChop. |
| WPS | ✅ | 2026-05-31 | PIN + PBC. |
| Stress | ⬜ | — | Not run. |

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

Every column ✅ *plus* a clean Stress soak. **Nine cards are there: RTL8812AU (DKMS),
AR9271, RTL8821AU (DKMS), MT7612U, RT3070, RT5372, RT5572, RT2500USB, and RTL8187L** — every
Ralink we have (RT2500USB / RT3070 / RT5372 / RT5572) now at full marks.
