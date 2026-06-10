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
- `uv run python scripts/verify_pcap.py <chip>` — **the faithfulness gate.** One cursor walks
  the *whole* capture; the port's real bring-up/handlers must reproduce every op. It PASSes only
  when every op is matched or explicitly waived, and stops at the first **unaccounted** op —
  which *is* the next thing to port. The replay primitive is shared per family (Realtek 0x05,
  Ralink 0x06/0x07; `scripts/rtw88_pcap_replay.py` / `rt2x00_pcap_replay.py`); each chip owns
  its `scripts/<chip>/verify_pcap.py` recipe. Run it after every change.
- `python scripts/pcap_slicer.py <main.log> <pcap>` — maps `capture.py` log timestamps to
  pcap frame ranges (e.g. "firmware upload happens in frames 14182–14400"). Pick the
  cold-boot capture.
- `Grep` / `Read` directly against `data_dumps/<chip-source>/` for register and macro
  lookups (e.g. `Grep '#define\s+REG_FOO' data_dumps/rtw88-source-v6.18/`).

Captures are produced by `src/wifit3/scripts/capture.py` on the Kali persistent
USB; each capture ships a `*_logs/main.log` (absolute-epoch timeline) that
`pcap_slicer.py` consumes.

### The verify is one monotonic walk — fail-closed, never short-circuited

`verify_pcap.py` is not a milestone checklist; it is **one walk down the whole captured
conversation with a single cursor** — a unit test that stays red until the port is 100 %
faithful, where the op it fails on *is* the next driver code to port.

**Port from the source; the wire is your test.** Translate the kernel/vendor function line by
line and run `verify_pcap.py` after each chunk. Don't reverse-engineer what produced each byte
— read the code, let the wire confirm. Pcap-first sessions stall the moment the bytes stop
looking linear.

**Every op has exactly one honest fate** as the cursor advances:
- **matched** — the port's real handler reproduces it byte-for-byte; or
- **waived** — an *explicit, named, counted* boundary for a producer the port doesn't
  reproduce (a separate timer like the sreset poll; the chip-version probe before power-on;
  airmon-the-tool's own mac80211 setup — we port the *vendor driver*, not airmon). Printed in
  the report, never a silent drop; or
- **unaccounted** — anything else **stops the walk and names the op**: the frontier, the next
  thing to make faithful. PASS ⇔ zero unaccounted.

**Dispatch async producers; never strip them.** The capture is one serialized stream, but the
bring-up isn't the only writer — timer threads interleave (a ~2 s phydm watchdog, an sreset
poll, airodump's channel hop). Within one capture every op has a fixed position, so you don't
delete them — you **dispatch**: when the cursor hits a producer's opening op, run *that*
handler (carrying its state across fires), then continue. An un-ported mechanism then can't
hide — it writes a register no handler claims and surfaces as the next unaccounted op.

**Never "improve" on the wire.** The capture is ground truth for the chip's end state.
Reproduce what the driver wrote; never *add* a write "for breadth" or substitute a value you
assume is better. A write nobody made is as much a divergence as one you missed — that is
exactly how an ungrounded, possibly-harmful deviation gets bolted onto a faithful port.

Reference shapes: `scripts/rtl8187/verify_pcap.py` (clean single-cursor init walk) and
`scripts/rtl8188eus_dkms/verify_pcap.py` (same, plus operational-phase dispatch).

### Port every operation — the EFUSE read especially

Don't skip a read because it "only feeds a value." Skipped reads are the #1 partial-port bug,
and the EFUSE/EEPROM read (crystal_cap, RF-path count, per-rate TX power, MAC) is the one that
gets dropped most — it has bitten this project more than once. It's on the wire, so
`verify_pcap.py` now catches a skip as a divergence; but port it *deliberately*, not because
the gate forced you. Same for every helper write, every switch case, both `init` **and**
`start`.

### Green ≠ faithful — the gates' blind spots, and the comment-blind audit

`verify_pcap` green + `beacon_watch` healthy is **necessary, not sufficient.** Both gates are blind
to a whole bug class, and we shipped `rtl8188eus_dkms` green while hardcoding per-card efuse values
it should read — found only by accident. Internalise the limits:
- **`verify_pcap` is green by construction for hardcoded values.** Constants tuned to reproduce the
  recorded wire cannot be validated *by* that wire. It only catches a wrong value that changes a
  **captured write**. Per-card-variable values (efuse fields, chip cut, board / PA-LNA / antenna /
  channel-plan), uncaptured paths (TX-desc variants, 40 MHz, power-save, sreset, runtime IQK/LCK),
  and internal-state correctness are all outside its reach.
- **`beacon_watch` only catches catastrophic RX loss** on one scenario.

**The trap when hunting these gaps is our own comments.** A port's comments are the porter's
assumptions written as fact (`this value is always X so we hardcode it`, `this fn never runs on
$chip so we skip it`). An agent auditing *by reading our code* anchors on them and rubber-stamps.
**Audit comment-blind:** derive expected behaviour from **kernel source + real chip state** — extract
the real efuse / chip-version from the pcap, never trust the byte a comment claims — then diff our
*emitted bytes / computed values* against it. Treat every `always / never / skip / no-op` comment as
a **hypothesis to falsify**, default-assume-wrong until silicon or source proves it. Walk the kernel
**call graph**, classify each leaf `faithful / hardcoded / omitted / N-A`, make every "faithful"
cite its anchor, and prioritise conditional / per-card / runtime leaves over straight-line tables.
Worked example + per-axis plan: each chip's `<CHIP>.md` port-completeness audit section (start with
`chips/rtl8188eus_dkms/RTL8188EUS_DKMS.md`). This is fleet-wide — every driver brought up against one
dev card likely shares the pattern.

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
   per-chip recipe as you go — `scripts/rtl8187/verify_pcap.py` is the reference shape).
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

### morrownr's Recommendations

https://github.com/morrownr/USB-WiFi/blob/main/home/Recommended_Adapters_for_Kali_Linux.md

If anyone knows a good wireless card for Kali Linux, it's morrownr!

### Cards in the mail (check doorstep)

- **Panda PAU06** — RT5372. Slots into existing `chips/rt2800usb/` as a
  `DeviceID` entry, minimal delta — no new port expected.

### Shipped

- **ALFA AWUS036NH** — RT3070 (`148f:3070`, REV_RT3070F, RF3020, 1T1R, burned EFUSE).
  **DONE 2026-06-09 — the first byte-perfect rt2x00-family member.** A standalone clean-room
  `chips/rt3070/` (NOT a `chips/rt2800usb/` DeviceID delta — that shared base has a confirmed
  EFUSE word/byte addressing bug the old firmware-only gate hid). `verify_pcap rt3070`
  reproduces capture-1 end to end — init → airmon monitor entry → every airodump/iw channel
  hop, **8879/8879 ops single-cursor**, waiving only aireplay's TX-status polls. RX is
  kernel-parity (8.4 beacons/s vs the kernel's 8.9), TX (deauth → reconnect handshake) is
  human-confirmed. New code path vs the family (RF3020 → `config_channel_rf3xxx`,
  `init_rfcsr_30xx`, `init_bbp_30xx`) — the candidate base the family converges onto later.
  Facts + gate: `chips/rt3070/RT3070.md`; per-attack status: [VERIFICATION.md](../VERIFICATION.md#rt3070).
  Follow-ups since closed: WPS green (PIN→M4, PBC→PSK); the sustained-use RX falloff was the
  RX-DMA wedge (concurrent control transfers when a UI view switch cancels a tune mid-flight),
  fixed d425550 — not the AGC/no-DIG drift first suspected.

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
