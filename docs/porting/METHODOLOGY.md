# Porting a chipset

You're re-implementing a Linux kernel (or vendor) driver in Python. The C source is what you
translate; a recorded USB capture of that same driver talking to *this* card is what you check
your work against. You hold both, so a finished port isn't a guess — it's a translation you can
verify against the wire.

Three companion docs: **CODE-STYLE.md** (how to write the port), **CHIP-DOC.md** (the per-chip
reference you ship with it), and **GOTCHAS.md** (recurring traps from past bring-ups). Terms used
below — pcap, timeline, bundle — are defined at the end.

> **Read only this chip's C source while porting.** Don't crib from another chip's driver in this
> tree: a sibling carries its own bugs and pulls you off the source (one chip was ported as
> Jaguar-1 by copying a sibling — it's actually Jaguar-2). Reading a shared base you genuinely
> import is fine, and comparing siblings while *debugging* is fine. The rule is about porting from
> a sibling instead of from source.

## Prerequisites

- **uv** for all Python — `uv run …`; bare `python` lacks the deps.
- **tshark** on PATH — the verify tool and every pcap query shell out to it.
- **The card, bound to userland** — Windows: WinUSB via Zadig. Linux: the splash's device-setup
  step writes a per-chipset modprobe blacklist + udev access rule and asks for a replug. The
  blacklist — *not* the udev rule — is what keeps the kernel off the card across replugs (a rule
  only chmods the node; the kernel still binds + taints cold state). Ad-hoc: `rmmod` the module and
  run as root.

Porting writes registers and replays the vendor firmware-download path with nothing between you
and the silicon. A byte-diff catches *inaccurate* sequences, not every *dangerous* one — test on
a card you can afford to lose. We only ever write RAM and registers and replay the vendor
*download* path; we never program EFUSE/EEPROM fuses (one-time-writable, permanent). A port that
adds a fuse write is wrong.

## Step 0 — Ask the user

Before writing code: which mode, where's the bundle, do you have the source.

- **Mode** — *kernel-dev* (I show each function as I port it and get milestone sign-off) or
  *autonomous* (I port and verify the whole driver myself, you review at the end — the default).
- **Bundle** — the `captures_<chip>/` directory: the pcap, the timeline + logs, the over-air
  capture, the firmware blob, and (DKMS) the vendor `driver-source/`.
- **Source** — DKMS cards ship it in the bundle. Mainline cards need the kernel tree at the
  captured version (read the driver name + vermagic from the bundle's `driver.log`; current
  captures are v6.18). Offer to fetch it from `torvalds/linux` at the matching tag if needed.

## Step 1 — Find where to start

A 10,000-line driver beside an empty `.py` isn't a blank page. The driver has a small fixed set
of entry points, and you port outward from them.

Find the doors — where the kernel calls in: the USB probe, the mac80211/cfg80211 ops it registers
(start/stop, config/tune, RX-filter, TX/xmit), and the firmware upload. Everything else is reached
from these. Trace the call graph out of each door; the union is your whole port surface, and the
only code that matters. Code never reached from a door (a PCI-only path on a USB card) isn't
"skipped" — it's not on the graph.

Order the doors by the timeline. `pcap_slicer.py` maps timeline timestamps to pcap frame ranges,
so you can see which door fires when: plug-in → firmware → init → monitor → channel hops →
injection. That order is your milestone sequence, derived from this driver rather than a template.
A milestone is a demoable chunk — firmware uploads and the chip ACKs, the EFUSE read, init
completes in monitor, one channel tune. In kernel-dev mode, propose the list and get sign-off; in
autonomous mode, proceed.

A few things hold for every driver:

- Bring the card up in the kernel's order — don't reorder or defer steps to demo something early.
  Cards calibrate RX/RF at specific points; move it and you're no longer bringing the card up the
  way the kernel does.
- Read the EEPROM/EFUSE early, because its values gate later init (RF-path count, antenna config,
  board type, channel plan, chip cut). Read them at runtime; never hardcode this card's values —
  a sibling with a different byte must still work.
- Monitor mode is a method on your driver, not an external command. `airmon-ng`/`iw` only trigger
  the driver's register writes; you port those writes.
- Port against the cold-boot pcap (card plugged in fresh).
- Commit each milestone on its own once it verifies. Milestone labels ("M1") live in commit
  messages and the chip doc, never in code.
- Start the chip's reference doc (CHIP-DOC.md) as you go.

The `Driver` ABC your `driver.py` must subclass is in CLAUDE.md → "Adding a New Chipset".

## Step 2 — Port the whole graph; skip nothing by name

Skipping the wrong code is the largest source of bugs here, so the rule is simple: port every line
reachable on the call graph — every branch, every helper, every switch case, both init and start.

Don't judge a branch irrelevant by its name. "Looks like Bluetooth," "the 40 MHz path,"
"coexistence" — these are exactly where drivers hide RX/RF-critical writes. Read what the code
*writes*, not what the function is called. A coex init block has held RX tuning we needed; a
width-conditional has held shared tuning the 20 MHz path depended on.

A branch this card's silicon can't trigger is still ported, behind its real runtime check (the
`if (efuse->X)` / cut / cap test from the source), marked `# TODO: verify — untested here, needs
<hardware>` with its citation. That's "ported, untested," not "skipped" — and it's what lets a
sibling card work.

Channel width: tune narrow, port wide. We only ever tune the 20 MHz primary, so we don't implement
the 40/80 MHz tuning *behavior* (secondary-channel math, per-width setup). But you still port the
code inside a width-conditional when it runs for the 20 MHz path or sets shared state.

Bands: port every band the kernel supports. If it does 5 GHz, you do 5 GHz — every channel it
tunes, at 20 MHz.

Never type a register address or bitfield from memory; grep it out of the source and paste it
verbatim (see CODE-STYLE.md).

## Step 3 — Verify each milestone against the pcap

After porting a milestone, check it against the pcap with the verify tool
(`uv run python scripts/verify_pcap.py <chip>`; the tool's internals are documented in that script
and the per-chip `scripts/<chip>/verify_pcap.py`).

Source is the guide, the pcap is the test. Port from the source, then replay the pcap to confirm
your driver makes the same USB calls the kernel made. Don't read the pcap op-by-op and reproduce
bytes you don't understand — that produces drivers that corrupt chip state or emit a tune prefix
the kernel never sent.

A divergence is a bug in your driver, never in the pcap or the kernel. Every byte in and out was
recorded; the replay feeds your driver the exact reads and ACKs the real card returned. If your
driver's output differs, it did something the kernel's didn't. So when the tool reports a
divergence, go back to the source and port what produced that op. Don't hardcode the diverging
byte, don't waive the op or edit the tool to skip it, and don't paper over a flaky step with a
retry loop — a step that needs N attempts means the port diverged from the source (the 8814au 8×
RF re-roll was a port gap, not hardware).

The target is a clean run with zero waived ops, end to end — including the STA→monitor setup, the
background timer writes the kernel interleaves (watchdog, sreset poll, hop timer: port the
mechanism and register it as an async handler), and TX. The injection and deauth frames in the
pcap are built by the driver's xmit path; reproduce them through `inject_frame()` and byte-match
them. "Don't port every line" is true only of pure-logic lines that touch no register and so have
no wire op to match — it isn't a licence to waive wire ops.

Prove one channel tune before fighting airodump's hop. A manual `iw set channel N` sweep
delineates each tune cleanly; get that byte-for-byte first. airodump calls the same `iw set
channel` under the hood, so once the unit tune matches, the hops are just that tune on a timer.

## Step 4 — RX quality: beacon_watch is the real bar

A clean verify run proves you reproduced the kernel's wire ops. It does not prove the card
*receives well*, and reception is what the port ultimately delivers. `beacon_watch.py` measures
that against a known-good card, and it outranks the verify gate.

Pin a strong nearby AP (closer than any other), fixed channel, 60 s, replug-cold between runs:

```
uv run python scripts/beacon_watch.py --bssid <ref-AP> --channel <ch> --duration 60
```

A known-good card holds ~9.8 beacons/s there. If your port hears roughly half that while the
known-good card stays steady, that's an RX bug in the port (front-end overload, AGC/DIG not
backing off, a missed tuning write) — fix it in the driver, never conclude "move further from the
AP." And never optimize for AP count or breadth: hearing more distant APs trades away the near
one, and being unable to hear a nearby AP is a non-starter. Score the reference AP's rate against
a known-good card at the same moment.

## Step 5 — Hardware testing and the TX gate

Validate on hardware with `uv run python scripts/<chip>/test_hw.py` (add `--debug`). The agent
runs this itself. Don't add "quick wins" that surface beacons to the user mid-port — reception is
judged by the scripts, not by eye.

The one human-gated step is live 802.11 TX. The agent *wires* TX — descriptor build, the bulk-OUT
path, the `aireplay_*` methods — but never fires live injection or deauth. That's the user's
explicit action.

If the device wedges, ask the user to unplug, wait, replug (resets cold-boot state).

## Step 6 — Before the port is done

A clean verify and flowing beacons are necessary, not sufficient — both gates are blind to a value
hardcoded to match the recording, and to uncaptured paths (TX-descriptor variants, power-save,
sreset, runtime recal). The agent does 1–6; 7 is the user's.

1. **Zero waivers, everywhere** — re-read the verify report end to end (cold bring-up, monitor
   entry, hops, injection/deauth). A waiver is an un-ported op hiding.
2. **Coverage audit** — confirm every reachable branch is ported or carries a `# TODO: verify`
   with its gate cited. Walk the graph for branches dropped without a marker; classify each leaf
   ported / ported-untested / not-on-graph.
3. **Every pcap, not just one** — run the verify against all cold-boot pcaps for this chipset
   (confirm same silicon first). Each completes with zero waivers.
4. **TX byte-match** — diff each frame your inject path builds against the same frame in the pcap.
   Only the sequence number (and, for WEP, the IV) legitimately differs.
5. **Async producers accounted for** — list the kernel's periodic threads (grep for
   `INIT_DELAYED_WORK` / watchdog / link-tuner / DIG); each that runs in our scenario is a
   registered async handler, not stripped.
6. **Recalibration cadence** — confirm your per-tune recal (freq / VCO / synth / TX power / AGC)
   matches the kernel's channel-config path, and the per-hop lock holds so a cancelled tune can't
   strand the chip on a stale channel.
7. **Hands-on break-it pass (user)** — alternate targets fast, hop hard, replug mid-run, soak
   30 min, fire the live attacks. Stress finds what a snapshot and a single pcap can't.

## Appendix — driver migrations (mainline → vendor/DKMS)

The Realtek 11ac cards went mainline-rtw88 → vendor DKMS for ~2× the 2.4 GHz monitor breadth.
Mainline rtw88 and the vendor stack are different codebases above the same registers, so the gap
lives in the shared RX path and re-porting from the vendor source recovers it. Cards with no
vendor fork — MediaTek, Atheros, RTL8187 — stay on mainline.

When re-porting from a different source tree, keep both drivers and A/B them. The new port lands
in a sibling package (`chips/rtl<chip>_dkms/`); the old one stays. Both register for the same
VID:PID, ordered by a per-family env var, new port default. Port in a fresh session with only the
vendor source and the new pcap in view, and treat it as a new bring-up. Baseline the old driver at
the reference AP first, and flip the default only once the new port ties or beats it on hardware.

## Housekeeping — every new port

- **Credits** — add the upstream driver's substantive contributors to CREDITS.md, mapped to the
  new card. Tally commit authorship of the kernel path (and the vendor repo); beware kernel file
  renames (GitHub's `?path=` filter doesn't follow them, so scrape the pre-reorg path too). Drop
  tree-wide mechanical committers; keep the real builders.
- **Licensing** — wifit3 is GPL-2.0-only (a derivative of GPLv2 drivers). Any firmware blob in
  `chips/<chip>/assets/` is not GPL: record its provenance + redistribution terms from
  linux-firmware's WHENCE and byte-verify the blob (see FIRMWARE.md).

## Terms

- **source** — the kernel/vendor C you're porting. Mainline is tag v6.18; vendor/DKMS ships in the
  bundle.
- **pcap** — the recorded USB conversation between the kernel driver and the card. The test.
- **timeline** — `main.log` in the bundle: what `capture.py` was doing at each moment.
  `pcap_slicer.py` maps its timestamps to pcap frame ranges.
- **over-air pcap** — the fixed-channel RF capture in the bundle; a side baseline, not the bar.
- **bundle** — the `captures_<chip>/` directory `capture.py` produced.
