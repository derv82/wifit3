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
`usb_dumps_new/captures_<driver>/capture-N.pcap` is the **ground truth**. Every USB transfer
in the working capture must exist in the port; the source explains why each one is there (and
gives the real register/macro/function names — never type constants from memory, grep them out
and paste verbatim). Where the two disagree, the wire wins.

**Source lives in two places:**
- **Mainline kernel C** → `data_dumps/<driver>-source-v6.18/` (the `[SRC]` cite target). Fetch
  from `torvalds/linux` @ `v6.18` (`gh api repos/torvalds/linux/contents/<path>?ref=v6.18`, or
  the `Download-GitHubFolder` PowerShell helper) — agent or user, doesn't matter.
- **Vendor DKMS source** → `usb_dumps_new/captures_<driver>/driver-source/` (+ tarballs in
  `usb_dumps_new/driver-sources/`). `capture.py` auto-pulls the bound DKMS source into each
  capture; a mainline-only card has none, and `driver.log`'s vermagic is the fetch recipe.

Deterministic helpers (see `scripts/AGENTS.md` for the full brief):
- `uv run python scripts/verify_pcap.py <chip>` — **the faithfulness gate.** Replays the
  port's bring-up against the cold-boot capture and raises at the first byte that diverges
  from the wire. Per-family codecs (Realtek 0x05, Ralink 0x06/0x07; see
  `scripts/rtw88_pcap_replay.py` / `rt2x00_pcap_replay.py`). Run it after every milestone.
- `python scripts/pcap_slicer.py <main.log> <pcap>` — maps `capture.py` log timestamps to
  pcap frame ranges (e.g. "firmware upload happens in frames 14182–14400"). Pick the
  cold-boot capture.
- `Grep` / `Read` directly against `data_dumps/<chip-source>/` for register and macro
  lookups (e.g. `Grep '#define\s+REG_FOO' data_dumps/rtw88-source-v6.18/`).

Captures are produced by `src/wifit3/scripts/capture.py` on the Kali persistent
USB; each capture ships a `*_logs/main.log` (absolute-epoch timeline) that
`pcap_slicer.py` consumes.

### Start from the source; the pcap is your test — and it has more than one writer

**Port from the source, not from the pcap.** Open the kernel/vendor function you're porting
and translate it into the driver line by line; run `verify_pcap.py` after each chunk as the
offline unit test. Don't read the wire and reverse-engineer what code produced each byte —
read the code, let the wire confirm. Sessions that walked source↔driver this way produced
faithful ports; the session that went pcap-first stalled the moment the bytes stopped looking
linear.

**The capture has more than one writer.** A cold-boot capture is one serialized stream of the
card's traffic, but the bring-up isn't the only producer on it. Timer threads the source
registers run concurrently and interleave their transfers — common ones to watch for: a
phydm/ODM watchdog every ~2 s (DIG + CCK-PD + adaptivity + NHM), an sreset poll, and under
airmon airodump's channel-hop timer. Their bytes recur at a fixed cadence and won't match the
function you're porting. **They are not noise — they are in the source, and they still must be
ported.** If you catch yourself wanting to *delete* recurring bytes to make the diff line up,
you've found an async producer, not a glitch.

**Strip, but never forget.** The synchronous diff *must* slice async streams out — they land
nondeterministically against the linear bring-up. But a stripped stream is unverified, and a
byte-for-byte PASS over a stripped DM is green over a hole — usually the exact hole where
runtime RX lives. So every op the card emitted must be *claimed*:
- **For every `_strip_<X>` you add, add a paired `verify_<X>`** that byte-diffs that stream's
  per-fire burst by replay. Enabling fact: the driver serializes register access across each
  timer callback, so every async producer's *per-fire* op run is **contiguous** — slice it by
  anchor and replay it (reads served, RMW writes checked), like any sync milestone.
- A stream genuinely not reproduced by design (airmon's STA→monitor dance, the chip-version
  prologue) is an **explicit waiver with a reason**, never a silent drop.
- The gate reports coverage: async patterns verified / waived (with reasons) / **unaccounted**.
  Unaccounted ⇒ NOT verified, regardless of sync PASS.

### Port every operation — the EFUSE read especially

Don't skip a read because it "only feeds a value." Skipped reads are the #1 partial-port bug,
and the EFUSE/EEPROM read (crystal_cap, RF-path count, per-rate TX power, MAC) is the one that
gets dropped most — it has bitten this project more than once. It's on the wire, so
`verify_pcap.py` now catches a skip as a divergence; but port it *deliberately*, not because
the gate forced you. Same for every helper write, every switch case, both `init` **and**
`start`.

### Porting never needs hardware when you have the capture + the source

The capture has every read **and** write; the source has the algorithm — so `verify_pcap.py`
**always** asserts a driver's correctness **offline**, read-feedback calibration (IQK/LCK)
included (the replay serves the recorded reads → a faithful algorithm reproduces the recorded
writes; branches and loops follow the captured path like the EFUSE walker). Hardware only
measures *benefit* (did beacons/s rise), never *correctness*. "I can't test X without hardware"
is always wrong — wire X into the mocked transport.

### Develop in the hardware loop — agent-driven

Pcap verification gates faithfulness offline, so bring-up is the **agent's** loop now, not a
human-in-the-loop one:

1. Agent proposes the milestone set.
2. Agent pcap-verifies each milestone (`verify_pcap.py`) — faithful to the wire.
3. Agent runs `scripts/<chipset>/test_hw.py` **itself** on the milestones where it makes
   sense, once the user has plugged in + WinUSB-bound (Zadig) the card.
4. Agent completes milestones without per-iteration handoff, **committing each**.
5. **The one human-gated step: live 802.11 TX (frame injection / deauth).** The agent never
   fires it — it *wires* TX (descriptor build, bulk-OUT path) but does not execute live. All
   other milestones (channel tune, 2.4 GHz RX, 5 GHz, TX wiring) are the agent's to finish
   [[passive_by_default]].
6. RX-health A/B: `beacon_watch.py` (live device) vs `beacon_watch_usbcap.py` (the
   `usb_dumps_new/` capture's beacon count for the same APs) — how our driver compares to the
   kernel's, same location, the nearby-AP beacons/s as the stable yardstick.

Device borked? The user replugs (resets cold-boot state).

### Hardware-verifiable milestones

Structure a bring-up as a sequence of small, demoable milestones, each
**pcap-diffed (`verify_pcap.py`) before declaring it done** — don't port a whole driver blind
and hope. The canonical first milestone:

- **M1 = firmware upload + FW_READY ACK only.** Demoable, no PHY init.

Then layer PHY/MAC/RF init, channel tune, RX, TX as subsequent M's, diffing the
generated USB sequence against the cold-boot capture at each step.

**Commit each milestone as its own commit** once it's both replay-diffed and
(where possible) HW-smoke-verified. Small per-milestone commits keep the bring-up
bisectable and the retired-vs-vendor history honest — don't batch several
milestones into one commit.

### Scope: 20 MHz channel *width* only — NOT "2.4 GHz only"

**20 MHz is the channel *width*, not the band.** This rule has been mis-read as "skip 5 GHz"
and led to 5 GHz RX/TX being dropped entirely — that's a bug, not the rule. **If the kernel
driver supports 5 GHz, our port must too** (every 5 GHz channel the kernel tunes, at 20 MHz
width). Channels 36–165 are in scope; only the 40/80 MHz *bandwidth* is not.

Do **not** port the kernel's 40/80 MHz channel-*width* path (`bw=1/2`, the `ch_group_index`
offset math, the secondary-channel + per-width `EXT_CCA`-group setup). wifit3 only ever tunes
the 20 MHz primary — every frame it captures (beacons, EAPOL, WEP IVs) and transmits
(deauth/replay) rides the primary at legacy rates, so 40/80 buys nothing and is pure port
surface. (See `chips/mt76x2u/MT76X2U.md` → "Channel width — 20 MHz only".)

### Bringing up a fresh card — recipe

When new cold-boot captures land in `usb_dumps_new/captures_<driver>/`
(`capture-N.pcap` + `capture-N_logs/main.log` + `driver-source/` if it's a DKMS card):

1. `pcap_slicer.py <main.log> <pcap>` — map "plug-in → FW load → channel hop → packets" to
   frame ranges. Pick the cold-boot capture.
2. Get the source (both homes — see "Pcap is ground truth" above): mainline C into
   `data_dumps/<driver>-source-v6.18/`; the vendor DKMS source is already in
   `usb_dumps_new/captures_<driver>/driver-source/` (capture.py pulled it).
3. Extract the FW blob from the cold-boot pcap, byte-verify against `linux-firmware/`, ship it
   in `chips/<driver>/assets/`.
4. M1 = FW upload + FW_READY ACK only. Demoable, no PHY init — **and pcap-verified.**
5. Layer each subsequent milestone, **`verify_pcap.py <chip>` after every one** (build the
   per-chip recipe as you go — see an existing `scripts/<chip>/verify_pcap.py` for the shape).
6. Drop a `<CHIP>.md` ground-truth doc next to the driver with `[SRC]`/`[WIRE]` citations;
   accumulate verified facts there. **Read the sibling `chips/<other>/<OTHER>.md` docs first**
   — same-family setup + the scars from porting them save re-learning.

The structural requirements a driver must satisfy (the `WlanDriver` Protocol,
`_all_drivers()` registration) live in `CLAUDE.md` → "Adding a New Chipset".

---

## Cleanroom DKMS re-ports (the 2.4 GHz RX fix)

The four Realtek 11ac-family cards (8822bu, 8814au, 8821au, 8812au) are currently
mainline-`rtw88`-derived and share `chips/rtw88_base/`. They inherit mainline's
weak 2.4 GHz monitor RX. The fix is to **re-port each from its vendor (DKMS)
source, cleanroom**.

### Why — mainline's weak 2.4 GHz monitor RX

The Kali A/B (`usb_dumps_new/`, same physical cards on their in-kernel vs vendor DKMS driver,
fixed-channel) showed mainline `rtw88`'s 2.4 GHz monitor RX (AGC/DIG) is materially weaker than
the vendor stack — cleanest case **RTL8822BU: 8 APs → DKMS 29 (3.6×)**. The deficit is in the
shared `rtw88_base` RX path our mainline-derived ports inherit, not the hardware or PyUSB (the
Ralink RT5572 reads 2.4 and 5 GHz equally well). Vendor and mainline are completely different
codebases above the same registers (`rtw_phy_dig()` vs the PHYDM/ODM stack), and the vendor
carries long-session AGC/DIG/thermal stability a 15 s snapshot can't measure. Hence: re-port
each Realtek 11ac card from its **vendor (DKMS) source, cleanroom**. Per-card A/B detail lives
in `usb_dumps_new/DRIVER-STATUS.md` + each `chips/<chip>/<CHIP>.md`.

(In-tree MediaTek / Atheros / RTL8187 cards are unaffected — no vendor fork, mainline is canonical.)

### Shared workflow (per card)

1. Branch `dkms/<module>` (e.g. `dkms/88x2bu`).
2. **Keep BOTH drivers.** The vendor port lands in a *sibling* package
   (`chips/rtl<chip>_dkms/`); the mainline-derived port stays put. Both register in
   `wlan/manager.py` for the same VID:PID, ordered by a per-family env var (`WIFIT3_RTL<chip>`,
   read fresh each call so it flips without a restart): DKMS is the default, `=mainline` opts
   back. One-env-var A/B at users' fingertips. (Retire-vs-keep is the lead's call, not doc
   policy.)
3. Port in a **fresh session** with only the vendor source + the new cold-boot pcap in view —
   the mainline driver and the mainline-derived Python kept out of context, so the port is
   faithful to the vendor code, not a hybrid. Treat as a new bring-up (recipe above). Capture
   the live mainline baseline (max beacons/s + nAPs) first — the A/B target to tie-or-beat.
4. The default flips to the vendor port once it's **HW-proven** to tie/beat mainline on breadth
   + stability (`WIFIT3_RTL<chip>` default ordering). `rtw88_base/` stays as long as any
   mainline-derived driver still imports it.

All four vendor sources are in `usb_dumps_new/driver-sources/` (tarballs) +
extracted `usb_dumps_new/captures_*/driver-source/`.

### Remaining work

The **8812au / 8814au / 8821au** vendor re-ports are **done** — `chips/rtl<chip>_dkms/` exist
and pass `verify_pcap.py`; HW A/B + default-flip status lives in `VERIFICATION.md` and each
`<CHIP>_DKMS.md`. Two left:

- **RTL8822BU** — highest-payoff re-port, **pending**. Mainline `chips/rtl8822bu/`; vendor =
  morrownr `88x2bu-20210702` 5.13.1 (`captures_rtl88x2bu/driver-source/` +
  `usb_dumps_new/driver-sources/rtl88x2bu-5.13.1.tar.xz`); branch `dkms/88x2bu`; mainline A/B
  `captures_rtw88_8822bu/`.
- **RTL8188EUS** — mainline fidelity fix **DONE**: IQK + LCK + crystal-cap ported, byte-verified,
  the port now matches the mainline kernel (~77% vs 83% reception). Mainline's RX ceiling on this
  card is ~80% with bad-window collapses. **DKMS re-port now active** — branch `dkms/8188eu`,
  sibling `chips/rtl8188eus_dkms/`; vendor `realtek-rtl8188eus 5.3.9` in
  `usb_dumps_new/captures_8188eu/driver-source/` (DKMS A/B 86–89%, min 7, no collapse). See
  `chips/rtl8188eus_dkms/RTL8188EUS_DKMS.md`.

---

## Blank-EFUSE / no-EFUSE cards → `planning/BLANK-EFUSE-SUPPORT.md`

Detecting a blank/counterfeit EFUSE, warning the user, and the in-RAM override (a generic
image substituted into the parsed struct — **never** burning fuses) are a card-support + UX
feature, not a porting step. Moved to `planning/BLANK-EFUSE-SUPPORT.md`.

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
