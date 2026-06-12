# Focus View Redesign

## Status — direction locked 2026-06-12, NOT yet built

The spatial "router-admin" redesign (originally "Idea #1" below) is the chosen
direction. The layout, the responsive story, the art convention, and a
risk-managed migration plan are agreed. Nothing is built yet. The plan is
deliberately staged so v1 is never broken, never branched, and never
throwaway-blocking — see **Migration plan**. Build only when there's time;
until then this doc is the spec.

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
- **Responsiveness** → the metaphor is endpoints-along-the-long-axis, so in
  portrait the long axis is *vertical*: router on top, card on bottom, data
  flows *down*, dashboard rows become vertical wires. Same mental model, same
  code, transposed on a Horizontal/Vertical swap at a width breakpoint.

Resulting hierarchy (the thing the spreadsheet has none of):
**headline (what) → flow channel (proof it's working) → endpoints (who) →
clients (secondary targets) → log (details, on demand).**

---

## Locked layout decisions

- **Campaign headline**: center-top, surrounded by lots of negative space to
  emphasize it. The one big "listening / attacking / cracking / cracked" focal
  point. This is the new heart of the view — it absorbs the live state that
  CAPTURE used to carry.
- **Flow channel**: the packet dashboard, stretched between card and router as
  the centerpiece (finally given the room it deserves). Data flows right→left
  (router → card); injections/deauths flow left→right (card → router). A deauth
  lighting the channel red and fading back is the long-term flourish.
- **Card endpoint** (left in landscape / bottom in portrait): one clearly-a-
  wireless-card art. Static facts *above* (model, chipset/driver); dynamic
  *below* (what the card is doing — replaying / chopping / cracking / PIN
  attempt %, ETA).
- **Router endpoint** (right in landscape / top in portrait): one clearly-a-
  router art. Static facts *above* (ESSID, channel, encryption); dynamic
  *below* (beacons, signal, clients, handshakes/IVs).
- **Clients**: a **list** (not per-client connector lines — those are an O(n)
  layout problem only a list solves cleanly), pinned under the router,
  **bottom-right half**. Compact rows: MAC · power · pkts · per-row deauth.
  Broadcast-deauth button at the top of the list.
- **Event log**: **bottom-left half**. The hard-won <40-char log lines are
  finally justified — fixed, capped width, never expands.
- **Degradation on tiny terminals (80×40)**: collapse art to a single glyph,
  keep headline + one flow line + log. Same "gut irrelevant things when
  real-estate is small" rule that today collapses PACKET ACTIVITY, but applied
  to a layout that degrades gracefully along *one* axis instead of a 6-box grid
  reflowing in two.

### Mockups

Landscape (wide):

```
                   ● Listening for handshake · -71 dBm
   \ /    beacon  ──────────────────────────<- 9s   \ /
 __\Ｖ__  data    ──────────────────────────<- 1s  __\Ｖ__
 / Alfa/  inject  0s ->──────────────────────────  |NETGR91|
 /__o_/   eapol   ──────────────────────────<- 0s  |__o___|
 rtl8187l                                            NETGEAR91
 ┌ LOG ───────────────┐   CLIENTS (2)
 │ Target locked.     │   ··── fa:..:aa  -79  10  [✂]
 │ M1 ▸ M2 captured   │   ··── aa:..:ff  -80 134  [✂]
 └────────────────────┘            [ Deauth all ]
```

Portrait (narrow / phone SSH) — same scene, axis rotated, zero new concepts:

```
    NETGEAR91  ch6 -71dBm
        |NETGR91|
   clients: 2  [deauth all]
   ··fa:aa -79  [✂]
   ··aa:ff -80  [✂]
   ─────┼─────  ● handshake
   beacon ▼ data ▼ eapol ▼
   ─────┼─────
      __\Ｖ__  Alfa
   ┌ LOG ──────────┐
   │ M1 ▸ M2 ok    │
   └───────────────┘
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

---

## Deferred (explicitly NOT in v1 of the redesign)

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

- **Coexist, don't branch.** v2 is a **new file** `focus_v2.py` (a second
  Screen), selected behind a flag (env var `WIFIT3_FOCUS_V2=1` or a hidden
  dev keybind). `focus.py` stays the default and is untouched except for
  importing the shared view-model. Both screens install. No long-lived branch →
  nothing to diverge or conflict → never blocked.
- **Abandon path** if v2 doesn't pan out: `rm focus_v2.py` + delete the flag.
  No reverts. The view-model extraction stays as a v1 cleanup.

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

## Open testing/sizing questions (carried over, still unresolved)

- Bare-minimum supported dimensions — 80×40? 100×80? 120×80? Everything above
  expands to fill.
- Most common real terminal sizes to test (laptop / desktop / mobile-SSH
  portrait + landscape / low-res / high-res, small vs big fonts).
- How to size Windows Terminal to an exact width/height for repeatable testing.
- Does Textual support portrait/landscape-conditional box rendering (flexbox-
  like)? The width-breakpoint axis-rotate above assumes yes via CSS + a
  Horizontal/Vertical swap — confirm the cleanest mechanism.

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
