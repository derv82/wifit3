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
| AR9271 | ✅ | ✅ | ✅ | ✅ | ⬜ | ⬜ | ⬜ |
| RTL8187L | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| RTL8188EUS | ✅ | ✅ | ✅ | ✅ | ⬜ | ⬜ | ⬜ |
| RTL8821AU | ✅ | ✅ | ✅ | ⬜ | ⚠️ | ⚠️ | ⬜ |
| RTL8812AU | ✅ | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⚠️ |
| RTL8822BU | ✅ | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⬜ |
| RTL8814AU | ✅ | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⬜ |
| MT7612U | ✅ | ✅ | ✅ | ⬜ | ⚠️ | ⬜ | ⬜ |
| MT7610U | ✅ | ✅ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| RT2800USB | ✅ | ✅ | ⚠️ | ⬜ | ⬜ | ⬜ | ⬜ |
| RT2500USB | ✅ | ✅ | ⚠️ | ✅ | ⬜ | ⬜ | ⬜ |

### README asterisk rule

A chipset carries a `*` in the README's supported-hardware table when it has a
**❌, or a ⚠️ that is a genuine hardware/driver limitation of that card** (RF
hop-death, an unburned-EFUSE test unit, partial-direction capture). A ⚠️ that
only reflects **incomplete attack-suite coverage** (the attack works where it
was run, just not fully exercised on that card) does **not** asterisk — the card
is fine, the testing is simply unfinished.

Asterisked today: **RTL8812AU, RT2800USB, RT2500USB**.

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
| WEP | ⬜ | — | Not run on this card. |
| WPS | ⬜ | — | Not run on this card. |
| Stress | ⬜ | — | A 1,094-pkt calibration marathon ran clean, but no attack-soak endurance test. |

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
| Handshake | ✅ | 2026-05-25 | Full M1–M4 after the RX-poll reader-thread + ToDS-filter (`0xf410400f`) fixes. |
| PMKID | ⬜ | — | Engine is production-complete (NEXT-STEPS) and this is the main dev card, but no PMKID-specific HW run is *dated* here — left untested pending a citation. |
| WEP | ⚠️ | 2026-05-24 | ARP-replay + native PTW + Fragmentation live-cracked the dd-wrt WEP box (`en_hwseq=0` sw-seq fix). ChopChop oracle HW-verified; the ChopChop daemon's end-to-end run is still pending. |
| WPS | ⚠️ | 2026-05-27 | Single-PIN crack walked M1→M7 + recovered the PSK on a WPS test AP; PBC capture verified through P3. Pending: full multi-attempt `--campaign` sweep, multi-router lock matrix, and PixieWPS (not built). |
| Stress | ⬜ | — | Not run. |

→ `chips/rtl8821au/RTL8821AU.md`, `engine/attacks/wep/README.md`, `engine/attacks/wps/README.md`

### RTL8812AU — ALFA AWUS036ACH (2.4 / 5 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-31 | M2/M4 captured live after the ToDS-filter fix (with M1/M3 → full 4-way). RX-DMA aggregation arm fixed the ~5 s bulk-IN cliff. |
| PMKID | ⬜ | — | Not separately recorded. |
| WEP | ⬜ | — | Not run on this card. |
| WPS | ⬜ | — | Not run on this card. |
| Stress | ⚠️ | 2026-05-31 | **RF-synth hop-death** (rtw88-inherited HW limit, in-tree driver has it too): single channel survives 30 min+, but sustained dual-band 0.25 s hopping wedges RX after seconds-to-minutes. Mitigation (`dynamic.py`: DIG + pwr-track + decoupled LCK) delays it ~2–4× but doesn't eliminate it; no userland recovery — the user replugs. |

→ `chips/rtl8812au/RTL8812AU.md`

### RTL8822BU — TP-Link Archer T3U Plus v1 (2.4 / 5 GHz)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-25 | Full M1–M4 captured (HW-confirmed); completion held 3/3 (one benign missed M3 *retransmit*). |
| PMKID | ⬜ | — | Not separately recorded. |
| WEP | ⬜ | — | Not run. |
| WPS | ⬜ | — | Not run. |
| Stress | ⬜ | — | Not run. |

→ `chips/rtl8822bu/RTL8822BU.md`

### RTL8814AU — ALFA AWUS1900 (2.4 / 5 GHz, 4T4R)

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ✅ | 2026-05-26 | Deauth kicks a phone off and EAPOL re-capture is HW-confirmed. RX path complete + pcap-byte-validated; 0/100 cold boots deaf. |
| PMKID | ⬜ | — | Not separately recorded. |
| WEP | ⬜ | — | Not run. |
| WPS | ⬜ | — | Not run. |
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

### RT2800USB — Panda PAU05 (RT5372) / PAU09 N600 (RT5572) / ALFA AWUS051NH v2 (RT3572) (2.4 / 5 GHz)

Three silicons share this driver; status differs per unit.

| Capability | Status | Date | Details |
|---|:--:|---|---|
| Handshake | ⚠️ | — | Full attack chain verified live on a burned silicon (deauth → EAPOL M2+M3). **The AWUS051NH v2 (RT3572) test unit has an erased EFUSE** → TX hardware-limited and end-to-end capture *on that unit* isn't validated (a properly-burned RT3572 is unaffected). |
| PMKID | ⬜ | — | RT3572 unit's PMKID is **not validated** (under investigation, likely a frame-parser issue); burned-silicon PMKID not separately recorded. |
| WEP | ⬜ | — | WEP ARP-replay was noted dead on the RT5572 (zeroed-IV injection); whether fully fixed is unconfirmed → untested. |
| WPS | ⬜ | — | Not run. |
| Stress | ⬜ | — | Not run. |

→ `chips/rt2800usb/RT2800USB.md`

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

## Stress / endurance — protocol TBD

No standard soak test exists yet, so Stress is ⬜ almost everywhere. Before this
column means anything, define what "passes": e.g. N minutes of continuous
channel-hop + capture while watching for thermal shutdown, RX going deaf, USB
pipe wedge, and host-side memory growth. The one populated cell (RTL8812AU ⚠️) is
a documented RF hop-death limit, not the output of a soak harness.

## What "Fully supported" will mean

A chipset is **fully supported** when every attack column is ✅ *and* it clears
the (future) Stress soak. Today the wall is: bring-up + deauth solid everywhere,
handshake confirmed on most cards, PMKID confirmed on a few, WEP/WPS proven on
the RTL8821AU reference card, and Stress essentially unstarted.
