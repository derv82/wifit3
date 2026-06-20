# Verify Framework — design

> **Status: design, not built.** This captures what the chipset-verification tool should
> become. Today it exists as a per-chip `scripts/<chipset>/verify_pcap.py` plus shared
> family replay helpers; that shape has known failure modes (below). This document is the
> target to build toward, and the reference for any change to the current scripts.
>
> The *principles* a porter follows (zero waivers, "a divergence is always our bug",
> port-everything) live in `planning/PORTING.md` Step 3 and do not depend on this tool's
> internals. This doc is the tool.

## 1. What the tool is for

A chipset port re-implements a Linux driver in Python. We have a pcap of the real kernel
driver talking to the exact card over USB — every read, every write, every ACK. The tool
**replays the card half of that conversation** to our driver: when our driver issues a
read, it returns the bytes the real card returned at that point; when our driver issues a
write, it checks the bytes against what the kernel driver wrote. Walk the whole pcap with
a single cursor and our driver must reproduce every operation the kernel driver made.

That makes it an **integration test of the port against the recorded wire** — the strongest
correctness signal we have offline, and the thing that lets a chipset be ported without
the hardware in hand. (Reception *quality* — does the card actually hear a near AP — is
`beacon_watch`'s job, not this tool's; see PORTING.md Step 4.)

## 2. Why the current per-chip scripts fall short

Real failure modes observed while rolling verification out across the fleet — the tool
must design these out:

1. **The script can be made to lie.** A per-chip `verify_pcap.py` is editable code. An
   agent under pressure has rewritten one to print `PASS` and never compare. If "green"
   is a script the agent owns, "green" is meaningless. **The pass/fail logic must not
   live in code the porter edits.**
2. **Waiving is unbounded and abused.** The legitimate cursor needs to skip nothing — but
   the current scripts let an agent waive arbitrary ranges to reach green. "This section
   can't be reproduced" is the most common wrong turn in a port, and the tool currently
   *enables* it. Waiving must be a narrow, justified, loud, last-ditch act — not a
   one-liner that buys a green run.
3. **A lone diverging op is a poor debugging signal.** "Divergence at op 0x44" doesn't
   tell the agent *where* in the bring-up it is or *what neighbourhood* of the kernel
   code to read. Worse, after a bad manual waive the cursor is misaligned and every
   subsequent "next divergence" is noise. The agent needs context and a section label,
   not a bare offset.
4. **tshark in the agent's hands is fragile.** Porting requires tshark (pyshark is
   unreliable on Windows), but agents shelling out to `tshark` directly produce brittle,
   inconsistent queries. The tool should own all tshark interaction behind a stable
   interface.
5. **Per-chip scripts duplicate and drift.** Every chip has its own `verify_pcap.py`;
   they re-implement the same walk and re-derive the same family decode. The walk, the
   reporting, the waive accounting, the async dispatch — all of that is generic and
   belongs in one framework. Only the chip-specific glue should be per-chip.

## 3. What the tool must guarantee

- **The verdict is the framework's, not the porter's.** PASS ⇔ zero unaccounted ops ⇔
  the cursor reached the end of the pcap with every op matched (or explicitly,
  accountably waived). The porter cannot reach PASS by editing glue.
- **Zero waivers is the default and the goal.** A waiver is exceptional, named, counted,
  and printed. The tool actively resists blanket waives.
- **A divergence localises itself** — section + context window + decoded fields — so the
  fix is "read this kernel function", not "stare at the pcap".
- **Generic across chip families**, with the per-chip surface as small as possible.
- **Driven through one stable interface** (a CLI) that hides tshark and is the same for
  every chip.

## 4. Architecture

Five layers. The middle three are generic framework; the porter supplies only the glue
(§4.3) and the chip's driver code.

```
   pcap ──▶ [4.1 op-stream decode]  (family/chipset relay; owns tshark)
                  │  ordered ops: (dir, endpoint, request, addr, len, data, frame#)
                  ▼
        [4.2 replay transport]  ──serves reads/ACKs──▶  the ported driver
                  │  compares each driver op to the next expected op
                  ▼
        [4.4 section cursor]  (timeline + pcap_slicer → "you are in set_channel")
        [4.7 async dispatch]  (registered handlers for timer/watchdog producers)
                  │
                  ▼
        [4.5 divergence report] / [4.6 waive ledger]  ──▶  [4.8 CLI]  ──▶ agent
```

### 4.1 Op-stream decode — the relay

Turns raw USB frames from the pcap into an ordered stream of typed operations:
`(direction, endpoint, request, address, length, data, frame#)`. This is the only
USB-family-specific decode, and it is the hard part — Realtek (vendor control `0x05`),
Ralink/MediaTek-USB (`0x06`/`0x07`), Atheros, and newer MediaTek all frame their
register I/O differently, and we cannot enumerate every chipset's endpoints up front.

Design:

- A **`Relay` interface** the framework defines: given decoded USB frames, yield the op
  stream; given an op the driver just issued, say whether it matches the next expected op
  and (for reads) what bytes to return.
- **Built-in family relays** for the families we already understand (Realtek `0x05`,
  Ralink `0x06/0x07`). The porter selects one by an **enum**.
- If no family fits, the porter **writes a chipset relay** (documented in the relay
  registry, §5). **Prefer a chipset-specific relay over mutating a shared family relay** —
  a family relay is shared by sibling chips, and "fixing" it for a new chip silently risks
  their verification. Granularity is a per-case call; when in doubt, isolate.
- The relay **owns tshark.** It is the only component that parses the pcap, behind the
  `Relay` interface. Nothing else shells out.

### 4.2 Replay transport — the cursor

A **mock transport** that implements the same read/write interface the driver's real
transport exposes, injected into the driver in place of PyUSB. Most drivers already take
their transport via construction (`from_usb_device` / a transport wrapper), so the glue
is an injection point, not a refactor.

- On a driver **write**: pop the next expected op from the relay; compare endpoint /
  request / address / data. Match → advance. Mismatch → **divergence** (§4.5); the walk
  stops.
- On a driver **read**: confirm the next expected op is a read of the right shape, then
  return the **recorded response bytes**. This is what makes read-feedback algorithms
  (EFUSE walk, IQK/LCK calibration, any `read-modify-write`) verifiable offline — the
  driver branches on the same values the silicon gave the kernel.
- The cursor is **single and monotonic**: one walk from the driver's first op to the end
  of the pcap. No rewinding, no per-section reset.

### 4.3 The glue the porter provides

Kept deliberately small — everything else is framework:

1. **Relay selection** — a family enum, or a chipset `Relay` implementation (§4.1).
2. **The driver's transport injection point** — how the framework hands the mock
   transport to this driver.
3. **Section openers** — the first op (or a recogniser) that marks the start of each
   named phase, so the cursor can label position (§4.4). Derived from the timeline.
4. **Async handlers** — for each periodic producer the kernel runs, a handler + its
   unique opening op (§4.7).

That glue is data + small hooks. It contains **no pass/fail logic** — so it cannot be
edited to force a green run (§3).

### 4.4 Section awareness

A bare op offset is a poor signal. The **timeline** (`main.log`) + `pcap_slicer` already
map wall-clock phases to pcap frame ranges (firmware, phy init, airmon/monitor,
`set_channel`, injection). The framework consumes that map plus the porter's section
openers (§4.3) and tags the cursor with the **current section**. Every report then reads
"divergence in `set_channel`, op 3 of the tune" — pointing the agent at the right kernel
function instead of a hex offset.

### 4.5 Divergence reporting

A divergence must hand the agent enough to fix it without touching the pcap:

- the **section** and position within it;
- a **context window** — the previous N ops, the diverging op, and the next N ops
  (≈5 each) — so a misalignment is visible as a *shift*, not a lone surprise;
- **decoded fields** for each: direction, endpoint, request, address, length, data;
- **expected vs. got** for the diverging op, byte-aligned;
- a pointer to the relevant section opener / kernel phase.

### 4.6 The waive ledger — last-ditch, loud, accountable

Waiving exists, but the tool fights its abuse:

- **No silent or blanket waive.** There is no "skip to op N", no "waive `bytes[0:]`". The
  only way to waive is an explicit call **pinned to a section with a reason string**,
  e.g. `waive(section="airmon", reason="…")`. A waive that isn't pinned to a known
  section is rejected.
- **Every waive is printed and counted** in the final report, with its section and
  reason. A PASS with any waivers says so, loudly.
- **The framework can forbid waivers in declared zones.** Cold bring-up, monitor entry,
  channel tune, and TX are zero-waiver by policy (PORTING.md Step 6 §1); a waive there is
  an error, not a yellow flag.
- The honest end state is **zero waivers**. The ledger exists to make the rare real one
  visible and justified — not to grease a green run.

### 4.7 Async producers

The pcap is one serialized stream, but the kernel has more than one writer: timer threads
interleave (a ~2 s phydm watchdog, an sreset poll, the channel-hop timer). The cursor must
not trip over them, and must not let the porter strip them.

- The porter **registers an async handler** with its **unique opening op**. When the
  cursor reaches that op, the framework hands control to the handler, which runs until its
  burst ends, then returns the cursor to the main driver walk. The burst is contiguous
  because the kernel holds the device IO lock across each timer callback, so a tick lands
  as **one block** and never splices into the middle of a tune — the handler consumes its
  block and the main walk resumes exactly where it paused. (This is why dispatch is sound,
  not guesswork.)
- The opener must be unique. Dispatch the wrong handler and its first op mismatches →
  divergence, fail-loud. There is no silent mis-attribution.
- This is why a missed always-on producer can't hide: its first write reaches the cursor
  with no handler claiming it → unaccounted op → not PASS. The fix is to port the
  mechanism and register it — never to waive the writes.

### 4.8 The CLI — the agent's interface

The agent drives the tool through one CLI (a `.py` invoked via `uv run`), never by
editing a script and never by touching tshark. The CLI is also a **read interface onto
the pcap**, which is where agents otherwise misuse tshark. Roughly:

- `verify <chip> [--pcap N]` — run the walk; emit a structured divergence report (§4.5)
  or PASS.
- `sections <chip> [--pcap N]` — list sections and their frame ranges (from §4.4).
- `ops <chip> --section set_channel [--limit 10]` — the decoded ops in a section.
- `op <chip> --frame K` / `--index I` — decode a single op.
- `diff <chip> --frame K` — expected vs. our driver's emitted op at that point.

CLI-as-interface keeps the agent away from both editable pass/fail logic and raw tshark,
and makes "what does the kernel do here?" a query instead of a parse.

## 5. The relay registry

One place lists every relay, the families/chipsets it covers, and its USB signature
(endpoints + control request codes). A porter checks the registry first:

- family relay covers this chip → select it by enum;
- nothing fits → write a chipset relay, add it to the registry, **do not** retrofit a
  family relay used by other chips (§4.1).

Keep it in this doc (or a `scripts/verify/relays/README.md` once the framework lands) so
the family/endpoint knowledge accrues in one spot instead of scattering across per-chip
scripts.

## 6. Coverage — a diagnostic, never a gate

It's tempting to wire line coverage (`pytest --cov=src/wifit3/chips/<chip>` around the
verify run) into the verdict. **Do not gate on it.** A single card exercises only the
code paths *its* EFUSE selects; the un-hit lines are the sibling-card surface we
deliberately keep (PORTING.md Step 2). Make 100% coverage the bar and the agent deletes
the un-hit `if (efuse->rf_paths == 1)` arm to go green — gutting exactly the
forward-compatibility we want.

Use coverage the other way: the **un-hit reachable lines are the `# TODO: verify`
worklist** — what a different specimen of the same chipset would exercise. A report, not a
gate.

## 7. Open design questions

- **Glue mechanism.** Is the cleanest injection a mock transport passed at construction,
  or a proxy that intercepts the driver's transport calls? Most drivers already accept an
  injected transport — confirm that holds across families before committing.
- **Relay granularity.** Where exactly is the family/chipset line? We don't yet know
  enough about each family's endpoint variation to predict it. Bias to chipset-specific
  until a family abstraction is clearly safe.
- **Section-boundary precision.** The timeline is wall-clock; openers are exact. When the
  timeline is too coarse to delimit a section cleanly, do openers alone suffice, or does
  the relay need to expose more structure?
- **Async opener uniqueness.** Some producers may not have a single unique opening op.
  What's the fallback recogniser when the opener is ambiguous (without reintroducing
  guesswork)?
- **TX in the same walk.** TX bulk-OUT is the driver's xmit path (we reproduce it via
  `aireplay_*`, PORTING.md Step 3), so it belongs in the cursor — confirm the relay
  decodes bulk-OUT frames as comparable ops, not as opaque blobs.

## 8. Migration from today's scripts

- **Reference shapes (good):** `scripts/rtl8187/verify_pcap.py` (clean single-cursor init
  walk) and `scripts/rtl8188eus_dkms/verify_pcap.py` (init + operational async dispatch).
  The framework generalises these. Caveat: both **predate the zero-waiver-on-the-airmon-
  dance rule** (PORTING.md Step 3) and waive that setup, so they are the *structural*
  template, not a complete exemplar — the framework reproduces the dance, it does not
  inherit their waiver.
- **Anti-pattern (retire):** the windowed, milestone-counting gates
  (`rtl8814au_dkms`, `rtl8822bu_dkms`). They count ops per window instead of walking one
  cursor — exactly the shape that tolerates a misaligned waive and a faked pass.
- Build the framework, port the two good shapes onto it first to prove the relay +
  injection model, then migrate the rest and delete the per-chip walk logic, leaving only
  glue (§4.3).
