# Focus View Redesign

## Status — 2026-06-18: **v2 is the DEFAULT (step 4 done) — v1 kept behind `WIFIT3_FOCUS_V1=1` for the soak; delete v1 later**

The spatial "router-admin" redesign (originally "Idea #1" below) is the chosen
direction; the full v1 layout is pinned to the cell (see **Locked layout
decisions** + **Mockup**). v1 is **landscape-only** — portrait is deferred to its
own later feature (rationale in Deferred). The migration is staged so v1 (today's
Focus) is never broken, never branched, never throwaway-blocking — see
**Migration plan**.

**Done & shipped to `origin/master`** (5 commits, `c84d362`..`7b05904`): the
throwaway-able **shell** behind `WIFIT3_FOCUS_V2=1` — region-per-module package
`ui/screens/focus_v2/` painted from `ui/focus_model.fake_snapshot()`, plus the
shared `ui/ansi_art.py` (black→transparent art) and `scripts/ui/shoot_focus_v2.py`
(headless SVG/geometry/text harness). v1 `FocusView` untouched + default. Layout
proven at 80×24 → 180×45, geometry locked by `tests/ui/test_focus_v2_layout.py`,
`1226` tests green. **Steps 2.1–2.x of the Migration plan are complete.**

**Done — step 3a–3c (Migration plan §3):** v1's campaign-value derivations are
extracted into the shared `ui/focus_model.py` as pure functions + a
`build_snapshot()` factory (incl. the synthesized CAMPAIGN HEADLINE); v1's
`update_ui` calls them (behavior-preserving, tests green). `FocusViewV2` now
builds + paints a real `FocusSnapshot` each tick — live headline, endpoints,
clients, event log (capture pipeline duplicated from v1, with auto-save), flow
channel fed real `packet_stats` deltas, and the rainbow signal bar on the router
power line. `fake_snapshot()` is kept as the no-target fallback. Verified by
`tests/ui/test_focus_v2_capture.py` (mock interface, no hardware).

**Done — step 3d:** v2's attack + per-client/broadcast deauth BUTTONS are wired.
The topbar composes the full attack set (5 stable ids); `derive_buttons` shows
only the ones that fit the target + drives their label/variant/enabled each tick
(Start/Stop toggles, the cross-attack TX mutex). The screen-side handlers +
campaign lifecycle (PMKID, WPS PIN, WPA3 down, WEP Replay/Chop, WPS-PBC
auto-capture) are duplicated from v1 — the log/save/teardown side effects stay
per-view by design; only the derivations are shared. Verified by the v2
button-wiring test (no live TX). v2 is now feature-complete behind the flag.

**Done — step 4 (Migration plan §4):** v2 is the default Focus screen. The flag
flipped — v1 `FocusView` is now the fallback behind `WIFIT3_FOCUS_V1=1` (the
inverse of the old `WIFIT3_FOCUS_V2`), kept as a zero-cost escape hatch during
the soak. WEP / WPS / WPA (PMKID + handshake) all exercised live on hardware.

**Remaining — delete v1 (deferred):** once v2 has run as daily-driver a while,
delete `focus.py` + its v1-only tests (`tests/ui/test_focus_capture.py`) and the
`WIFIT3_FOCUS_V1` branch in `ui/app.py` in one sweep. The shared `focus_model`
brains stay. Kept for now as the field fallback + behavior source-of-truth.

---

## Diagnosis — why the current Focus view fails

(Grounded in `screenshots/wifit3-3-focus-handshake.png` + `src/wifit3/ui/screens/focus.py`.)

- **It's flat, not ugly.** Six panels (TARGET / SECURITY / CAPTURE / PACKET
  ACTIVITY / CLIENTS / EVENT LOG) all carry the *same* bright-blue title bar at
  the *same* weight. Six identical shouting bars = the eye has nowhere to land.
  The flatness is literally made of repeated chrome. The redesign deletes most
  of those bars (the card/router *are* their own labels; the headline and flow
  channel need none).
- **The element you love most is the most cramped.** PACKET ACTIVITY — the live
  proof of what Wifit3 is doing — is squished into a ~18-cell top-right corner,
  while the EVENT LOG (which never needs width) eats the right two-thirds. The
  single biggest win of the redesign is promoting the packet flow from corner
  afterthought to the **spine** of the view.
- **Scanner already proves the target pattern.** `wifit3-2-scanner.png` works:
  one hero (the AP table) + a log band pinned along the bottom. One focal point,
  one timeline. Focus fails because it has *six* heroes and no timeline anchor.
  Making Focus *consistent with Scanner* means giving it the same DNA — one
  focal scene + a bottom log band — NOT preserving the six-box grid.
- **Real estate is mis-distributed, not truly tight.** The whole left column
  under the lone attack button is dead space. The problem is distribution (top
  row dense, log hogging the rest), so the fix is to redistribute, not to invent
  space.

---

## Organizing principle — flow axis + two endpoints

The one sentence the whole design hangs off:

> **Two endpoints — my card and the target router — sit at opposite ends of the
> terminal's *long axis*. Between them is a flow channel (the packet dashboard)
> where packets visibly move. Everything else attaches to the endpoint it
> logically belongs to.**

A spatial metaphor *is* a hierarchy, which is exactly what the flat grid lacks.
"My card is here, the router is there, data flows between" is understood
pre-verbally. The ANSI art is just the skin on that skeleton — not the point.

Each previously-open question collapses once this principle is accepted:

- **Clients** belong to the *router* (they're its connected devices) → pinned at
  the router endpoint.
- **The log** is narration, never spatial, never wide → a band along the *short*
  axis.
- **What guides the eye** → the dead center-top becomes the single big CAMPAIGN
  headline ("what am I doing right now").
- **Responsiveness** → landscape **width tiers** via Textual's
  `HORIZONTAL_BREAKPOINTS` (confirmed in 8.2.5: it auto-applies a CSS class per
  width breakpoint, so sizing is pure CSS keyed off `-compact`/`-normal`/`-wide`,
  with no resize-watching glue). **Portrait is deferred** to its own later
  feature — the Scanner view isn't portrait-friendly either, so a portrait Focus
  would be inconsistent, and dropping it removes the only fiddly part (the
  endpoint order-reversal). Endpoints are still built as dockable regions so a
  future `-portrait` breakpoint is mostly added CSS, not a rewrite.

Resulting hierarchy (the thing the spreadsheet has none of):
**headline (what) → flow channel (proof it's working) → endpoints (who) →
clients (secondary targets) → log (details, on demand).**

---

## Locked layout decisions

Landscape only. **80-col floor**; the full scene is comfortable at ~30 rows and
must not *break* at 80×24 (24 rows is the true classic-terminal default, not 40).
The **20-col endpoint width** is set by the `.ans` art (itself sized to hold a
full BSSID, `aa:bb:cc:dd:ee:ff` = 17, beneath it).

- **Top "action area"** (fixed height ≈3): the back button + the
  encryption-conditional attack buttons live together top-left — all the
  clickables in one place ("argh that didn't work, lemme go back"). The **status
  line** fills the remaining width and stays centered (it's usually short —
  "Cracking", "Replaying", "PIN 8021/11000"); on a very wide terminal it floats
  centered in the gap. Up to 3 status lines.
- **Only LOG and CLIENTS are bordered/titled panels.** Card, flow channel, and
  router are borderless/title-less — the card and router *are* their own labels;
  dropping that chrome buys the flow channel its width.
- **Flow channel** (centerpiece, vertically centered): the packet dashboard
  stretched between the endpoints. 5 rows — `beacon · data · (wep iv | eapol,
  encryption-gated) · inject · deauth`. All flow **right→left** (newest at the
  right, scrolling left — you read the attack's recent history L→R); no
  directional arrows, the motion *is* the direction. **Labels right-aligned,
  numbers left-aligned**, both flush against the bars. Trailing number = running
  `/s` for the continuous rows, a recent **count** for `eapol` (a handshake is
  ~4 frames). **Custom** sparklines (Textual's `Sparkline` is single-series),
  **adaptive height**: 2-row (16 levels) when there's vertical room, 1-row when
  cramped. `deauth` kept (it's the frame we inject — honesty over the
  "INJECT/DEATH" optics; can de-stack by row order if it grates).
- **Card endpoint** (left, vertically centered): the card art, then static facts
  — **chipset/driver + the card's own BSSID** when the driver exposes it (not the
  marketing name, often unresolvable from VID:PID) — then the dynamic line
  (replaying / chopping / cracking / PIN % + ETA). The card BSSID lives here, NOT
  in CLIENTS (our card isn't the target's client). Buttons moved to the top
  action area, freeing vertical room in this column.
- **Router endpoint** (right, bottom-aligned so its info row lines up with the
  card's): **power + signal *directly above* the router art; the ESSID *directly
  below* it** (the name labels the router), then BSSID and `ch · WPA2/CCMP`.
  Splitting the ESSID away from the power line spreads the labels out instead of
  clustering them above the art. The art is trimmed to its last non-blank row, so
  the freed row sits above the power line rather than as dead space below. No "N
  clients" line (redundant with the CLIENTS header).
- **Clients**: bordered list, **fixed exact-fit width** (BSSID · pwr · pkts ·
  button), left-aligned rows, each with an **inline `[✕]`** (white-on-red) — one
  click deauths that client, no select-then-act. Broadcast `[ Deauth all ]`
  pinned at the top.
- **Event log**: bordered, bottom-left, **fluid width** (it expands; CLIENTS
  stays fixed). The <40-char lines mean it never *needs* width, but logs are the
  priority so they get the slack.

### Vertical height ladder

Height fills in a deliberate order, so the view stays dense rather than stretched:

1. **Top** band fixed (~3 rows; up to 3 status lines).
2. **Mid** band (card · flow · router) grows first, capping once the sparklines
   reach full 2-row height and the endpoint columns fit (~13 rows). A floor is
   reserved for the bottom so ≥3 clients always show.
3. **Bottom** band (LOG · CLIENTS) takes everything beyond that — so a taller
   terminal shows *more log lines and more clients*, not taller sparklines.

Shrink → 1-row sparklines + a few clients; grow → 2-row sparklines lock in, then
log/clients keep expanding. (Width: endpoints fixed, flow + log fluid, clients
fixed.)

### Mockup — landscape schematic

WPA2 target mid WPA-downgrade (every region populated). Schematic, *not*
cell-exact — the live shell render is in the SVG shots / `scripts/ui/shoot_focus_v2.py`
text dumps. Sparklines 2-row; labels right-flush, numbers left-flush.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ‹ Scanner   Extract PMKID   WPA Downgrade   WPS Brute Force   ● WPA Downgrade   │
│                                                                deauthing 2 …    │
│                                                                M1 ✓   M2 —      │
│  __▌__             beacon ▇▇███████▆▆▆████████  7/s           ▂▄▅█ -71 dBm      │
│ /Alfa/             data   ▆▇████████████████████ 180/s         NETGEAR91        │
│  ‾‾‾               eapol  ▅▅████████▅▅██▅▅▅▅▅██ 10              \   /            │
│ rtl8187l          inject  ▅▅████████▇▆██▃▄▅▆██ 34/s            ▟███▙            │
│ 00:c0:ca:..:33    deauth  ▅▅████████▆▅██▃▃▅▆██ 13/s             NETGEAR91       │
│ ● deauthing                                                    a8:fc:b7:..:42   │
│                                                                ch 6 · WPA2/CCMP │
│╭ LOG ─────────────────────────────────╮╭ CLIENTS (5) ─────────────────────────╮│
││ 19:42:01 Target locked.              ││          [ Deauth all ]              ││
││ 19:42:04 M1 captured (ANonce)        ││ fa:11:22:33:44:aa  -79   10    [✕]    ││
││ 19:42:06 Deauth ▸ 04:2e:…:b8         ││ 9c:b6:d0:1a:2b:3c  -67  512    [✕]    ││
│╰──────────────────────────────────────╯╰──────────────────────────────────────╯│
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Art convention

- **One art per endpoint is the bare minimum**: unmistakably a router,
  unmistakably a wireless card, with lines connecting them. Build the whole
  layout against ONE generic card glyph + ONE generic router glyph.
- Prefer **stylized box-drawing glyphs we control cell-by-cell** over rigid
  literal art: they animate (antenna buzz on TX, beacon pulse, flowing packets)
  and reflow predictably. Per-card art that's a different size per chip breaks
  alignment and the responsive rotate.
- **Animation must be instrumentation, not decoration**: a buzz means TX is
  actually firing; a pulse means a beacon actually landed. Throttle frame rate
  over SSH/pihole. (We already run a 30 FPS easing timer for the signal bar, so
  this is cheap.)
- **Green-LED breathe convention**: art cells painted dark green `rgb(0,128,0)`
  are the animation targets — the breather lerps them `(0,128,0) → (0,255,0) →
  (0,128,0)` on a ~1.5 s cycle. The `.ans` art self-describes its live cells, so
  art edits and animation code never couple (paint a cell dark green → it
  breathes). v1 = always-breathe; gating the breathe on real events (card LED
  while RX flows, router LED on an actual beacon) is the additive instrumentation
  upgrade.

---

## Deferred (explicitly NOT in v1 of the redesign)

- **Portrait / axis-rotate.** Deferred to its own later feature. The Scanner view
  isn't portrait-friendly, so a portrait Focus would be inconsistent; deferring
  also drops the endpoint order-reversal (the only fiddly part of the responsive
  story). Endpoints stay dockable so a `-portrait` breakpoint is mostly added CSS
  later, not a rewrite.
- **Per-chipset ANSI art.** Delightful, but the single thing most likely to eat
  days and block the layout. It's a pure skin / lookup table once the metaphor
  is proven — add it *after* the layout is nailed, never before.
- **Per-client connector lines** (each client a node wired to the router, deauth
  = severing the line). The coolest idea in the brainstorm, but per-client lines
  are the hardest thing to keep robust across sizes. Ship the compact client
  *list* first; revisit literal lines only if they survive the portrait squeeze.

---

## View model — decouple the brains from the layout

The reason a redesign *feels* like it throws away all the campaign-value wiring:
in `update_ui` the **hard-won logic and the rendering are fused.** E.g.
`_update_crack_section` both *computes* the crack state machine (samples →
"Starting…" / "Cracking…" / "N/10k usable IVs") AND calls `.update()` on a
specific `#lbl-crack` widget. The expensive part (the state machines) is welded
to the disposable part (which widget, what CSS).

**De-risking move: extract the campaign-value derivation out of `update_ui` into
a plain view-model — pure functions taking `(ap, iface, campaigns)` and
returning render-ready strings/numbers.** This is a *behavior-preserving*
refactor of v1: it keeps passing v1's existing tests, ships safely, and is a net
cleanup even if the redesign never happens. After it, the *layout* is the only
disposable thing — the brains are shared by both views.

What moves into the view-model (the reusable brains, today fused in `focus.py`):

- SSID display chip + truncation; BSSID; channel.
- Last-beacon staleness → now / orange chip / red chip (+ `_format_duration`).
- WPS+PMF compact line (lock glyph); `_wps_status_markup` (lock countdowns,
  p1/p2 phases, ETA); WPA3-down status line.
- Attack-button eligibility (is_wep gating, WPA3-transition, PMF, the
  cross-attack TX mutex `_other_long_running_tx`).
- Windowed beacon rate (`_beacon_samples`) + count → signal-bar target.
- Handshake instance counting (complete / partial / per-message breakdown) +
  persisted-history back-fill; PMKID counts + persisted.
- WEP: IV count/rate, `_replay_status_markup`, `_update_crack_section`,
  `_update_fakeauth_line`.
- Client rows (`_refresh_clients`).
- **NEW (what v2 needs that v1 doesn't): a synthesized CAMPAIGN HEADLINE** — one
  line naming the dominant current activity, derived from the same states above.

Already shared / done right (both screens import as-is, no work): the
capture-event pipeline (`capture_events.py`, `capture_log.py`), encryption
formatting (`encryption_format.py`), the signal bar (`signal_bar.py`), and the
`PacketDashboard` widget (`widgets/packet_dashboard.py`).

Shape: a per-tick snapshot (dataclass) the layout just paints — roughly
`target{…}`, `security{…}`, `capture{…}`, `headline`, `buttons{eligibility}`,
`clients[…]`. The layout does no derivation; it reads fields and places them.

---

## Migration plan — coexist, do NOT fork

The whole point is to never break v1, never diverge a branch (no merge
conflicts / cherry-pick reverts), and keep a one-command abandon path.

- **Coexist, don't branch.** v2 is a **new package** `ui/screens/focus_v2/`
  (region-per-module widgets + a `FocusViewV2(Screen)`), selected behind a flag:
  `app.py:on_mount` installs `FocusViewV2` as the `"focus"` screen when
  `WIFIT3_FOCUS_V2=1`, else the v1 `FocusView` — so Scanner's existing
  `push_screen("focus")` transparently lands on whichever. The shared view-model
  lives at `ui/focus_model.py`, **outside** the package, imported by both
  screens. `focus.py` stays the default, untouched but for that import. No
  long-lived branch → nothing to diverge → never blocked.
- **Abandon path** if v2 doesn't pan out: `rm -rf focus_v2/` + delete the flag
  branch in `on_mount`. One directory, no reverts. `ui/focus_model.py` stays as a
  v1 cleanup (it's shared).

Order (each step protects the next):

1. **Extract the view-model** from `update_ui` (behavior-preserving; v1 stays
   green on its existing tests). Safe and useful regardless of whether v2 ships.
2. **`focus_v2.py` shell fed FAKE static data**, behind the flag. Hardcoded
   SSID/clients/headline + the art + the flow channel. No campaign wiring yet.
   Resize it, SSH from the phone, rotate to portrait. **This is the unconfirmed
   part** — "does the card→router flow actually look good at 80×40 and in
   portrait" — so prove it in an afternoon of pure-throwaway shell code BEFORE
   porting anything. If the shell looks bad small, bail cheaply here.
3. Only if the shell holds: **wire the shared view-model into v2.** Campaign
   values flow in for free — no re-derivation, because step 1 already separated
   them from layout.
4. **Flip the default** when v2 wins; delete `focus.py` when happy. Or
   `rm focus_v2.py` if it doesn't.

At no point is v1 broken, branched, or throwaway-blocking. The only disposable
artifact is one new file.

---

## Testing/sizing — resolved

- **Dimensions**: 80-col floor (classic terminal width). Full scene comfortable
  at ~30 rows; must not break at 80×24 (the real classic default — 40 rows is
  generous). Everything above expands to fill (flow channel + LOG/CLIENTS first).
- **Repeatable sizing needs no real terminal**: Textual's
  `app.run_test(size=(w, h))` pins exact dimensions headless, so geometry tests
  reproduce 80×24 / 80×30 / 120×40 deterministically (and the phone-SSH portrait
  size once portrait is built — just feed its cols×rows).
- **Conditional box rendering**: yes — `HORIZONTAL_BREAKPOINTS` (8.2.5)
  auto-toggles a per-width CSS class; layout swaps are pure CSS off that class.
  No Textual CSS media-queries needed.
- **Autonomy split**: geometry / placement / overflow / width-cap, and "the
  animation fires on the right event", are agent-verifiable via Pilot +
  `widget.region`/`.size`; the aesthetic go/no-go is the human's, fed by exported
  SVG screenshots at each size.

---
---

# Appendix — original brainstorm (source material)

Preserved verbatim-in-spirit; superseded where it conflicts with the decisions
above (e.g. "support both views" → the coexist-behind-a-flag migration; clients
→ list, not connector lines).

## Focus View: Layout Problems

Some things I was thinking when I realized I really don't like the Focus UI:
- Top row is extremely information dense (TARGET INFO/SECURITY/CAPTURE/PACKET ACTIVITY).
  - I think SECURITY and CAPTURE could be swapped?
  - Or just.. there's gotta be a cleaner way to visualize a target, it's clients, attacks, and the live stats, attack progress...
- Logs get WAY too much horizontal space on a wide screen.
- I optimized for small terminals and a consistent overall UI look, but we made it ugly for wide screen / extremely high resolution.

Testing is just me maximizing and resizing the terminal to "a normal size":
- I think the gold standard is 80x40? or 100x80? I think we went with 120x80 maybe, as the bare-minimum supported dimensions. Everything else expands to fill.
- SSH: Just clone & build wifit3 on the pihole?
- iPhone SSH app -> Computer -> Wifit3 (Portrait + Landscape)
- I need to figure out how to size my Windows Terminal to an exact width/height.
- I need to know what are the most common Terminal resolutions (Laptop, Desktop, Mobile, Low resolution, High resolution, small fonts, big fonts)

Fluid design:
- Does Textual support "box" rendering that changes depending on Portrait/Landscape screen dimensions? Like Boostrap or whaever. Flexbox.


## Focus View: Complete Redesign?

Requirements:
- Access point information (sent by AP)
  - Static: BSSID, ESSID, Channel, Encryption/Cypher
  - Dynamic: Power, Beacons, Signal Bar, Last seen
  - Static/Dynamic? WPS Lock, Protected Management Frames (PMF).
- Clients table
  - Each client: BSSID, Power, Packet num, Fingerprint [not implemented yet] (Apple/Samsung/FireTV/Ring Camera).
  - Including buttons to deauth a specific (selected) client or "all"/broadcast.
  - Brainstorming: Maybe a red 'deauth' button next to every client BSSID (float:right), deauth button only appears on hover/selection.
  - Brainstorming: "broadcast deauth" button aligns to top of clients table.
- Attack buttons (Extract PMKID, WPA Downgrade, WPS Brute Force).
- Event Log, indicates current state (listening, attacking, cracking, cracked).
  - Most - if not all - log lines have been reduced to within a certain width ( < 50 chars?)
  - We can have a set width for the log, it doesn't have to expand.
- Packet Activity Dashboard
  - We have to keep this, it looks so cool and is great at visalizing what Wifit3 is doing.
  - Currently 5 rows high. We could split "data" to 2 separate line graphs "data" and "ivs"
  - Show be more prominent in Focus view.
- Overall: Consistent UX regardless of screen width/height
  - Gut irrelevant things when real-estate is small (the weay we collapse "PACKET ACTIVITY" right now is a good example of this)

Problems:
- Screen real estate is TIGHT. We thoroughly truncated pretty much every Log line and panel label.
  - We have shortened button labels to become basically meaningless: "Chop", "WPS PIN", "WPA[down]"
  - Panel borders & padding is eating a lot of screen real estate.\
- Showing lots of information in a super-easy-to-understand way.
- Showing lots of information that looks good on both Portrait, Mobile, high resolution (super wide & high, 200x100 or higher), low resolution (80x40).
- Multiple aeras subtly indicate when signal is dead (Last Seen=red, Signal=dimming heartbeat "X", should be more prominent.

----------

## Focus Redesign Idea #1: Visualize like a Router Admin Page

Picture of router. Picture of wireless card. List of clients. Logically grouped. Lines connecting them.

- Left side, full column: ANSI art wireless card
  - WiFI bars radiating out (always animated?)
  - Wireless card model & driver/chipset directly below card.
  - Wireless card uptime? Do we have access to this on the device? We can track it but a warm boot would lose the actual uptime, maybe we don't add this...
- Right side, lower-half: ANSI art router
  - WiFi bars radiating out (animated on beacon?).
  - Router name, BSSID directly below router.
  - Router channel, Power, Signal Bar - Directly above router, above/between antennas.
  - Security: Encryption, Cipher, Cracked Password - Underneath router name/bssid.
- Middle, Between Card and Router:
  - Top 3rd: Attack buttons, capture status
  - Middle 3rd: PAKCET ACTIVITY stretched between Card & Router, full length history, flows right-left (Card <- Router), i.e. the flow of data.
  - Bottom 3rd: Clients list, aligned directly "to the left" of the Router ANSI art.

Where does Event Log fit?
- Underneath the card? Should be enough space on lower 1/3rd, stretch to fill to the right, up to the left side of the clients table.
- Footer? That eats real estate for log lines that are never > 50 chars long.

Behavior:
- Packet Graph shows data flowing right-to-left from AP to the Card (Card <- AP)
  - Injections & Deauths should flow in the opposite direction, left-to-right (Card -> AP)
  - Deauth bar "lighting up" during deauth attacks would be cool, highlight the target being deauthed with red background, slowly fades back to normal.
- Separate lines for each client pointing to the router. I don't know if we can capture to/from on client<->AP packets, I feel like this is possible...
- Scanner->Focus transition should slide Focus VIew in from the right.
  - Focus View needs a large obvious "Back to Scanner" button on the upper(?) left side of Focus view, takes user back to scanner (sliding back out).
- Indicate which client we captured the handshake on? Could reuse that green "[check]HS" icon we have in Scanner view.
- Toast notifications for: Handshakes, PIN cracks, PBC, PMKIDs, WEP cracks
  - Red Toast when no beacon seen for X seconds (30 sec?) - OK to repeat 30sec later, reminds/nudges the user.
  - Orange on when deauthing a "PMF: Optional" AP: "Router advertises Protected Management Frames (PMF), deauths likely will not work, try WPA Downgrade"


### ANSI Art Mockup
```
[  Extract PMKID  ]                                            Power: -71dBm
[ WPS Brute Force ]                                            Beacons: x,xxx
[  WPA Downgrade  ]                                            [ Signal Bar ]
                                                            
     \  /      beacon __________________________________<-9s     \  /
    __\/___      data __________________________________<-1s   ___\/__
   / Alfa /    inject ->__________________________________0s  /______/|
  /___o__/     deauth ->________________________________<-1s |____;_;|/
                eapol __________________________________<-0s
  rtl8187l                                                      NETGEAR91
Alfa AWUS036H                                               xx:yy:zz:xx:yy:zz

+--------------------------------+     CLIENTS (2)     PWR    Pkts                         
| Target Locked.                 |   ff:ee:dd:cc:bb:aa -79dBm   10 [ Deauth ]
| starting attack ...            |   aa:bb:cc:dd:ee:ff -80dBm  134 [ Deauth ]
|                                |                       [ Deauth Broadcast ]
|________________________________|
```

OK the art does help show how cool it would look if we had ANSI blocks and proper router / wireless card artwork.
Completely unique UX. Super easy to understand.
I'm not entirely sure how we'll corral the Log and Client Table...
The ansi art design above still doesn't include Encryption, Capture status, current operation (cracking)
We could easily add a new row for IVS/sec, wait I think eapol & ivs swap depending on WPA/WEP.
WPS PIN, PBC, Handshake, PMKID, or Handshake+PMKID Captured!" bold black on green underneath "eapol" row.
Likewise WEP Crack result bold black on green underneath "ivs" row.

I guess there's room underneath the wireless card to indicate what it's doing during WEP ("Replaying" "Chopping" "Cracking")
And for WPA PIN we could show the PIN attempt, %, ETA...

Although the campaigns have a TON of room at the top center, room for a "CAMPAIGN" panel ("Active Attack" or something).
</content>
</invoke>
