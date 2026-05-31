# WPS attacks (PIN brute-force + pixie-dust)

Scope doc for porting Reaver/Bully-style WPS PIN recovery **and** a native
PixieWPS offline attack into wifit3. Native Python on top of `WlanInterface`,
same shape as the WEP / PMKID / SAE attacks. Drafted 2026-05-27 after reading
the reaver-1.4, kimocoder/bully, and wiire/pixiewps source dumps in
`data_dumps/repomix-dumps/`.

## Why now

`NEXT-STEPS.md`: WPS is *"detection done, attacks not started."* We already
decode the WPS IE (`packet.py:_parse_wps_ie` → `AccessPoint.wps*`) and surface
`wps_locked` with a 🔒 in Scanner/Focus. The missing half is the attack: PIN
brute-force with rate-limit backoff + ETAs, and PixieWPS bad-RNG cracking —
a great fit for the box of ~10 aging Ralink/Realtek/Broadcom routers on hand.

The motivating question was *"why is reaver/bully so painfully slow?"* The
research answer: **the slowness is self-imposed conservatism, not the
protocol.** The on-air floor for one PIN is `EAPOL-Start → EAP-Identity →
M1..M7`, sub-second on a responsive AP (reaver's own comment: M5/M7 turnaround
is "a few hundredths of a second"). The cost is three knobs both tools crank
down for safety, all of which we can revisit empirically against the fleet:

1. **Fixed inter-attempt sleep** — reaver `-d` = 1s *per PIN* (~3h of pure
   sleep over 11k attempts); bully `--pin2delay` = 5s after every M7 NACK.
2. **Full deauth→auth→assoc before *every* attempt** — both tools, "as some
   APs severely rate-limit otherwise." 1–2s of blocking waits each cycle.
3. **Coarse RX timeouts** — reaver blocks in `pcap_next()` on a whole-second
   `setitimer`. Bully's one real improvement: an **adaptive per-message
   timeout** (`avg + avg/8 + 5ms`, learned *separately* per M1/M3/M5/M7) plus
   link-layer ACK-confirmed retransmit. That, not lock logic, is why bully
   survives flaky APs.

### Locked decisions (2026-05-27, with Lead)

- **Online brute engine first**, pixie-dust layered on after (both ride a
  shared crypto core, so the core is M1 regardless).
- **Native PixieWPS, all modes** — including the Realtek time-seed and eCos
  searches, numpy-vectorized to keep the 2³¹–2³² seed loops interactive. No
  external `pixiewps` binary dependency.
- **Chase the speed win in v1** — single-association keep-alive + per-AP
  adaptive timing are first-class from the start, not a deferred pass.
  Optimize *measured time-to-PIN per router* ([[feedback_optimize_real_metric]]),
  not a global safe constant.

## No chipset changes

WPS is EAP-over-EAPOL (ethertype `0x888e`) inside ordinary 802.11 data frames —
the same inject+sniff path PMKID already uses. Confirmed reusable plumbing:

- `pmkid_harvest.py` already forges a client MAC + builds Auth/Assoc-Req frames.
- `interface.send_raw(..., use_no_ack=True)` — the inject primitive.
- `interface.register_rx_callback(cb)` — raw frame bytes for a low-latency
  state machine (we do *not* drive WPS off the UI-polled AP registry).
- `interface.register_forged_mac(mac)` — keeps our fake STA out of the client list.

**The catch:** reaver and bully both embed hostapd's WPS C library to
build/parse M1–M8 and do the crypto. We can't — so the bulk of the work is
**implementing the WSC Registrar ourselves in Python**. The crypto is all
stdlib (`pow()` for DH bignums, `hashlib`/`hmac`) and is *shared with pixie*.
Our parser currently decodes only EAPOL-**Key** (802.1X type 3); WPS is
EAP-Packet (type 0) → EAP-Expanded (254) → WSC, which we parse *inside* this
module via the RX callback rather than bloating the hot-path beacon parser.

## Protocol primer (one PIN attempt)

We act as the external **Registrar**; the AP is the **Enrollee**. One attempt
is one full EAP/EAPOL session:

```
TX  EAPOL-Start
RX  EAP-Request/Identity
TX  EAP-Response/Identity = "WFA-SimpleConfig-Registrar-1-0"
RX  M1   (Enrollee nonce N1, PKe = enrollee DH pubkey, device info)
TX  M2   (Registrar nonce N2, PKr, Authenticator)
        └─ both sides now derive: DHKey = SHA256(g^AB mod p);
           KDK = HMAC-SHA256_DHKey(N1 ‖ EnrolleeMAC ‖ N2);
           KDF(KDK) → AuthKey(256) ‖ KeyWrapKey(128) ‖ EMSK(256)
RX  M3   (E-Hash1, E-Hash2)         ← PixieWPS needs only up to here
TX  M4   (R-Hash1, R-Hash2, ENC{R-S1})
RX  M5   *or NACK*                  ← NACK here ⇒ FIRST half of PIN wrong
TX  M6   (ENC{R-S2})
RX  M7   *or NACK*                  ← NACK here ⇒ SECOND half wrong; M7 ⇒ SUCCESS
TX  WSC_NACK (tear down)
```

**The two-halves oracle** (why it's ~11k attempts, not 10⁸): the 8-digit PIN
splits into PSK1 (first 4 digits) and PSK2 (last 4 = 3 digits + 1 checksum).
M4 reveals our R-S1; the AP recomputes R-Hash1 against the PIN it knows and
answers M5 only if our guessed first half matches — else NACK. M6/R-S2 does the
same for the second half. So: **10⁴ (first half) + 10³ (second half, checksum
fixes the 8th digit) ≈ 11,000 worst case.** Bully derives the
FIRST/SECOND-half-wrong signal from *three* sources — explicit WSC_NACK,
EAP-FAIL, and (optionally) an M5/M7 timeout-as-NACK — worth replicating all
three for stubborn APs.

## Architecture — file layout & class responsibilities

```
engine/attacks/wps/
  README.md         this scope doc
  __init__.py
  wsc_crypto.py     SHARED CORE. Pure functions, no I/O, no state:
                      DH over RFC-3526 MODP group 5 (pow()), HMAC-SHA256,
                      WPS KDF, DHKey/KDK/AuthKey/KeyWrapKey derivation,
                      AES-128-CBC (M5/M7 encrypted settings), PIN checksum,
                      check_pin_half (the E-Hash1/2 verify pixie reuses).
                      Fully unit-testable offline vs hostapd/pixiewps vectors.
  messages.py       WSC TLV codec + EAP/EAPOL framing (Expanded type 254,
                      WFA vendor ID, SIMPLE_CONFIG opcode). Build M2/M4/M6;
                      parse M1/M3/M5/M7/NACK/ACK/DONE. Offline self-consistency
                      tests (round-trip against an in-process fake enrollee).
  registrar.py      WpsRegistrar — the per-PIN EAP/WSC state machine. Driven by
                      an inject callback + an RX queue; knows nothing about USB.
                      Returns FIRST_HALF_WRONG / SECOND_HALF_WRONG / SUCCESS /
                      TIMEOUT / PROTO_ERROR. Owns the session DH keypair+nonces.
                      Exposes the pixie bundle (PKE,PKR,E-Hash1/2,E-Nonce,
                      AuthKey) after M3.
  association.py    WpsAssociation — forge auth+assoc, KEEP-ALIVE across PINs,
                      re-assoc only on failure. Reuses pmkid_harvest builders.
                      The single-association experiment lives here.
  timing.py         Adaptive per-message timeout (bully's avg+avg/8+5ms, per
                      M-type) + per-AP latency stats. The measurement rig.
  pins.py           PIN keyspace: checksum, two-halves split, ordering,
                      save/resume per-BSSID (bully persists .pins + .run).
  lock.py           Lock detection (beacon AP-Setup-Locked IE — already parsed
                      into AccessPoint.wps_locked; + bully's 3-strike M3-NACK
                      heuristic for silent lockers) + adaptive backoff that
                      LEARNS per-router lock duration instead of a blind 60s.
  pixiewps.py       Native pixie-dust, all 5 modes; numpy-vectorized seed search
                      for the time-seed/eCos brute. Consumes wsc_crypto.
  campaign.py       WpsCampaign — the Focus-facing async orchestrator (mirrors
                      wep/campaign.py): pixie-first → brute sweep, lock/backoff,
                      progress/ETA, pause()/resume(). Owns the "one TX activity
                      on a half-duplex radio" invariant.
```

**Layering rationale (the Lead-level discussion):**

- `wsc_crypto.py` is the floor — *pure*, no state, no I/O — so it's provable
  offline before any hardware, and both the brute engine *and* pixie sit on it.
- `WpsRegistrar` is protocol-only: inject-callback in, RX-queue out, result
  enum back. Same spirit as the WEP daemons — testable against an in-process
  fake enrollee with zero hardware.
- `WpsCampaign` is the only object the UI talks to. Like `WepArpReplay` it's
  the single TX activity on the radio, so it gets `pause()/resume()` and the
  campaign owns association lifecycle, the PIN iterator, lock/backoff, and ETA.
- `WlanInterface` is **unchanged** — we lean entirely on existing `send_raw`
  + `register_rx_callback`. No driver/chip touch (answers "do we alter
  chipsets?" — no).

RX routing: the campaign registers an RX callback, filters to frames
to/from our forged MAC + target BSSID with ethertype `0x888e`, and feeds an
`asyncio.Queue` the registrar awaits — the low-latency path, bypassing the
UI-polled registry entirely.

## Where we try to beat reaver/bully (the v1 speed bet)

1. **One association across many PINs.** *Neither* tool does this — both
   re-associate every PIN. If even some of the fleet tolerate back-to-back EAP
   sessions on one association, that deletes the biggest per-attempt cost.
   `WpsAssociation` keeps the assoc alive and only re-associates on a failed
   exchange; `timing.py` measures whether keeping it alive triggers *harder*
   rate-limiting on a given router (the honest risk to watch).
2. **Per-AP adaptive timing**, pushed past bully: learn each router's real
   M-latencies and drive the inter-attempt gap toward *that router's* floor,
   not a global default. Event-driven async RX — no `setitimer` blocking model.
3. **Smarter, measured lock backoff.** Both tools sleep a blind fixed interval
   (reaver 60s, bully 43s) and re-poll the beacon. The fleet is the rig to
   *measure actual lock duration + trigger per model* and back off to that.

## PixieWPS (native, all modes)

Pairs naturally with the brute engine: pixie needs only **M1..M3** and is
*offline*, so the campaign tries pixie-dust **first** and falls back to online
brute only if the AP isn't pixie-vulnerable. The vuln: many APs derive the
secret nonces E-S1/E-S2 from a weak PRNG; recover them offline and the
10⁴/10³ half-PIN search runs against the captured E-Hash1/2 with no further
on-air traffic. Some modes (RTL819x) even decrypt M5/M7 to yield the WPA-PSK
directly.

Modes to port (cost = pure-Python concern):
- **Mode 1 — Ralink/MediaTek/Celeno**: O(1) LFSR state-recovery, no search.
  Plus the two trivial auto cases (E-S1=E-S2=0; E-S1=E-S2=E-Nonce). These are
  the *majority* of real hits on ~10-yr-old gear. Trivial in Python.
- **Mode 2 — Broadcom eCos**: ≤2²⁵ LCG steps. Tens of seconds plain Python;
  numpy-vectorize.
- **Mode 3 — Realtek RTL819x time-seed**: glibc `random()` seeded by `time()`;
  brute the Unix-timestamp seed over a window (full `--force` ≈ 2³¹). **This is
  the part that's ~100–1000× slower in pure Python** — port the `glibc_fast_seed`
  1-word pre-filter and vectorize the seed sweep with numpy; bound the date
  window by default.
- **Modes 4/5 — eCos simplest / Knuth**: full 2³² LCG/MINSTD index search;
  numpy-vectorized, experimental.

All standard crypto is stdlib; only the ~4 small buggy PRNGs need hand-porting.

## Milestones

```
M1  wsc_crypto.py — DH g5, HMAC-SHA256, KDF, AuthKey/KeyWrapKey, AES-CBC,
      PIN checksum, check_pin_half. Offline, unit-tested vs known vectors.
M2  messages.py — WSC TLV codec + EAP/EAPOL framing; build M2/M4/M6, parse
      M1/M3/M5/M7/NACK. Offline self-consistency vs a fake enrollee.
M3  registrar.py — per-PIN state machine, inject+RX-queue driven. Tested
      against an in-process fake enrollee (no hardware).
M4  association.py + FIRST LIVE single PIN on hardware — forge auth/assoc,
      run one known-wrong PIN at a real router, confirm M1..M4 + a first-half
      NACK. Instrument latencies.                       ← first hardware proof
M5  campaign.py + pins.py + lock.py — full two-halves sweep, checksum/ordering/
      resume, beacon-IE + 3-strike lock detect, adaptive backoff. Cracks a
      known-PIN router end-to-end.
M6  Single-association keep-alive + timing.py adaptive per-AP timeouts; measure
      across the fleet (re-assoc-per-attempt vs kept-alive; per-router floor).
                                                         ← shippable online attack
M7  pixiewps.py — native all 5 modes; numpy-vectorized time-seed/eCos; wire
      pixie-first into the campaign. Tested vs pixiewps's own vectors.
M8  Focus UI — WPS panel: pixie-first button + brute toggle + live PIN/ETA +
      lock status. PASSIVE BY DEFAULT — all TX behind explicit buttons
      ([[feedback_passive_by_default]]).
```

M1–M3 are pure offline build (no hardware, fully unit-tested) — de-risk the
crypto + protocol exactly like the WEP cracker. M4 is the ground-truth
hardware checkpoint (spec is the kernel/hostapd WSC, but the live pcap is
truth). The fleet drives the M6 timing work.

## Constraints / house rules

- **Passive by default** — WPS PIN/pixie are active TX; gate them behind
  explicit Focus buttons, like the WEP frag/chop toggles
  ([[feedback_passive_by_default]]).
- **No real network identifiers in commits** ([[feedback_no_ssids_in_commits]]) —
  test PINs/BSSIDs from the fleet stay out of git.
- **Root-cause, not band-aids** ([[feedback_no_bandaids_root_cause]]) — if an
  exchange is flaky, diff our M-frames against a reaver/bully pcap; don't paper
  over it with retry loops.

## Open questions for the Lead (before M1)

- **Test target**: which fleet router is the permanent WPS dev target (a la the
  dd-wrt WEP box [[project_wep_test_router]])? Pixie-vulnerable one ideal for
  M7, but M4/M5 want a plain WPS-PIN AP.
- **Resume state**: adopt bully's per-BSSID `.pins` (shuffled order) + `.run`
  (checkpoint) files, or fold WPS PIN progress into the planned SQLite layer
  from `NEXT-STEPS.md` ("User persistence + decloak DB")?
- **numpy**: confirm adding numpy as a dep is acceptable (needed for the
  vectorized time-seed/eCos pixie search at interactive speed).
- **Fake-enrollee test harness**: build a minimal in-process WSC enrollee for
  M2/M3 unit tests, or capture a real M1..M8 pcap from the fleet and replay it?
  (Lean: build the fake enrollee — it makes the two-halves oracle testable
  without hardware, then validate against a real pcap at M4.)

## Status — 2026-05-27

**Online brute engine built + offline-proven (31 tests).** Files:
`wsc_crypto.py` (DH g5 / HMAC-SHA256 / KDF / pure-Python AES-128 anchored to
FIPS-197 + NIST CBC), `messages.py` (WSC TLV + EAP/EAPOL/WFA framing, M2/M4/M6
build, M1/M3/M5/M7/NACK parse, M7→Network-Key extract), `registrar.py` (per-PIN
state machine + split-PIN oracle), `association.py` (live `WlanTransport` +
WPS-registrar assoc), `pins.py` (two-halves keyspace + checksum), `lock.py`
(beacon-IE + 3-strike detect, *learned* backoff), `campaign.py`
(COMMON→first-half→second-half sweep, single kept-alive association, `.run`
resume under `captures/`, ETA). Decisions honoured: **single-association v1**
(re-assoc only on loss; `transport.drain()` between attempts), **pixie deferred**
(no numpy/glibc this iteration). Hardware probe: `scripts/wps/wps_probe.py`
(`--pin`, default wrong+correct pair, `--campaign`).

### Verified facts (hardware, TestAP1 / RTL8821AU) + the bug it caught

- The full EAP path works on-air: assoc → AP sends **EAP-Req/Identity** (body
  `"hello"`) → our Identity response → AP sends **M1** (~426–436 B, opcode
  WSC_MSG, msg_type 0x04) as an EAP-Request.
- The WSC message MUST be bounded by the **EAP length field**, not the end of
  the frame — anything trailing the EAP packet (chip-side padding, future
  hardware metadata) would otherwise leak into the next Authenticator HMAC
  (`HMAC(authkey, M_prev ‖ M_curr)`), which covers the raw WSC bytes. Symptom
  before the fix: identity passed (no authenticator), but **every M2 was
  rejected** — AP retransmitted M1 ~9× then sent **WSC_NACK with
  config-error 2** (`WPS_CFG_DECRYPTION_CRC_FAILURE`). Fixed in
  `messages.parse_rx_frame` (slice `[attrs_start : e+eap_len]`); regression test
  `test_parse_rx_strips_trailing_fcs`.
- WSC opcodes `WSC_Start=1 ACK=2 NACK=3 MSG=4 Done=5`; msg-types `M1=0x04…
  M8=0x0c WSC_ACK=0x0d NACK=0x0e DONE=0x0f`; vendor-id `00 37 2A`, vendor-type
  `00 00 00 01`. Assoc WPS-registrar IE = `00 50 F2 04 | 104A 0001 10 | 103A
  0001 02` (reaver `WPS_REGISTRAR_TAG`).

### Verified working end-to-end (2026-05-27, TestAP1)

Post-FCS-fix, a single `--pin <correct>` run walked the full
identity→M1→M2→M3→M4→M5→M6→M7 exchange and recovered the PSK. **The first-half
oracle (M5) and PSK extraction (M7) both confirmed on real hardware.**

### Top speed lever: link-layer ACK (the "why so many M1" finding)

The AP retransmits each message ~15× because **our injected STA never sends an
802.11 ACK** — it's not a firmware-level client, so the chip's MAC filter
doesn't match our forged MAC and hardware auto-ACK never fires. We reply to
every retransmit (our only delivery insurance, since we TX no-ACK), which is why
it converges — but it's ~15× the frames per attempt, i.e. the dominant per-PIN
cost over an 11k sweep. **Getting the card to auto-ACK** (program the chip MACID
to our forged MAC + enable HW ACK in the current RX mode) would cut each attempt
to ~7 frames — a ~10× speedup, directly serving the speed goal. Per-chip MAC
work, so it goes through the careful per-driver hardware loop, not a blind edit.
Bully's `--noacks` exists for the same reason.

### Pending (user will hardware-test; agent does not run hardware)

- **Full `--campaign` sweep on hardware** — single-PIN is confirmed; the
  COMMON→first-half→second-half sweep + lock backoff + `.run` resume still want a
  real multi-attempt run (and will be ~15× faster once auto-ACK lands).
- **UI Focus integration (M8)** — a WPS panel + start/pause behind an explicit
  button ([[feedback_passive_by_default]]). Not yet wired.
- **PixieWPS (M7)** — deferred; revisit the dependency question (numpy for the
  time-seed/eCos search) before building.

## PBC (push-button) capture — design (2026-05-27)

Opportunistically grab the PSK when someone opens a WPS **Push-Button
Configuration** walk window (~120 s, `WPS_PBC_WALK_TIME`).

### Role flip: we're the Enrollee; the PSK is in M8
PBC = the AP is the **Registrar** (button-pressed, holds the creds), we're the
**Enrollee**. The message polarity inverts vs the PIN attack:

```
PIN  (us=Registrar): AP sends M1/M3/M5/M7 (Req) ;  we send M2/M4/M6 (Resp)
PBC  (us=Enrollee):  AP sends WSC_Start/M2/M4/M6/M8 (Req) ; we send M1/M3/M5/M7/Done (Resp)
                                                                  PSK is in M8 ↑
```
EAP request/response polarity is unchanged (the AP is always the authenticator),
so our framing is reused as-is — we just build the *other* WSC messages and the
prize moves M7→M8.

### Why it must be active (no passive eavesdrop)
M8's credential is encrypted under KeyWrapKey ← AuthKey ← DHKey ← `g^(ab) mod p`.
A passive listener sees PKe/PKr but neither private key — recovering the secret
is the Computational DH problem over the **1536-bit MODP group 5**, i.e.
infeasible (≈ a 1536-bit discrete log, well past the 1024-bit nation-state
borderline). This is the one cryptographically-sound part of WPS. So reaver/
bully/pixiewps all work by *being* a live endpoint; a captured third-party
exchange yields nothing (no private key → no AuthKey, so even pixie can't run on
it). **Conclusion: the only way to the PSK is to be the enrollee that completes
the handshake — we must race the legit device, not sniff it.**

### Detection — passive, always-on, zero TX
The walk window is advertised in beacons/probe-resps: **Device Password ID =
0x0004 (PBC)** + **Selected Registrar = 1** (both already parsed). Derive
`AccessPoint.wps_pbc_active` and edge-trigger on OFF→ON. (Confirm
`wps_selected_registrar` is stored on the model, not just parsed.)

### Reuse vs new
Reused: `wsc_crypto` (100%), `messages` framing + encrypted-settings decrypt,
`association` (assoc IE flips `RequestType` Registrar→Enrollee 0x01), the test
harness (flip it to play Registrar). New, small: enrollee builders M1/M3/M5/M7
(M1 carries `DevPwId=PBC`), parse WSC_Start + M2/M4/M6/M8, **M8 `ATTR_CRED`
(nested) → Network Key**, the **PBC device-password constant** (public/fixed —
verify from hostapd), enrollee identity `WFA-SimpleConfig-Enrollee-1-0`.

### Behaviour / decisions (locked 2026-05-27)
- **`w` toggle: off → selected → global**, session-only, default **off**.
  "selected" lights up once a future *AP watchlist* exists (user marks APs → hop
  only their channels → auto-target for deauth / downgrade / PBC). Global first.
- **Overlap: ignore `MULTIPLE_PBC_DETECTED` on our side** — never self-abort. We
  win by **finishing first** (the AP still aborts true simultaneous overlaps),
  with DoS-and-retry across repeated presses as the fallback. **Speed-bound:** a
  real phone finishes PBC in ~2–3 s; our no-ACK retransmit flood makes us ~tens
  of seconds, so the race hinges on the **auto-ACK speed lever** above + starting
  the instant the beacon flips (before their device associates).
- **Armed + window detected** → pause channel-hop, tune to the target, capture,
  resume hop. **Disarmed + window** → loud Scanner banner only (no TX).
- **Focus**: a focused AP going PBC-active → auto-capture (already on-channel).
- **Ethics deferred** — global auto-invade grabs bystanders' PSKs the instant
  they press their own button; revisit before release (PRE-RELEASE).

### Milestones
```
P1  WpsEnrollee — enrollee state machine (EAPOL-Start→Identity→M1..M7, extract
     PSK from M8, WSC_Done). Offline-tested vs a fake Registrar (flip the
     harness). Verify the PBC device-password constant.            ← pure offline
P2  PBC detector — AccessPoint.wps_pbc_active + OFF→ON edge event. Parse-only.
P3  WpsPbcCapture — associate as enrollee, run WpsEnrollee, ignore overlap,
     return SSID+PSK, save like other captures. scripts/wps/pbc_probe.py (press
     the AirLink button to test).                                  ← 1 button-press HW test
P4  Scanner UX — `w` off/selected/global, the banner, the armed hop-takeover.
P5  Focus integration — auto-capture on a focused AP's PBC window.
P6  Ethics/safety pass — eligibility, loud logging, PRE-RELEASE note.
```
P1–P3 are engine + offline-testable + one button-press. P4–P5 are UI/orchestration.

### Status — 2026-05-27 (hardware-verified through P3)

- **P1–P3 DONE + hardware-verified.** Full PBC capture works on the AirLink:
  press-and-hold the WPS button → `pbc_probe.py` walks M1..M8 and recovers the
  PSK. Confirmed both via `--now` (blind, after pressing) and via the detector
  (press while it watches → "PBC window OPEN" → capture). Field note: this AP
  only advertises DevPwId=PBC + SelectedRegistrar in beacons while the button is
  **held long enough to open the window**; a short tap shows nothing (which is
  why an early run read pbc_active=False — correct, not a bug). Some APs never
  advertise it at all → use `--now`.
- **P4 (Scanner) + P5 (Focus) DONE (offline-tested; live-TUI test pending).**
  Scanner `w` cycles off→selected→global (session-only); a passive 1 Hz watcher
  banners every opening window and, when armed+eligible, pauses hop → tunes →
  `WpsPbcCapture` → resumes. Focus auto-captures a window on its target (already
  on-channel; gated to one attempt, once per BSSID, and only when no other TX
  activity owns the radio). Recovered PSK stored on the AP + saved to
  `captures/<ssid>_<bssid>_<ts>.wps`. PbcWatcher/save unit-tested;
  the Textual wiring needs a live run to confirm.
- **P6 (ethics/eligibility pass) remains** — global auto-invade grabs bystanders'
  PSKs; revisit before release (PRE-RELEASE).

## Hard-MAC WPS gap (2026-05-31)

The baseline hardware sweep surfaced a pattern: **WPS (PIN + PBC) works on every
firmware-based card** (AR9271, RTL8188EUS, RTL8821AU, RTL8812AU, RTL8822BU,
RTL8814AU, MT7610U) but **fails/struggles on the two oldest hard-MAC,
no-firmware, register-only parts**:

- **RTL8187L** — PBC timed out; PIN sent guesses but only got NACKs, no crack (❌).
- **RT2500USB** — PBC timed out; PIN got valid first-half-wrong NACKs (engine works)
  but unreliable (⚠️).

Both cards pass deauth, handshake, PMKID, and WEP ARP/ChopChop — so association +
injection work; only the *longer, more stateful* WPS EAP exchange fails. Leading
hypothesis: these parts have no hardware auto-ACK / different TX timing, and the
WPS state machine's no-ACK retransmit flood (see "Top speed lever" above) is more
fragile over the dozens of frames per PIN than the short handshake/PMKID
exchanges. Not yet root-caused — investigate the per-message timing + retransmit
behaviour on a hard-MAC card before promising WPS there. (RT2500USB's RX also
died ~1 min into the session, which may have compounded its result.)
