# Wifit3 — Porting & Hardware Enablement

Forward-looking. Everything about getting drivers and hardware working: the
**porting playbook** (how we do it) and the **queue** (what's pending). Current
per-card verification state lives in `../VERIFICATION.md`; release logistics in
`RELEASE-PLAN.md`; product/UX features in `FEATURES.md`.

---

## Porting playbook

How a chipset gets ported, distilled. The principles are load-bearing — most
port bugs trace to skipping one of them.

### Pcap is ground truth; source explains *why*

When porting a kernel driver, the kernel C is the **spec**, but the cold-boot
`usb_dumps/captures_<driver>/capture-N.pcap` is the **ground truth**. Every USB
transfer in the working capture must exist in the port; the source explains why
each one is there (and gives the real register/macro/function names — never type
constants from memory, grep them out of `data_dumps/<chip-source>/` and paste
verbatim). Where the two disagree, the wire wins.

Deterministic helpers (see `scripts/AGENTS.md` for the full brief):
- `python scripts/pcap_slicer.py <main.log> <pcap>` — maps `capture.py` log
  timestamps to pcap frame ranges (e.g. "firmware upload happens in frames
  14182–14400"). Pick the cold-boot capture.
- `Grep` / `Read` directly against `data_dumps/<chip-source>/` for register and
  macro lookups (e.g. `Grep '#define\s+REG_FOO' data_dumps/rtw88-source-v6.18/`).

Captures are produced by `src/wifit3/scripts/capture.py` on the Kali persistent
USB; each capture ships a `*_logs/main.log` (absolute-epoch timeline) that
`pcap_slicer.py` consumes.

### Develop in the hardware loop

Hardware testing is the **user's** job, not the agent's. The loop:
1. Agent proposes code changes.
2. User runs `python scripts/<chipset>/test_hw_<chipset>.py` (optionally
   `--debug`) and pastes output.
3. Agent reads output, iterates.

Device borked? The user replugs (resets cold-boot state). Never try to
flash/test hardware from the agent side.

### Hardware-verifiable milestones

Structure a bring-up as a sequence of small, demoable milestones, each
**pcap-diffed before declaring it done** — don't port a whole driver blind and
hope. The canonical first milestone:

- **M1 = firmware upload + FW_READY ACK only.** Demoable, no PHY init.

Then layer PHY/MAC/RF init, channel tune, RX, TX as subsequent M's, diffing the
generated USB sequence against the cold-boot capture at each step.

### Scope: 20 MHz primary channel only

Do **not** port the kernel's 40/80 MHz channel-width path (`bw=1/2`, the
`ch_group_index` offset math, the secondary-channel + per-width `EXT_CCA`-group
setup). wifit3 only ever tunes the 20 MHz primary — every frame it captures
(beacons, EAPOL, WEP IVs) and transmits (deauth/replay) rides the primary at
legacy rates, so 40/80 buys nothing and is pure port surface.
(See `chips/mt76x2u/MT76X2U.md` → "Channel width — 20 MHz only".)

### Bringing up a fresh card — recipe

When new cold-boot captures land in `usb_dumps/captures_<driver>/`
(`capture-N.pcap` + `capture-N_logs/main.log`):

1. `pcap_slicer.py <main.log> <pcap>` — map "plug-in → FW load → channel hop →
   packets" to frame ranges. Pick the cold-boot capture.
2. Pull pristine kernel source into `data_dumps/<driver>-source-v6.18/` (matches
   Kali's runtime kernel, keeps `[SRC]` cites version-aligned).
3. Extract the FW blob from the cold-boot pcap, byte-verify against
   `linux-firmware/`, ship it in `chips/<driver>/assets/`.
4. M1 = FW upload + FW_READY ACK only. Demoable, no PHY init.
5. Drop a `<CHIP>.md` ground-truth doc next to the driver with `[SRC]`/`[WIRE]`
   citations; accumulate verified facts there as they're confirmed.

The structural requirements a driver must satisfy (the `WlanDriver` Protocol,
`_all_drivers()` registration) live in `CLAUDE.md` → "Adding a New Chipset".

---

## Cleanroom DKMS re-ports (the 2.4 GHz RX fix)

The four Realtek 11ac-family cards (8822bu, 8814au, 8821au, 8812au) are currently
mainline-`rtw88`-derived and share `chips/rtw88_base/`. They inherit mainline's
weak 2.4 GHz monitor RX. The fix is to **re-port each from its vendor (DKMS)
source, cleanroom**.

### Why — the 2.4 GHz RX weakness, and the A/B that pinned it

Hardware testing showed weaker-than-expected 2.4 GHz reception across the
rtw88 family: RTL8814AU (severe), RTL8821AU (~7/s for a close router), RTL8822BU
(a 5 GHz AP at −50 dBm but a 2.4 GHz AP at −81 dBm at the same spot — ~31 dB,
backwards from physics, since 2.4 GHz should carry *better*). All share
`rtw88_base`, pointing at a shared 2.4 GHz RX path rather than three independent
bugs.

The **Kali A/B (2026-06-01, `usb_dumps_new/`)** ran the same physical cards on
their *in-kernel* driver and on their *vendor DKMS* driver, fixed-channel:

- **RTL8822BU is the clean A/B:** mainline **8 APs → DKMS 29** (3.6×).
- **RTL8814AU mainline is noisy** — one run collapsed to 1 AP (a bad run: even
  its strongest AP caught 21 beacons vs a healthy ~140), an older mainline
  capture heard 11 — while DKMS robustly hears **21–24**. (Don't cite 8814au
  mainline as "deaf" — it's noisy, not dead.)

So the deficit lives in **mainline's 2.4 GHz monitor RX path (AGC/DIG)**, which
our mainline-derived port faithfully inherits — not the hardware, not PyUSB. The
RT5572 (rt2800usb) reads 2.4 and 5 GHz at the *same* power with excellent beacon
rates, so weak 2.4 GHz RX is **not** universal across userland drivers — it's
this family. Vendor and mainline are **completely different codebases** (mainline
`rtw_phy_dig()` in `phy.c` vs the Realtek PHYDM/ODM stack `hal/phydm/phydm_dig.c`)
— same silicon and registers, every layer above them different. Vendor drivers
also carry the long-session stability (sustained AGC/DIG, thermal) a 15 s
snapshot can't even measure.

(The in-tree MediaTek / Atheros / RTL8187 cards are unaffected — no vendor fork
exists, mainline is canonical, our source already matches. Background:
`usb_dumps_new/DRIVER-STATUS.md`.)

### Shared workflow (per card)

1. Branch `dkms/<module>` (e.g. `dkms/88x2bu`).
2. **Keep BOTH drivers — do NOT delete the mainline-derived `chips/<driver>/`.**
   The vendor port lands in a *sibling* package (`chips/rtl<chip>_dkms/`, e.g.
   `chips/rtl8814au_dkms/`); the mainline-derived port stays put. Both register in
   `wlan/manager.py` for the same VID:PID, ordered by a per-family env var
   (`WIFIT3_RTL<chip>`, read fresh each call so it flips between runs without a
   restart): the DKMS port is the default, `=mainline` opts back to the mainline
   driver. This keeps a one-env-var A/B at users' fingertips and unblocks anyone
   whose card setup the mainline/legacy path happens to fix — it gives users
   OPTIONS. Only retire a driver once it's *confirmed* to add no value
   (significantly worse on every axis than its sibling); even then, git history is
   the record, so a delete is rarely worth it. (A future splash-screen driver
   picker is far off — don't design for it now.)
3. Port in a **fresh session** with only the vendor source (`driver-source/`) and
   the new cold-boot pcap in view — mainline driver *and* the mainline-derived
   Python kept entirely out of context, so the new port is faithful to the
   vendor code, not a mainline/vendor hybrid. Treat as a new bring-up (recipe
   above). Capture the live mainline baseline (max beacons/s + nAPs) first — it's
   the A/B target the vendor port must tie-or-beat.
4. **The default flips to the vendor port only once it's HW-proven** to tie/beat
   the mainline on breadth + stability (set the `WIFIT3_RTL<chip>` default ordering
   in the manager). The mainline port remains the env-var fallback indefinitely.
   `rtw88_base/` stays as long as any mainline-derived family driver still imports
   it.

All four vendor sources are in `usb_dumps_new/driver-sources/` (tarballs) +
extracted `usb_dumps_new/captures_*/driver-source/`.

### Priority queue (by measured payoff)

**1. RTL8822BU — highest payoff.**
- Current `chips/rtl8822bu/` (mainline rtw88, uses `rtw88_base`).
- Vendor: morrownr `88x2bu-20210702` 5.13.1 (PR #264 6.18-compat), module
  `88x2bu` — `captures_rtl88x2bu/driver-source/` +
  `driver-sources/rtl88x2bu-5.13.1.tar.xz`.
- Gain **8 → 29 APs** (3.6×) fixed ch1; prime suspect of the RX weakness.
  Mainline A/B: `captures_rtw88_8822bu/`. Branch `dkms/88x2bu`.

**2. RTL8814AU — breadth + throughput win.**
- Current `chips/rtw88_8814au/` (mainline rtw88, uses `rtw88_base`).
- Vendor: morrownr `8814au` 5.8.5.1, module `8814au` —
  `captures_rtl8814au/driver-source/` + `driver-sources/rtl8814au-5.8.5.1.tar.xz`.
- Gain: DKMS robustly hears **21–24 APs** (30 by usbmon, best AP 168 beacons) vs
  mainline's noisy breadth (1 AP on a bad run, 11 on a healthier one). Mainline
  A/B: `captures_rtw88_8814au/`. Branch `dkms/8814au`.

**3. RTL8821AU — stability + carries 8812au (2-for-1).**
- Current `chips/rtl8821au/` (uses `rtw88_base`; **only** driver with working
  SW-seq fragmentation — see the note on this below).
- Vendor: Lucid-Duck `8821au-20210708` 5.12.5.2 (branch `kernel-6.18-compat`),
  module `8821au` — `captures_rtl8821au/driver-source/` +
  `driver-sources/rtl8821au-5.12.5.2.tar.xz` +
  `kernel-6.18-compat-rtl8821au.patch`. **This is the multi-chip rtl88xxau driver
  — it implements 8812au too.**
- Gain: 8821au's own breadth is **tied** (mainline 26 ≈ DKMS 20–26, a stability
  play), but the same port **also delivers 8812au**, which IS bottom-tier
  (8–10 APs) and should be lifted by the shared phydm RX/AGC. That 2-for-1 raises
  its value above its own tied breadth. A/B: `captures_rtw88_8821au/`. Branch
  `dkms/8821au`.
- **Scope decision (2026-06-04):** port 8821au **standalone** into
  `chips/rtl8821au_dkms/` (sibling to the untouched mainline `chips/rtl8821au/`;
  env var `WIFIT3_RTL8821`). **No shared `rtl88xxau_base/` yet** — a base with one
  consumer is planning too far ahead, and an 8821au-bad / 8812au-good split would
  strand it. Leave `# TODO(8812au)` breadcrumbs where the vendor source branches on
  chip (RF path count 1×1 vs 2×2, RFE options, pwr-seq table, FW blob, per-rate
  txpower tables) — one driver referencing the next is mildly smelly but justified,
  since the whole reason to port 8821au is the 8812au. Whether 8812au later shares a
  base, rides this port as a sibling, or gets its own `chips/rtl8812au_dkms/` is
  decided **after** the 8821au A/B. Live mainline baseline (2026-06-04, ch1/30s):
  22 APs, 2727 beacons. **A/B canary = `NETGEAR2G` (`aa:bb:cc:dd:ee:01`)** — a strong
  nearby AP; track its RSSI + beacons/s as the DIG-health indicator (baseline
  ~7.7/s, below its ~9–10/s healthy rate). Full methodology +
  deliberately-committed-PII note in `chips/rtl8821au_dkms/RTL8821AU_DKMS.md`.

**4. RTL8812AU — rides the 8821au vendor port (88xxA sibling).**
- Current `chips/rtl8812au/` (88xxA family with 8821au; shares `rtw88_base`).
- **No separate effort, no kernel-C work.** The 8821au vendor source above is the
  multi-chip rtl88xxau driver — it implements **8812au in the same tree**
  (`hal/rtl8812a/`, `halrf_8812a_*`, `Hal8812PhyCfg`, `phydm_rtl8812a.c`,
  `rtl8812au_recv/xmit.c`). How 8812au reuses that work — shared base,
  sibling-in-the-same-port, or its own `chips/rtl8812au_dkms/` — is **deferred to
  the post-8821au-A/B decision** (see item 3); the standalone 8821au port leaves
  `# TODO(8812au)` breadcrumbs to make whichever path cheap. Either way the
  8812a-specific RF/power tables port 1:1 from the same tree. Expected to lift its
  bottom-tier breadth (8–10 APs) the way the vendor RX lifts 8822bu — *unverified
  for 8812au specifically*.
- Caveat: no *vendor* bring-up capture for 8812au (DKMS never built standalone on
  6.18 — `captures_rtw88_8812au/` is the mainline run), so it leans on the 8821au
  vendor capture for the shared 88xxA init flow + its own mainline capture to
  cross-check chip params. We port the vendor *source*; it needn't build on Kali.

**5. RTL8188EUS — deferred (likely skip).**
- Current `chips/rtl8188eus/` — a **working** mainline-derived port; rtl8xxxu
  family, **not** rtw88, so it doesn't even gate `rtw88_base` retirement.
- Vendor source on hand (`captures_8188eu/driver-source/`, aircrack-ng/gglluukk
  `rtl8188eus` 5.3.9, module `8188eu`).
- **Breadth is tied** (DKMS 19–26 ≈ mainline `rtl8xxxu` 20) — no measured win,
  and the only claimed upside (monitor/injection "robustness") is unquantified.
  A from-scratch port that risks **regressing a working driver for no proven
  gain** isn't worth it. **Port only if a concrete, measurable win appears** (e.g.
  an injection-reliability A/B we can show). A/B baseline: `captures_rtl8xxxu/`.

> **8821au SW-seq fragmentation:** historically the 8821au was the one card with
> a working `en_hwseq=0` software-sequence TX path (used by WEP fragmentation).
> That one-card special-case is being **removed** (one card arbitrarily owning
> one feature is a smell; WEP fragmentation is dropped — ARP-replay + ChopChop
> carry WEP). The re-port should **not** reintroduce a `SUPPORTS_SW_SEQ`
> mechanism.

---

## Blank-EEPROM override (rt2800usb — RT3572 rescue + no-EFUSE cards)

**Idea:** when a card's EFUSE/EEPROM reads blank, substitute a known-good
512-byte image into the *in-RAM* parsed struct so the chip is configured from
sane values instead of all-`0xFF`. Subsumes the deferred "93C66 EEPROM fallback"
item in `RT2800USB.md` — same need (no usable on-chip config), one mechanism.

**Soft override only — never burn fuses.** EFUSE is one-time-programmable (bits
blow `0→1` permanently); a wrong burn bricks the card or sets an illegal
RF/regulatory state with no undo. We do **not** write hardware — only replace the
values the driver reads into RAM at init (`efuse.py` already parses the EFUSE into
a struct; feed it our image instead when blank). Fully reversible, zero risk.

**Design (discuss class shape with lead before coding):** an `EepromOverride`
source in `efuse.py` — detect blank (identity programmed but RF/cal region
`0xFF`, `NIC_CONF0 == 0`), load a 512-byte image, produce the *same*
`EepromValues` the normal parser yields. Gate behind an **explicit flag/CLI
opt-in** so it never silently fakes calibration on a healthy card — surfacing fake
cal as real is worse than a known-weak card. Image provenance: kernel `rt2800`
defaults, or a dump from a genuine RT3572 if one is ever acquired.

**Honest expectations (a gamble, but worth building regardless):**
- **TX should improve** — power is stuck at the low fallback (`RFCSR12=0x6b`, max
  attenuation) *because* the EFUSE reads blank; a good image with real
  `default_power` lifts it. Clear potential win.
- **Per-unit RF cal can't be faked** — crystal/freq trim + power cal are measured
  per individual card at the factory. A generic image is *better than blank* but
  has the wrong trim for this die; on counterfeit silicon those values were never
  measured.
- **RX is the open question** — the rx-filter cal is a *runtime* loopback sweep
  (RFCSR24/BBP55), not an EFUSE value, and it **rails** on the counterfeit unit.
  Either (A) the blank EFUSE mis-configures the front-end earlier → loopback dies
  → rail (a good image might revive RX), or (B) the counterfeit front-end is just
  bad and no image helps. Unknown until tested.
- **Worst case:** counterfeit silicon doesn't respond and nothing changes.

**Experiment:** inject a plausible image into the RAM struct on the RT3572, A/B
the beacon rate + deauth strength vs blank. Low cost, real learning; builds the
genuine-no-EFUSE-card feature either way. If it meaningfully rescues the unit,
re-run the matrix and reconsider the demotion. (RT3572 demotion status lives in
`../VERIFICATION.md` § "Unsupported — pending genuine hardware".)

---

## Hardware queue

### Cards in the mail (check doorstep)

- **Panda PAU06** — RT5372. Slots into existing `chips/rt2800usb/` as a
  `DeviceID` entry, minimal delta — no new port expected.
- **ALFA AWUS036NH** — RT3070. Same family, similar treatment (a `DeviceID` +
  chip-id extras entry + minor RXWI/TXWI tweaks, not a from-scratch port).

Bring-up + matrix verification once hardware arrives.

### Distant-future hardware ($$$)

- TP-Link Archer T2U Plus (RTL8821AU / RTL8811AU).
- Generic MT7601U — cheapest dongle, weird packet injection.

---

## MT7921AU (AWUS036AXML) — paused, low ROI

On mainline `mt7921u` it **does** enumerate on Kali (2026-06-01 trip,
`captures_mt7921u/`) *and* under WinUSB on Windows (FW upload gets partway). The
airodump data makes it the **weakest 2.4 GHz card in the fleet: 6–8 ch1 APs vs
20+** on the good cards. Its usbmon shows **~1 bulk-IN frame, no RX payload**, so
the usbcap extractor can't even measure it.

One architectural trait explains all of it: the connac/mt7921 RX uses a **deep
pre-submitted large-URB pool** that neither usbmon nor our sync transport
captures → weak mainline monitor RX **and** unmeasurable-via-usbmon **and** hard
bring-up, all the same root cause.

**Bring-up blocker: the `FW_START_REQ` wall** (reproduces on Kali too, not
WinUSB-specific). Leading hypothesis — **shallow bulk-IN URB pool:** the kernel
pre-submits 128 URBs/endpoint via `mt76u_alloc_queues` before any FW traffic;
our transport does one-at-a-time sync reads. Around `FW_START_REQ` the chip
expects a posted URB to be receivable (a status response on EP 0x85 + a 0-length
URB completion on EP 0x84 as the boot signal); between drainer iterations there's
none, and EP0/bulk-OUT goes dead. Fix would mean a libusb **async URB port**
(`libusb_submit_transfer`, pre-submit ~32 URBs/EP) — high effort, and upstream
`mt7921u` monitor support is itself unstable.

First re-confirm the hardware enumerates at all before sinking more time in.
See `chips/mt7921au/MT7921AU.md` + `chips/mt7921au/KALI-HANDOFF-2026-05-19.md`.
Revisit post-Defcon.
