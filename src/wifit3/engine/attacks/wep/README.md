# WEP attacks (and cracking)

Scope doc for porting `aireplay-ng -1/-3/-4/-5` + an `aircrack-ng`-equivalent PTW key recovery into wifit3. Native Python on top of `WlanInterface`, same shape as the existing PMKID / SAE / decloak attacks.

## Why

`NEXT-STEPS.md` used to say "skip WEP — outdated". Flipped 2026-05-22: WEP networks still occasionally surface (legacy IoT setups, ancient routers left running) and seeing one in Scanner with no attack available is the wrong end of the wifit3 promise. Modern isn't the only target.

## Tool taxonomy (refresher)

| `aircrack-ng` tool | wifit3 equivalent |
|---|---|
| `airodump-ng` — passive RX, IV collection | already-present Scanner / Focus, extended with IV dedup + count |
| `aireplay-ng` — active TX, IV generation | new — this directory |
| `aircrack-ng` — PTW / FMS / KoreK cracker | MVP shells out; full-native ports PTW |
| `airdecap-ng` — post-crack pcap decrypt | one-shot utility, not a TUI feature |

## Phase 1 — IV capture (~2 days, first M)

Listen for WEP-encrypted data frames on a target BSSID. Extract the 3-byte IV (offset 24 in the encrypted body, immediately after MAC header). Dedup + count uniques. Stash to either:
- in-memory ring buffer (live counter in Focus), and / or
- a `.cap` / `.ivs` file on disk for downstream cracking.

No TX required. Builds entirely on the existing parser RX path.

UI: WEP-encrypted AP in Scanner gets a `(W)` marker. Focus on it shows "X unique IVs / Y total frames" counter, ticking up live.

## Phase 2 — IV generation attacks (~2 weeks total)

Mirror `aireplay-ng`'s option flags so the existing documentation / muscle memory transfers cleanly.

### `-1` Fake authentication (~1 day) — prerequisite
Open-system authentication + association as a forged client so the AP accepts our future injections. Builds on the auth / assoc machinery already in `pmkid_harvest.py`. Lands first.

### `-3` ARP replay (~1–2 days) — the workhorse
Listen for an ARP request (broadcast WEP data frame, distinctive 68 / 86-byte size depending on padding). Once captured, replay it on a loop. The AP echoes back ARP replies — each carries a fresh IV. **Most WEP networks fall to this attack alone** given an existing client on the network.

### `-5` Fragmentation [Bittau 2005] (~3–5 days)
Inject fragmented frames with known plaintext (LLC / SNAP header — always the same prefix). The AP's response, when reassembled, reveals up to ~1500 bytes of keystream. With keystream we forge arbitrary packets — typically a fake ARP request to feed back into `-3`. Useful when no client is on the network to source the seed ARP.

### `-4` ChopChop [KoreK 2004] (~3–5 days)
Byte-by-byte decryption of one captured packet via the AP as an ICV oracle. Strip the last byte, XOR in a guess, send. AP either acks (correct guess) or doesn't. 256 guesses per byte max. Recovers plaintext + keystream without ever knowing the key. Slower than fragmentation; useful as a fallback when fragmentation doesn't elicit a response.

## Phase 3 — Cracking

### MVP path (~½ day)
Shell out to a system-installed `aircrack-ng` against the dump file, parse its stdout for the recovered key, surface in the UI. External dependency, but the user-visible flow is identical to the native path.

### Full-native path (~1 week)
Port PTW [Pyshkin / Tews / Weinmann 2007]. Needs ~40–85 k unique IVs for 104-bit recovery. Breakdown:
- RC4 inner loop: ~20 lines
- IV → key-byte vote tabulation: ~200 lines
- Search + key-candidate testing: ~200 lines
- FMS + KoreK fallback for stubborn cases: another ~500 lines

Net: ~500–1000 LoC of careful crypto math. Self-contained; no external `aircrack-ng` required.

## Milestones

```
M1  — Phase 1 IV capture + UI counter           (~2 days)
M2  — Fake auth (-1) on a forged client         (~1 day)
M3  — ARP replay (-3) end-to-end                (~2 days)
M4  — Shell-out PTW crack ("MVP scope")         (~½ day)   ← shippable
        ───── ship here for the satisfying flow ─────
M5  — Fragmentation (-5)                        (~4 days)
M6  — ChopChop (-4)                             (~4 days)
M7  — Native PTW                                (~5 days)
M8  — FMS + KoreK fallback                      (~3 days)
        ───── "full native" milestone ─────
```

M1–M4 in ~1 week of focused work lands the visible feature. M5–M8 close it out in another ~2–3 weeks.

## Testing

User has a box of ~2017-era routers — many of those still support WEP. Plan: dedicate one as a permanent WEP test target, leave it running while developing. WEP-active networks on the wider neighborhood scan are rare but possible (legacy IoT, forgotten setup APs).

## Open questions before M1 starts

- Does our existing RX path surface WEP-encrypted data frames cleanly, or do some chips auto-decrypt / drop them? Should sanity-check on each working driver (`ar9271`, `rt2800usb`, `rtl8821au`, `rtl8822bu`, `rtl8812au`, `rtl8188eus`, `mt76x2u`, `mt76x0u`) — monitor-mode promiscuous RX usually delivers the encrypted bytes verbatim, but worth confirming.
- IV-dump file format: stick with `.ivs` (aircrack native, compact) or `.cap` (pcap-format, inspectable in Wireshark, ties into existing pcap export plumbing)? Lean `.cap` for symmetry with our handshake export.
- Should chopchop / fragmentation actually land before M4 ships, or after? Argument for after: ARP replay covers ≥90% of WEP-with-client cases; the fallbacks are for edge cases that may never come up in real testing.

---

## M5/M6 — Fragmentation (-5) + ChopChop (-4): refined design (2026-05-24)

**Status:** M1–M4 + native PTW (M7) shipped + hardware-verified. **M5
Fragmentation is now DONE + hardware-verified end-to-end** (offline crypto →
on-air probe → `en_hwseq=0` software-seq TX fix on rtl8821au → `WepFragmentation`
daemon → campaign + Focus "Frag" button → live crack of the dd-wrt box). **M6
ChopChop is the only attack left** (its crypto `chop_last_byte_and_fixup` is
done; next is its own oracle probe, then daemon, then a "Chop" toggle next to
Frag). This section supersedes the auto-escalation idea sketched in Phase 2.

Known minor: post-frag replay IV-rate (~50-80/s) trails replay off a real
client ARP (~150-200/s) — the forged ARP (default sender/target 192.168.1.x)
doesn't amplify like a real host's ARP. Fine for frag's no-client use case
(still cracks in ~2 min); optional future lever is a configurable forged-ARP
target IP.

### ARP-replay rate control: P&O (IMPLEMENTED 2026-05-24, PENDING HW validation)

**Replaced the adaptive burst climber with perturb-and-observe** (`arp_replay.py`,
`_maybe_adjust_rate`). Control var = injection pps; objective = IVs/s measured
over a `_PO_DWELL_S=3.0`s dwell (> the AP's ~1-2s relay delay); step
`_PO_STEP_PPS=16`, start `_PO_START_PPS=96`, bounds `[24, 500]`, reverse only on
a >`_PO_IMPROVE_EPS=3%` drop (noise deadband). Runs only while `replaying`. 4
offline tests cover keep-direction / reverse / hold-when-not-replaying / clamp.
**Needs hardware validation:** confirm it converges to and hovers near the IVs/s
peak (the ~80-120 pps the box liked), doesn't oscillate wildly, and recovers
post-frag. Watch the heartbeat `X pps → Y IVs/s`. Tunables if it's twitchy:
bigger dwell (slower, steadier), smaller step (tighter hover).

**Why we got here — the climber was NOT trusted:** evidence it was
*suboptimal*, only that it beat a static 400 pps.

- **Hardware evidence (2026-05-24, user):** at ~350-400 pps it got ~30 IVs/s,
  but at ~80-120 pps it got ~80 IVs/s. **More injection → FEWER IVs/s.** So
  there's an interior optimum (~100 pps-ish on the dd-wrt box) and the climber
  overshoots it. Both a too-low fixed cap (the reverted 150) and the climber's
  runaway (~400) miss it.
- **Why the climber is wrong:** it hill-climbs **burst size** on **per-cycle IV
  gain**, but the AP's relay has a **~1-2s delay**, so a burst's IVs land in
  *later* cycles → the per-cycle signal is misattributed noise. You can't
  gradient-climb a sub-delay measurement.
- **The objective is IVs/s** (≈ time-to-crack), NOT pps and NOT capture% (a
  vanity metric — see [[feedback_optimize_real_metric]]).
- **The right shape (user's framing, a known standard):** closed-loop
  extremum-seeking / **perturb-and-observe** (cf. solar MPPT). Control variable =
  injection rate (pps). Objective = IVs/s measured over a window **longer than
  the relay delay** (~2-3s). Step the rate, wait > delay, measure IVs/s, follow
  the gradient, settle/hover at the pleateau. Start slow, climb, hover.
- **Diagnostics already in place:** the 8s heartbeat now logs `X pps → Y IVs/s`
  (pps = literal injected/cycle, NOT estimated from capture%). IVs/s is stable;
  pps bounces — another reason to control on IVs/s.
- **Frag injection is SEPARATE + low-rate:** `WepFragmentation._inject_round`
  sends ~9 fragments then sleeps `_ROUND_GAP=0.2s` ≈ **~45 pps**, fixed cadence,
  NOT the climber. It just needs one successful round (usually round 1), so it's
  not hammering the AP and isn't part of this rework.

### State machine — human-driven, NOT auto-escalation

The earlier "replay → frag → chop ladder that auto-advances on failure" is
**dropped.** Two reasons:
- ARP Replay is never *provably* failed (may just need a client / a deauth).
- Frag/Chop failure is **ambiguous** — no AP response could be out-of-range, a
  TX glitch, *or* a genuinely immune AP, and we can't tell which. So we can
  never honestly auto-advance on "exhausted."

So transitions are **user-driven**, with exactly one auto-transition: on
**success** (which is unambiguous).

**ARP Replay is the IV engine + home base. Frag and Chop are two alternative
"manufacture an ARP seed" sub-modes** for when replay has no ARP (no client
traffic). All three are mutually-exclusive TX activities (one half-duplex
radio) — which is why `WepArpReplay` already has `pause()`/`resume()`.

```
Generate IVs ─► REPLAYING (fake-auth underneath; waits for / replays ARPs)
   │  user clicks Frag ─► pause replay ─► FRAGMENTING
   │  user clicks Chop ─► pause replay ─► CHOPPING
   │  (click the other mode = stop current + start it — click-to-switch)
   │
   FRAGMENTING/CHOPPING ──success(keystream→forged ARP)──► hand to replay,
                                                          resume ─► REPLAYING
   FRAGMENTING/CHOPPING ──round fails──► KEEP RETRYING the same mode (log
                                          progress "Frag: 0/N usable ARPs"),
                                          never auto-stop or auto-switch
   Stop IVs ─► tear everything down
```

"Keep retrying" is the point: a chosen attack loops until the *user* stops or
switches it — we never auto-stop on a failed round. Log a running tally
("Fragmentation: 0/20 rounds produced a usable ARP", "ChopChop: byte 4/36")
so the user can judge when it's fruitless and switch. The only thing the code
decides on its own is *success* (→ resume replay), because that's unambiguous;
"no response" never is (range vs TX vs immune AP), so the human calls it.

### Button UX (Focus, WEP target, campaign running)

- **Frag** and **Chop** buttons appear alongside the running campaign, each a
  Start↔Stop toggle, **always enabled** (no disable-the-other dance — clicking
  one stops the other → click-to-switch, Tab-friendly).
- The campaign owns the "only one TX activity at a time" invariant via
  `replay.pause()`/`resume()`.

### Both attacks reduce to: keystream → forge ARP → feed replay

- **Fragmentation [Bittau 2005]:** XOR a captured frame against the known
  LLC/SNAP prefix → 8 bytes of keystream. Send ≤16 tiny fragments (each
  encrypted with that keystream) of a known-plaintext frame; the AP reassembles
  + re-encrypts under one fresh IV + relays it → capture that → recover a
  *longer* keystream (~1500 B). Enough keystream → forge a broadcast ARP.
- **ChopChop [KoreK 2004]:** chop the last byte of a captured frame, guess its
  plaintext (256 tries), fix the ICV (CRC32 is linear), send; the AP relays it
  iff the guess was right (the oracle). Walk backwards → full plaintext +
  keystream, no key. → forge a broadcast ARP.

### Build order (de-risk like we did the cracker)

1. ~~**`wep_crypto.py` FIRST — offline, fully unit-testable, no hardware.**~~
   ✅ **DONE + verified** (commits 06bc85b, 798126c, 5503b09):
   `icv`, `wep_encrypt`, `forge_arp_request`, AND `chop_last_byte_and_fixup`
   (the ChopChop linear-ICV fix-up — landed via affine-CRC cancellation +
   a GF(2) trailing-byte solve, gated by a decrypt→check-residue oracle).
   11 offline tests in `tests/engine/test_wep_crypto.py`. So the ENTIRE crypto
   surface of frag + chopchop is built and proven with zero hardware.
2. **Fragmentation send-side primitives — ✅ DONE + offline-verified** (`build_fragments`,
   `seed_keystream_from_arp`, `arp_request_plaintext` in `wep_crypto.py`; 5 new
   tests). KEY SIMPLIFICATION: the frag payload is itself a **broadcast ARP**, so
   one round collapses three things — the AP's relay is simultaneously (a) the
   oracle's success signal, (b) a directly replayable ARP seed, and (c) ~40 B of
   fresh keystream (XOR vs our known 36-B plaintext). Seed PRGA is free from a
   captured broadcast ARP (fixed SNAP+ethertype `AA AA 03 00 00 00 08 06`). 8-B
   seed → 4 data bytes/fragment → 16×4=64 ≥ the 36-B ARP. The send-side tests
   prove SELF-CONSISTENCY (a simulated reassembly round-trips) — NOT that the
   real AP reassembles+relays; only the probe can establish that.
3. **`scripts/wep/frag_probe.py` — ✅ READY for hardware** (the README's
   recommended first hardware step): fake-auths, sends fragmented broadcast-ARP
   rounds, dumps EVERY RX frame timestamped to a `.pcap` + console log, flags
   candidate relays (fresh-IV FromDS broadcast WEP data from our forged SA). The
   pcap is the ground truth (spec+pcap model). **NEXT: run at the dd-wrt box,
   read the pcap, then code `fragmentation.py`'s oracle to what's observed.**
4. **Fragmentation VERIFIED END-TO-END ON HARDWARE (2026-05-24).** The
   `en_hwseq=0` software-sequence fix (rtl8821au `build_tx_desc_data`) let the
   dd-wrt box reassemble our 9 fragments and rebroadcast the result; decrypting
   a relay with the test key confirmed it byte-for-byte as our forged ARP. **The
   live-AP ORACLE SIGNATURE (the thing `fragmentation.py` watches RX for):**
   a Data frame, **FromDS** + **Protected**, **Addr1 (DA) = broadcast**,
   **Addr3 (SA) = our forged STA MAC**, **fresh IV** (≠ our seed IV), ~68 B
   (24 hdr + 4 IV/KeyID + 36 ARP + 4 ICV). It appears post-burst on the target
   BSSID *and its sibling BSSes* (the box rebroadcasts our broadcast onto each
   BSS), so match on `Addr3 == our_mac`, not on a specific `Addr2`/BSSID. That
   relayed frame is also already an ARP-sized broadcast → the capture store logs
   it as a replay seed automatically. So fragmentation **success = "a relay with
   our SA appeared"** → tell the campaign, which resumes ARP replay (the new
   seed is in the store). Tooling: `scripts/wep/frag_probe.py` (inject + dump) +
   `scripts/wep/analyze_frag_pcap.py` (`--key` decrypt-verify).
5. **`fragmentation.py` DONE + wired** (`WepFragmentation`): seeds from any WEP
   data frame (LLC/SNAP known-plaintext, ethertype 0x0800 then 0x0806 — NOT a
   replayable ARP), fragments a broadcast ARP under a shared sw_seq, watches the
   pinned oracle, hands the relay to the campaign on success (immediate handoff),
   keeps retrying on a barren round. Campaign owns the Frag sub-mode
   (`start_frag`/`stop_frag`/`_on_frag_success`, replay pause/resume); Focus has
   the "Frag" toggle. **`chopchop.py` is still a skeleton** — next: its own probe
   (inject a chopped guess, dump, read the relayed shortened frame / accept-vs-
   reject signal, gold-standard-verify vs the key), then the daemon, then a "Chop"
   toggle. No sw_seq needed for chop (single frames, not a reassembled train).

### File status

`wep_crypto.py` and `fragmentation.py` are fully implemented + tested +
hardware-verified. `chopchop.py` is still a skeleton (interface + algorithm
docstring + `NotImplementedError`); its crypto (`chop_last_byte_and_fixup`) is
done in `wep_crypto.py`, so only the live-AP oracle + daemon + UI remain.
