# Porting a chipset to wifit3

> The procedure a coding agent follows to bring up a new USB Wi-Fi chipset.
> Read it top to bottom, then start at Step 0. The steps are ordered.

You are re-implementing a Linux kernel driver in Python. The kernel/vendor C **source**
is what you translate. A recorded run of that driver — talking to *this exact card* over
USB — is what you check your work against. You hold both, so a working port is not a
guess; it is a translation you can verify against the wire.

## Vocabulary (used precisely — never blur these)

- **source** — the kernel/vendor C you are porting. Mainline is tag **v6.18**;
  vendor/DKMS source ships inside the capture bundle.
- **pcap** — the recorded USB conversation between the kernel driver and the card. The
  verify tool replays it. The pcap is the **test**.
- **timeline** — `main.log` in the bundle: a timestamped record of *what `capture.py`
  was doing* at each moment (plug-in, firmware load, monitor entry, channel hops,
  injection). `pcap_slicer.py` maps timeline timestamps to pcap frame ranges. The
  timeline is the **map**.
- **over-air pcap** — the fixed-channel RF capture in the bundle. `beacon_watch_usbcap.py`
  counts its beacons to show how the kernel driver did *at capture time* — a side
  baseline. It is **not** the bar; the bar is the live `beacon_watch.py` (Step 4).
- **bundle** — the `captures_<chipset>/` directory `capture.py` produced: the pcap, the
  timeline + per-tool logs, the over-air pcap, the firmware blob, and (DKMS only) the
  vendor `driver-source/`.

Never write "the capture" on its own. Say **pcap**, **timeline**, or **bundle**.

## Prerequisites

- **`uv`** for all Python (`uv run …`; bare `python` lacks the project deps).
- **tshark** on `PATH`. Required: the verify tool and every pcap query shell out to it
  (not pyshark — pyshark is unreliable on Windows). No tshark, no porting.
- **The card, bound to userland:**
  - **Windows** — bind it to WinUSB with Zadig.
  - **Linux** — `sudo rmmod <kernel-module>` to release the in-kernel driver, then
    install a udev rule for userland access (same mechanism as
    `src/wifit3/platform/linux.py`). Replugging on Linux re-binds the kernel module, so
    prefer the udev rule over replug cycles.

---

## Step 0 — Ask the user

Ask these before writing any code. Do not assume paths, bands, or feature sets.

1. **Which mode?**
   - **Kernel-dev** — I show you each function as I port it, propose the milestone plan
     for your sign-off, and confirm anything ambiguous before proceeding.
   - **Autonomous** — I port the whole driver and validate it with the scripts myself;
     you review at the end. *(Default.)*
2. **Where is the bundle** (`captures_<chipset>/`)? Call it `<bundle>`.
3. **Do you have the matching source?**
   - DKMS card → it's already at `<bundle>/driver-source/`.
   - Mainline card → you need the kernel tree at the captured version. Read the driver
     name + `vermagic` from `<bundle>/*_logs/driver.log` (all current captures are
     **v6.18**). If the user doesn't have it, offer to fetch that driver's directory from
     `torvalds/linux` at the matching tag (`gh api
     repos/torvalds/linux/contents/<path>?ref=v6.18`, or the `Download-GitHubFolder`
     PowerShell helper) into `data_dumps/<driver>-source-v6.18/`. Call the source root
     `<src>`.

Then confirm the card is plugged in and bound (Prerequisites).

---

## Step 1 — Find where to start

A 10,000-line C driver beside an empty `.py` is not a blank page. The driver has a
small, fixed set of entry points; you port outward from them.

1. **Find the doors.** In `<src>`, locate where the kernel calls into the driver:
   - the **USB probe** / disconnect (`.probe`, `id_table`);
   - the **net / mac80211 / cfg80211 ops** the driver registers — at minimum
     `start`/`stop`, `config` (channel/tune), the RX-filter setup, and the **TX/xmit**
     path;
   - **firmware** request/upload, called from probe or `start`.
   These are the only ways the driver is ever invoked. Everything else is reached *from*
   them.
2. **Trace the call graph** out of each door. The union of everything reachable is your
   entire port surface — and the only code that exists, as far as the port is concerned.
   Code never reached from a door (e.g. a PCI-only probe path on a USB card) is not
   "skipped"; it is simply not on the graph.
3. **Order the doors by the timeline.** The timeline shows which doors fire, and in what
   order, during the recorded run: plug-in → firmware load → init → monitor entry →
   channel hops → injection.
   `python scripts/pcap_slicer.py <bundle>/*_logs/main.log <bundle>/capture-N.pcap`
   **That order is your milestone sequence** — derived from *this* driver, not a template.
4. **Carve milestones along the driver's own boundaries.** A milestone is a demoable
   chunk of the graph (e.g. "firmware uploads and the chip ACKs ready", "EEPROM/EFUSE
   read", "init completes, chip in monitor", "one channel tune"). In **kernel-dev** mode,
   propose the list and get sign-off before porting. In **autonomous** mode, proceed.

Hold for every driver:

- **Bring the card up in the kernel's order.** Do not reorder or defer steps for
  convenience or to demo something early. Many cards calibrate RX/RF at a specific point
  in init; move it and you are no longer bringing the card up the way the kernel does.
- **The EEPROM/EFUSE read comes early because its values gate later init** — RF-path
  count, antenna config, board/PA-LNA type, channel plan, chip cut. Reach it before any
  code that depends on a per-card value. **Read those values at runtime; never hardcode
  this card's values** — a sibling card with a different EFUSE byte must still work.
- **"Monitor mode" is a method on your driver, not an external command.** `airmon-ng`
  and `iw` only *trigger* the driver's register writes; you port those writes. The
  monitor state the card ends in is the sum of the init path — reproduce the path.
- **Pick the cold-boot pcap** (card plugged in fresh, no prior session) as the one you
  port against.
- **Commit each milestone on its own** once it ports and verifies — never batch. Never
  write a milestone label ("M1", "M4a") into code or comments; those live in commit
  messages and the per-chip `<CHIP>.md` doc.
- **Start the `chips/<chipset>/<CHIP>.md` ground-truth doc** as you port — record each
  fact you verify with its `[SRC]`/`[WIRE]` citation (FW path, init order, tune semantics,
  per-chip bit gotchas), so the next session reads them instead of re-deriving.

The `WlanDriver` Protocol your `driver.py` must satisfy, and how to register it, are in
`CLAUDE.md` → "Adding a New Chipset".

---

## Step 2 — Port the whole graph; skip nothing by name

Skipping the wrong code is the single largest source of bugs in this project. So the
rule is absolute: **port every line reachable on the call graph (Step 1) — every branch,
every helper, every switch case, both `init` and `start`.**

- **You never judge a branch irrelevant by its name.** "Looks like Bluetooth", "the
  40 MHz path", "the 2-antenna arm", "coexistence" — these are exactly where drivers hide
  RX/RF-critical register writes. Read what the code *writes*, not what the function is
  called. (A coex init block has held RX tuning we needed; a width-conditional function
  has held shared tuning the 20 MHz primary depends on. Both bit us.)
- **A branch this card's silicon can't trigger at runtime is still ported**, behind its
  real runtime check (the `if (efuse->X)` / cut / cap test from the source). You couldn't
  exercise it, so mark that arm `# TODO: verify — untested here, needs <hardware that
  would exercise it>`, with its source citation. **That is "ported, untested" — not
  "skipped."** You're already in the source; porting the arm now is far cheaper than
  forcing a future port to re-derive it, and it's what lets a sibling card work.
- **Channel width — tune narrow, port wide.** The port only ever *tunes* the 20 MHz
  primary, so you do not implement the 40/80 MHz tuning *behavior* (secondary-channel
  offset math, per-width bandwidth setup). But you still **port the code** inside a
  width-conditional function when it runs for the 20 MHz path or sets shared state — per
  the skip-nothing-by-name rule.
- **Bands — port every band the kernel supports.** If it does 5 GHz, you do 5 GHz: every
  channel it tunes, at 20 MHz width.

Constants: never type a register address or bitfield from memory. Grep it out of `<src>`
and paste it verbatim.

---

## Step 3 — Verify each milestone against the pcap

After you port a milestone from `<src>`, check it against the pcap with the verify tool.
*(The tool itself — its relays, CLI, section awareness, async handlers — is specified in
`planning/VERIFY-FRAMEWORK.md`. The principles below do not depend on its internals.)*

**Source is the guide. The pcap is the test.** Port from the source, then replay the
pcap to confirm your driver makes the same USB calls the kernel made. Do **not** read the
pcap op-by-op and reproduce bytes you don't understand — that path produces drivers that
corrupt chip state or emit a tune prefix the kernel never sent and detune the card on
every hop.

**A divergence is always a bug in your driver — never in the pcap, never in the kernel.**
The instinct to declare an op "impossible to reproduce" is strong, and it is always
wrong:

> Every byte into and out of the driver was recorded. The replay feeds your driver the
> exact reads and ACKs the real card returned. So if your driver's output differs, your
> driver did something the kernel's didn't. There is no hidden state and no unknowable
> input — find what you ported wrong in `<src>` and fix it.

When the tool reports a divergence, the only correct response is to return to the source
and port what produced that op. Two responses are forbidden, because both fake a pass on
a broken port:

- **Never hardcode the diverging byte** to match the recording.
- **Never waive the op, and never edit the tool to skip or pass.**

**Waive nothing.** The target is a clean run with **zero** waived ops, end to end:

- **The STA→monitor setup** that `airmon-ng`/`iw` trigger is the driver's register
  writes — port them.
- **Background timer writes** the kernel interleaves (a watchdog, an sreset poll, the
  hop timer) come from a real driver mechanism — port the mechanism and register it as an
  async handler; don't strip the ops.
- **TX.** The injection and deauth frames in the pcap are built by the driver's xmit
  path. Reproduce them through `inject_frame()` — port `aireplay_test()` /
  `aireplay_deauth()` driver methods that build exactly those frames — and byte-match them
  (in this same walk where the relay decodes bulk-OUT as comparable ops — VERIFY-FRAMEWORK
  §7, still open — otherwise as the separate Step 6.4 byte-diff). The deauth especially:
  it's the attack we most need correct.
- **A kernel init step that "our card doesn't do"** still happened on this silicon, so
  it's on the wire — port it (a `linux_init()` that mimics it if it has no other home).

"Don't port every single line" is true only of pure-logic lines that touch no register
and so have no wire op to match. It is **not** a licence to waive wire ops. If the driver
wrote it to the device, you reproduce it.

**Prove one channel tune before fighting airodump's hop.** A manual `iw set channel N`
sweep delineates each tune cleanly (one per command, timestamped in `iw.log`); get *that*
tune byte-for-byte first. airodump calls that **same `iw set channel`** under the hood
(aircrack-ng source), so once the unit tune matches, airodump's hops are just that tune on
a ~0.25 s timer in scrambled order — already covered. Don't fight the interleave up front.

---

## Step 4 — RX/RF quality: `beacon_watch` is the real bar

A clean verify run proves you reproduced the kernel's *wire ops*. It does **not** prove
the card *receives well* — and reception is what the port ultimately has to deliver. The
live `beacon_watch.py` measures that against a known-good card, and it **outranks the
verify gate**.

- **The bar is a strong, nearby reference AP.** Pin one AP (closer than any other),
  fixed channel, 60 s, replug-cold between runs:
  `uv run python scripts/beacon_watch.py --bssid <ref-AP> --channel <ch> --duration 60`
- **A poor result is always the port, never the environment.** `beacon_watch.py` reports
  your live beacons/s for the pinned AP; a known-good card there holds ~9.8/s (near the
  ~10/s a 100 ms beacon interval allows). If your port hears roughly half that — ~50%
  beacon loss — while the known-good card stays steady, that's an RX bug in the port
  (front-end overload, AGC/DIG not backing off, a missed tuning write); fix it in the
  driver. **Never** conclude "move further from the AP."
- **Never optimise for AP count / breadth.** Hearing more distant APs trades away the
  ability to hear the near one clearly, and being unable to receive or attack a nearby AP
  is a non-starter. Score the reference AP's beacon rate against a known-good card at the
  same moment — not best-AP, not the average, not how many APs appeared.

---

## Step 5 — Hardware testing and the TX gate

- Validate on hardware with **`uv run python scripts/<chipset>/test_hw.py`** (add
  `--debug`). The agent runs this itself. Do **not** add "quick wins" that surface
  beacons to the user mid-port — reception is judged by the scripts (Step 4), not by eye.
  The goal is a port the agent completes with the human out of the loop.
- **The one human-gated step is live 802.11 TX.** The agent *wires* TX (descriptor
  build, the bulk-OUT path, the `aireplay_*` methods of Step 3) but **never fires live
  injection or deauth** — that is the user's explicit action.
- If the device wedges, ask the user to **unplug, wait, replug** (resets cold-boot
  state).

---

## Step 6 — Before the port is done

A clean verify run and flowing beacons are necessary, not sufficient — the gates are
blind to a class of bugs (a value hardcoded to match the recording passes the verify by
construction; uncaptured paths like TX-descriptor variants, power-save, sreset, and
runtime recal are never exercised). Run this list. The agent does 1–6; **7 is the user's.**

1. **Zero waivers, everywhere.** Re-read the verify report end to end — cold bring-up,
   monitor entry, channel hops, **and** injection/deauth. No waived ops anywhere. A
   waiver is an un-ported op hiding; port it (Step 3).
2. **Coverage audit.** Confirm every reachable branch is ported or carries a
   `# TODO: verify` with its gate cited (Step 2). Then walk the call graph for branches
   dropped *without* a marker — the verify catches skipped wire writes, but pure logic (a
   cap flag, a channel-list entry, a MAC-program value) can be silently missing. Classify
   each leaf: ported / ported-untested / not-on-graph.
3. **Every pcap, not just one.** Run the verify against all cold-boot pcaps for this
   chipset (confirm same silicon first). Each must complete the full walk with zero
   waivers.
4. **TX byte-match.** Diff each frame your `aireplay_*` / inject path builds against the
   same frame in the pcap, byte for byte. Descriptor / header / payload / padding must
   match; only the per-frame sequence number (and, for WEP, the IV) legitimately differs.
5. **Async producers accounted for.** List the kernel's periodic threads (grep the family
   for `INIT_DELAYED_WORK` / watchdog / link-tuner / DIG); for each, decide whether it
   runs in our scenario. If yes, it's a registered async handler (Step 3), not stripped.
6. **Recalibration cadence.** Confirm your per-tune recal (freq / VCO / RF synth / TX
   power / AGC) matches the kernel's channel-config path, and that the per-hop hardware
   lock holds so a cancelled tune can't strand the chip on a stale channel.
7. **Hands-on break-it pass (user).** Hammer it: alternate targets fast, hop hard, replug
   mid-run, soak 30 min, fire the live attacks (deauth → handshake, PMKID, WEP replay,
   WPS). Stress finds what a snapshot and a single pcap can't.

---

## Appendix — driver migrations (mainline → vendor/DKMS)

The Realtek 11ac cards went mainline-`rtw88` → vendor DKMS for ~2× the 2.4 GHz monitor
breadth. Mainline `rtw88` and the vendor stack are different codebases above the same
registers (`rtw_phy_dig()` vs the PHYDM/ODM stack), so the breadth gap lives in the shared
`rtw88_base` RX path and re-porting from the vendor source recovers it. Cards with no
vendor fork — MediaTek, Atheros, RTL8187 — are unaffected; mainline is canonical for them.

When re-porting a card from a different source tree, keep both drivers and A/B them:

1. Branch `dkms/<module>`.
2. **Keep both.** The new port lands in a sibling package (`chips/rtl<chip>_dkms/`); the
   old one stays. Both register for the same VID:PID in `wlan/manager.py`, ordered by a
   per-family env var (`WIFIT3_RTL<chip>`, read fresh each call): the new port is the
   default, `=mainline` opts back.
3. **Port in a fresh session** with only the vendor source and the new pcap in view — the
   old driver and old Python kept out of context — so the new port follows the vendor
   code, not a blend. Treat it as a new bring-up (this doc).
4. Baseline the old driver at the reference AP (Step 4) first. Flip the default to the
   new port only once it ties or beats that baseline on hardware.

Supported-hardware status and the pending-hardware queue live in `VERIFICATION.md`.

---

## Housekeeping — every new port

Before a port is "done", two things outside the code need updating:

- **Credits.** Add the upstream driver's substantive contributors to `CREDITS.md`, mapped
  to the new card. Tally commit authorship of the kernel path
  (`gh api --paginate "repos/torvalds/linux/commits?path=<driver path>" -q '.[].commit.author.name' | sort | uniq -c | sort -rn`)
  and/or the vendor repo's contributors API. **Beware kernel file renames** — GitHub's
  `?path=` filter does not follow them, so scrape the pre-reorg path too (e.g. the old
  `drivers/net/wireless/rt2x00/…` and `drivers/net/wireless/rtl8187_*.c`) or you'll miss
  the original authors. Drop tree-wide mechanical committers; keep the real builders.
- **Licensing.** wifit3 is **GPL-2.0-only** (a derivative work of GPLv2 kernel/vendor
  drivers — not an optional choice). Any new firmware blob shipped in
  `chips/<chip>/assets/` is **not** GPL: record its provenance + redistribution terms from
  the linux-firmware `WHENCE` manifest, and byte-verify the blob against `linux-firmware`.
