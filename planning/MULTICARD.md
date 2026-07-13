# Wifit3 — Multi-Card Support (Design Brain-Dump)

> ⚠️ **STATUS: INITIAL BRAIN-DUMP — NOT A PLAN OF RECORD.** Captured 2026-07-13 from a live
> design conversation so the thinking-cost isn't lost. **Every seam below needs significant
> ironing before any work starts.** The seam list is *illustrative, not exhaustive* — it's a
> few of many; expect the real refactor to surface more. **Post-alpha.** Alpha ships single-card.
> Do not start building from this doc as-is; it's the raw material for a real design pass.

---

## Why — the strategic case

Single-card Wifit3 is "cross-platform + userland, but a few % behind the Linux driver." That's a
*convenience* pitch, not a *capability* pitch — and on its own it may not justify the build. The
capability that changes the story is **multi-card**: pool RX to hear more, and split TX off its
own radio so a deauth doesn't deafen our own capture. "Plug in a second card for better results"
is a headline feature Linux CLI tools make you hand-assemble from multiple `airmon` interfaces.
That's the differentiator worth doing — *later, and right.*

## What already works — de-risking findings

The fear was "a global variable in a driver breaks two-of-the-same-card." The audit says the
framework is **more multi-card-ready than expected** — the enumeration layer is basically
scaffolding for this already:

- **Manager already fans out.** `_scan_bus` uses `usb.core.find(find_all=True)` (`manager.py:150`);
  `refresh()` builds a **separate `WlanInterface` per device** (`wlan0`, `wlan1`, …) via
  `from_usb_device(dev, …)`, each with its own `dev` handle (`manager.py:184-194`). Two identical
  cards both match and both get an interface; the dedup signature keys on `dev.address`
  (`manager.py:176`), so same-VID:PID-different-address is already distinct.
- **RX thread is per-instance, not a singleton.** `RxReaderThread` is explicitly *"Composition,
  NOT a base class"* — instantiated per driver with its own `read_once`/`dispatch`/thread. N cards
  = N reader threads, no shared state. (Only nit: both default to `name="rx"` in logs — pass
  distinct names.)
- **Driver global state is nearly absent.** Grep for module-level mutable `global` across all
  drivers: **one hit that matters** — `_saturation_count` in `rtl8822bu/rx.py:47`, a diagnostic
  counter in the *non-default* mainline 8822bu driver. Not in AR9271, not in MT7921AU. Everything
  else keeps state on `self`. That one global should be moved onto the instance regardless.
- **Thread-safety discipline exists.** `scripts/rt3070/hw_concurrency_test.py` + the per-driver
  `_hw_lock` already handle two-threads-one-driver races. Precedent to extend, not invent.

## Capability layers (severable)

The feature decomposes into four layers. **1–3 deliver the actual capability; 4 is polish and can
be deferred** (v1 of multi-card can require both cards plugged in at launch):

| Layer | What | Cost estimate (pre-ironing, low confidence) |
|---|---|---|
| 1. N concurrent interfaces, N callbacks | Two cards → independent drivers + RX threads | ~done (manager) |
| 2. Merged, deduped RX stream | Fan N cards' RX into one aggregator + shared model | small — a fan-in |
| 3. TX-card designation | One card is the TX radio; route `inject_frame` there | trivial — a role flag |
| 4. Hot-plug detect + Y/N modal + loading modal | Detect a plug mid-run, modal bringup, splice live | the weeks-of-Textual part; deferrable |

## Dedup design

**Key insight: most of the pipeline is already dedup-safe, so DON'T compare bytes.** The core
registry is keyed and idempotent under double-delivery:

- **AP registry** → `access_points[bssid]` (`interface.py:239`): two beacons (same emission *or*
  two intervals) just last-write-wins on RSSI/last-seen. Free bonus: whichever card has the
  stronger RSSI wins naturally → better data, not worse.
- **WEP IVs** → `_unique_ivs` set (`wep_store.py:180`): a second card's duplicate IV is dropped.
- **EAPOL** → guarded by `replay_counter`: a duplicate M2 can't mint a phantom handshake.

So a merge with **zero dedup still yields a correct AP list / handshake / WEP crack.** Dedup only
earns its keep at three sinks, where duplicates *lie* or *waste*:

1. **`packet_stats.record_rx`** — double-counts; frames/s reads ~2× with two cards on one channel.
2. **pcap output** — keep the on-disk capture clean.
3. **CPU** — skip re-parsing the identical frame.

**The cheap key: `(addr2, seq)`** — transmitter MAC + the 12-bit 802.11 sequence number. The
transmitter bumps `seq` once per frame, so:
- Beacon1 heard by card A and card B → same `(addr2, seq)` → **duplicate**.
- Beacon2 vs Beacon1 (consecutive) → `seq` incremented → **two real frames**.

An ~8-byte tuple into a **bounded ring/deque** (time-windowed ~1 s, or last few hundred keys). O(1)
per frame; **3 cards doesn't multiply cost** — one hash-lookup per arriving frame against one
shared recent-set, ring size fixed, memory flat in N. No length compare, no byte compare, no
full-frame hash.

**Parser gap:** `parse_80211_frame` reads `addr2 = frame[10:16]` but **stops before Sequence
Control (bytes 22–23)**. One-line add for 3-addr frames (4-addr WDS already returns `None`, ctrl
frames rejected): `seqctl = frame[22] | (frame[23] << 8); seq = seqctl >> 4`.

**Gotcha:** a real retransmit reuses `seq` and sets the Retry bit → `(addr2, seq)` collapses it
into the original. Fine for scanning (we don't want retransmits); **beacons are never
retransmitted** so it's moot for the AP list. Fold the Retry bit into the key if a use case ever
needs retransmits.

## Handshake payoff — cross-interface assembly

The real multi-card win for handshakes: **M2 from card 1 + M3 from card 2 → one valid handshake.**
The intent works because the handshake/EAPOL tracking keys on `(bssid, client, replay_counter)`
(see the EAPOL log line at `interface.py:421`), so frames arriving from *different* interfaces
assemble in one **shared** tracker. This is the argument for a single merged model rather than
per-card models stitched after the fact. EAPOL dedup across cards is easy — `seq` in a short deque;
the two copies arrive within milliseconds. *(Open: confirm the tracker actually assembles frames
whose halves came from different sources — see Open Questions.)*

## Seams — INCOMPLETE, a few of many

> This is the part that needs the most ironing. Treat as a starting list, not a spec.

- **Splash multi-select.** 1 card → `START` behaves exactly as today. >1 card → checkboxes to pick
  which cards join the session.
- **Async bringups.** Bring the selected cards up in parallel to save time — no conflict expected
  (independent USB handles), but *verify* (shared libusb backend, USB host-controller contention).
- **RX aggregation + dedup.** The fan-in above; where does the aggregator live (see IfacePool)?
- **TX-interface selection.** Default = "first" card; allow override at splash / on hot-plug.
  Passive-by-default still holds — TX stays behind the explicit-action gate.
- **Channel-hopping distribution.** Hopping every card over the *same* channels is wasteful.
  Partition `SUPPORTED_CHANNELS` across cards — but cards differ (2.4-only vs 2.4+5): the
  distribution algorithm must respect per-card capability, and more cards complicates it (band
  coverage, overlap, one-card-per-target dwell). Non-trivial on its own.
- **The `iface.method` audit — the big one.** Everywhere the app calls a single interface's method
  assumes one interface. All of it needs to route through a fleet abstraction — an **`IfacePool` /
  `CardPool`** (name TBD) owning the merged model, channel arbitration, and TX routing. This is the
  refactor that makes it "messy"; scope it explicitly before touching code.
- **Error handling must stop being fatal.** Today a bringup error is fatal, and a mid-session
  unplug is fatal. With a fleet, both must **degrade to a toast/log notification** as long as ≥1
  card is still active — only go fatal when the *last* card dies. Related landmine: the WinUSB
  mid-session install already blocks the view behind a modal for up to ~5 min — a fleet with
  hot-plug makes blocking modals worse; the notification model needs rethinking alongside this.

## Milestone sketch (order TBD)

Rough sequencing, each a committable stop — **not yet real milestones**, just the shape:

1. Spike (throwaway `scripts/multicard/`): 2× AR9271, then 2× MT7921AU, via the real `refresh()`
   path → fan both RX into one dedup aggregator → print one merged stream + per-card counts.
   Proves layers 1–2 and closes the global-state question on hardware. Blocks nothing.
2. Parser: add `seq` (one line) + tests.
3. `IfacePool`/`CardPool` skeleton + the `iface.method` audit (design pass first — Lead sign-off).
4. Splash multi-select + async bringup.
5. Merged model + `(addr2, seq)` dedup at the stat/pcap sinks.
6. TX-card selection.
7. Channel-distribution algorithm.
8. Non-fatal error/unplug → toast; hot-plug modal (Layer 4, last).

## Forward-compat — EvilTwin

We want **EvilTwin** eventually (see `FEATURES.md` Rogue AP / WPA3-downgrade sections), so the
fleet architecture should be designed with a TX-dedicated card in mind from day one — an evil twin
wants one radio impersonating the AP while another keeps listening. "Do it right the first time,
methodically, forward-thinking" is the explicit constraint on this whole design. Don't paint the
`IfacePool` into a single-TX corner.

## Open questions (must resolve before work)

- **Where does the merged model live?** `IfacePool`/`CardPool` owning it, vs. a merged
  `WlanInterface`, vs. an aggregator above per-card interfaces. This is the central class-design
  decision — **Lead discussion before execution** (per project convention).
- **Confirm cross-source handshake assembly** — does the tracker actually pair an M2 and M3 that
  arrived on *different* interfaces? Verify the exact key + shared-tracker assumption.
- **Channel-distribution algorithm** — real design needed (per-card band capability, overlap,
  per-target dwell). Its own sub-doc likely.
- **Test matrix — the enormous long tail.** 14 drivers; "do X and Y work together" is *pairs*, not
  singles — ~90+ combinations, on hardware, each with the live-TX gate. Plus 2× same-card. This is
  a release cycle by itself and needs its own strategy (representative pairs? per-family? tiered?).
- **libusb/host-controller contention** under N concurrent cards — confirm async bringup and
  sustained N-card RX don't starve on the shared backend or a single USB controller.

---

*Related: `FEATURES.md` → "Multi-card support (Minnie Drivers v2)" (summary), Rogue AP / EvilTwin
sections (forward-compat consumer).*
