# Campaign Abstraction — Proposal

**Status: PROPOSAL for Lead review — NOT yet implemented.** Drafted autonomously
while onboarding PMKID as a stoppable, radio-locking attack. Nothing in this doc
has touched the campaign system code; it's the "here's what I came up with" for
*"adding a new campaign should not be a nightmare of 5 files wiring each other's
buttons together."*

---

## The problem, concretely

A "campaign" = a long-running attack that **owns the half-duplex radio**, so it
must lock the other TX attacks out (and surface a Stop). Today there are four:
WEP replay, WPS PIN, WPA3 downgrade, WPS PBC. Onboarding a fifth (PMKID) means
hand-editing **~13 spots across 3 files**, every one a place to get the wiring
subtly wrong:

| # | Touch-point | File | What it is |
|---|---|---|---|
| 1 | `Campaigns` field | focus_model.py:39-42 | `wep: Any = None` |
| 2 | `other_long_running_tx` branch | focus_model.py:50-57 | the radio mutex, one `if` per attack |
| 3 | `derive_buttons` block | focus_model.py:522-570 | hand-written idle/running label+variant+disabled |
| 4 | `ButtonStates` field | focus_model.py:506-510 | one field per attack |
| 5 | `card_dynamic` branch | focus_model.py:612-621 | `● replaying` etc. |
| 6 | `derive_headline` block | focus_model.py:657-695 | priority + progress line |
| 7 | `*_status_markup` fn | focus_model.py:216/246/343 | campaign-specific progress |
| 8 | screen handle field | screen.py:197-201 | `self._wep_campaign = None` |
| 9 | `_campaigns()` wiring | screen.py:254-258 | bundle handle → Campaigns |
| 10 | `_tick()` teardown | screen.py:424-434 | completion detection |
| 11 | `on_button_pressed` branch | screen.py:567-572 | `btn-X → _toggle_X()` |
| 12 | `_toggle/_start/_stop/_launch_X` | screen.py:646-809 | 3-4 methods each |
| 13 | teardown in `_enter_target` + `action_go_back` | screen.py:301-304, 854-857 | stop on leave |

The user's flinch at `ButtonStates(gen_ivs=…, chop=…, pmkid=…, wps_pin=…, wpa3_down=…)`
is touch-point #4 — the most visible symptom of a deeper thing: **the generic
behaviour of every campaign is hand-written per campaign instead of derived from
one declaration.**

---

## The dividing line (from the full coupling audit)

### Generic to EVERY campaign — the machinery that should be wired once
- **Handle lifecycle**: optional handle on the screen, bundled into `Campaigns`,
  read-only in `focus_model`.
- **Radio mutex**: "is any *other* campaign running?" (`other_long_running_tx(exclude=me)`).
- **Button toggle**: idle → `label`/`variant`; running → `Stop …`/`error`;
  `disabled = not eligible or other-TX-running`.
- **Dispatch**: `btn-X` → start if idle, stop if running.
- **Teardown**: stop on target-change and on back/quit.
- **Completion sweep**: each tick, if a running campaign reports done → tear down.
- **Log wiring**: constructor `log=` callback → `screen._log`.

DEV: I can't *stand* the log callback. We should absolutely be doing event messages. Callback, sure. Whatever. But EVENTS that the receiver then decides how to render (log, toast, dialog).

### Genuinely campaign-SPECIFIC — stays bespoke, must not be forced uniform
- **Eligibility** predicate: `is_wep(ap)` / `ap.wps and not ap.wps_locked` /
  `ap.wpa3 and ap.transition_mode` / PMKID's `(not ap.wpa3) or ap.transition_mode`.

DEV: every single one of those decides based on `ap.*` so.. I feel like `eligible(ap) => bool` in the Protocol covers this pretty cleanly?
Those are all attributes on ap, right? No special plumbing required... I don't see a downside to this

- **Run mechanism**: TX-loop task (WEP/WPS) vs RX-callback dispatch (WPA3) vs
  one-shot exchange (PBC/PMKID).

DEV: RX Callback fundamentally is just TX-loop task right? Wait for RX, send TX, that's ChopChop in a nutshell?
That's ARP Replay as well? (wait for ARP, replay ARP)? And WPA Downgrade is.. well it's not well implemented. I don't think that'll see alpha; I'd rather have EvilTwin/FakeAP.
Let's assume that WEP just does not fit nicely into the campaign model due to how it was written. I get that.
So let's break down WEP into the basic features. The campaign is complicated, but it also glues together the different phases seamlessly...

1. Start ARP (ChopChop is not an option)
  a. "ARP Listen Mode": Listen to RX data from target, for replayable ARP packet (length)
    i. Step #2 (Chop Chop) is an option *any time*. Not required, not blocked by anything.
  b. Try replaying the ARP once (burst) and check if AP responds (replayable packet) (Can get stuck here FOREVER [no clients]).
  c. "ARP Replay Mode": Once we find a packet, spam it. "Spam it" carries lots of weight (brief bursts, optimizing TX pps based on the AP's outgoing ivs/second).
2. ChopChop packet forging
  a. Listen for choppable packet (length) in RX.
    i. "Stop Chop" is *always* an option once ChopChop has started.
  b. Once a choppable packet is see in RX stream, slowly forge the packet byte-by-byte via ChopChop replay attack.
  c. Once the packet is forged, execute Step 1b: Try replaying the <chopchop-packet>.
    i. If replayable, **end ChopChop campaign, return packet** (1c: ARP takes packet & "spams it").

This is like like 2 (ish?) campaigns in one:
1. WEP ARP replay campaign (only WEP attack option)
2. WEP ChopChop Campaign (only available when ARP replay campaign is active)
1.5. WEP Crack "Campaign"? (subprocess, automatically starts only when ARP replay campaign is active AND we have more than 10,000 "usable IVs")
  - I feel like this fits more as a task within ARP Replay. There only output of WEP Crack is the cracked result or "failed; not enough IVs".
  - it's not user driven at all, automatically started, no way to stop it (except by ending the entire Replay campaign).

...My point is 1a and 2a are both "Listen for RX packet(s)", and "send TX" (initial small burst attempt, later huge spam wave).

ARP becomes "paused" when ChopChop is active. ChopChop *does use* ARP's "1b" (check-if-replayable), but this is still considered part of the "ChopChop" campaign.

- **Completion signal**: `recovered_key is not None` / `state.phase == "done"` /
  none (manual only) / `task.done()`.

Enumerated Event callback would handle this. But yes, each attack has different events.
Idk how to do this cleanly...

```
if evt.type == WEP_CHOP_FORGING:
    logtree.branch("forging chopchop packet {evt.status}")
elif evt.type == WPA_PMKID_M1_EMPTY:
    logtree.fail("M1 does not contain PMKID [dim](AP...)[/dim]")
elif ...
```
or like `if evt.group == wep: _wep_event_handle(evt)` if we want to split into helpers.
But I do like the idea of all of the logging/markup in a single place. THAT is a nice feature.
But traceback from log message to code is.. well now it's very misdirected. Gotta look where an 'evt' is fired with `WEP_CHOP_FORGING`...

I don't have a clean answer, but I feel like sample code can help reduce these "hard problems" like ("Display") into solvable things.

- **Display**: headline/card/status markup — WEP's IV counts + chop verb, WPS's
  ETA + hard/soft lock countdown, WPA3's probe-response tally. These are NOT
  uniform and pretending they are would be a leaky mess.

Two campaigns can't run at the same time. Or shouldn't at least. ARP & ChopChop are separated ("Paused ARP Replay" could just as easily be "Stopped/Ended ARP replay" and we just restart it again to "Unpause").

So whatever campaign is active should get ownership of:

1. Main status area. All of it. Multiple lines.
  - That means ARP Replay needs to know about cracking. That means ChopChop needs to know about Cracking.
  - Kind of an unavoidable mess for WEP but that's the cost of a cleaner UX. And this seems like the lesser of all evils.
  - Main status area shouldn't change during the campaign.
2. "Card Activity" underneath the card ANSI art. 1-liner. Usually says "- Injecting" or "Trying PIN" or whatever. like *what the card is doing at that time*
3. "Additional status area" (underneath / within sparklines). WPA uses this for Encryption & WPS status. WPS uses this for Fake Auth status, something else... Usable IVs for WEP?
4. Log area. Don't have to worry about logtrees getting interrupted.
5. All RX & TX. No other campaigns can see RX (WPC can't see it while another campaign is active0. No other campaigns can use TX. Just 1 campaign at a time. Period.

- **Idiosyncrasies**: WEP's ChopChop sub-attack + PTW process-pool; WPS's on-disk
  `.run` resume + lock state-machine; PBC's no-button auto-trigger.

DEV: I feel like this all fits inside of a "run loop" for the campaign. Campaign starts -> Runloop starts. Campaign ends when Runloop ends.


---

## Proposed shape — a Protocol + a Spec registry

Two layers, deliberately split so the **machinery** is table-driven while the
**specifics** stay on the campaign where they belong.

### 1. `Campaign` protocol — normalises the runtime lifecycle
```python
# engine/attacks/campaign.py
@runtime_checkable
class Campaign(Protocol):
    name: str                          # mutex key: "wep"/"wps"/"wpa3down"/"pmkid"
    def start(self) -> None: ...
    async def stop(self) -> None: ...  # async-normalised; sync ones just return
    @property
    def is_done(self) -> bool: ...     # completion signal, campaign's own logic
```
Existing classes satisfy it with a thin shim: WEP's `is_done` → `recovered_key is
not None`; WPS's → `state.phase == "done"`; WPA3's → `False` (manual stop only).

DEV: I don't see why `is_done` needs to be a thing, Status Events can signal the end of a campaign (teardown, stopping of campaigns).

### 2. `CampaignSpec` — one static declaration per campaign, for the UI machinery
```python
# ui/campaign_registry.py
@dataclass(frozen=True)
class CampaignSpec:
    name: str                              # matches Campaign.name + Campaigns key
    button_id: str | None                  # None → no button (PBC: mutex-only)
    eligible: Callable[[AccessPoint], bool]
    idle_label: str;  run_label: str
    idle_variant: str = "primary";  run_variant: str = "error"
    start: Callable[[Screen, ap, iface], None]   # constructs + .start(), stores handle
    headline: Callable[[Campaign], list[str]] | None = None   # per-campaign display hook
    card: str | None = None                # card_dynamic label, e.g. "● replaying"

CAMPAIGN_SPECS = [WEP_SPEC, WPS_SPEC, WPA3_SPEC, PBC_SPEC, PMKID_SPEC]
```

DEV: Not sure about the "idle" stuff...
- Does idle handle the Button labels & styles?

start(), yep the runloop.

eligible: exactly what I was thinking.

`button_id`: ...makes sense, also the nullable part (invisible campaigns, PBC).
- We add to hook to it, we know which one to *not* disable during the campaign. We could change it to "Stop"? Is that the `idle_*` stuff?

headline: "display hook", hmm. So.. when would we call headline()? On every tick()?
- Every tick kind of makes sense, like for WEP we're getting thousands of IVs/sec, updating that on every-single-IV is crazy on UI performance. Ideally we'd just fetch the IV counts from the god "ap" target object inside of headline().
- I wanted to use Events for everything (including all statuses/cards) but.. yea callbacks for statuses solves the performance problem (only calculate status every tick instea dof every incoming IV).

`card_dynamic` Nice, yes that's the one under the card ANSI art... Just a str. I guess we fetch it every tick.
- Note that there's a UI bug where we hide the `card_dynamic` label when it's empty, causing the wireless card to "move down" when it hides and "move up" when there's a new status. It's a bit jarring, nothing too crazy. Just FYI.


### How it collapses the 13 touch-points
| Touch-point | After |
|---|---|
| mutex (#2) | `any(c.name != exclude for c in active_campaigns)` — **one loop, no per-attack branch** |
| buttons (#3, #4) | `derive_buttons` loops specs → `ButtonState`; running-state from the active set. **No hand-written block, no per-attack field** |
| dispatch (#11) | `on_button_pressed` maps any `btn-*` to `_toggle_campaign(spec)` generically |
| toggle/start/stop (#12) | one generic `_toggle_campaign` (start via `spec.start`, stop via `await handle.stop()`) |
| tick + teardown (#10, #13) | one loop: any `is_done` → drop; on leave → stop all active |
| card / headline (#5, #6) | spec's `card` string + `headline` hook — still per-campaign, but **referenced from one registry, not scattered if/else chains** |
| handle (#8, #9) + Campaigns field (#1) | kept as named fields for now (1 trivial line each); a later dict migration removes even these |
| status markup (#7) | **stays bespoke** — lives on the campaign / its paired display hook |

**Net: ~8 of 13 touch-points collapse into a single `CampaignSpec` entry.** The
remaining ~3 are display, which is honestly campaign-specific — but they move from
*scattered `if campaigns.wep is not None: …` chains* to *one hook per spec*.

DEV: toggle? Start/Stop I get.. but is Toggle just for the WPS-PBC case?


---

## Onboarding a new campaign — before vs after

**Before (PMKID today):** edit all 13 spots above.

**After:** write the attack class (the real, unavoidable work) + add ONE registry entry:
```python
PMKID_SPEC = CampaignSpec(
    name="pmkid", button_id="btn-pmkid",
    eligible=lambda ap: (not ap.wpa3) or ap.transition_mode,
    idle_label="PMKID", run_label="Stop PMKID",
    start=lambda screen, ap, iface: screen._launch_campaign(
        PmkidHarvestCampaign(iface, ap, log=screen._campaign_log)),
    card="● harvesting",
)
```
The button, mutex, dispatch, handle lifecycle, and teardown all come for free.
*That* is the greased express lane — and PMKID is its first rider.

DEV: I mean, sometimes we want to say *why* an attack is not eligible when we first load the Focus view. It's like a one-time thing.
"AP has PMF:Required therefore Deauth and PMKID etc has been disabled".
Maybe we could have it join them together in one treelog:
 - "Deauth X Disabled because of PMF:Required"
 - "PMKID X Disabled because of PMF:Required"
 - "Handshake X Disabled because of SAE3-Only"

---

## Honest limits

- **Display does not fully collapse.** WEP's IV-count headline and WPS's lock
  countdown are irreducibly specific; the spec carries a `headline` hook, it does
  not unify them. The win is concentrated on button/mutex/lifecycle/dispatch.

DEV: I agree, give the campaign carte blanche on the campaigns.
If None, fallback to the Idle state.

- **`Campaigns` stays a dataclass** in the incremental plan (one trivial field per
  campaign). A dict-keyed `Campaigns` removes even that but touches every
  `campaigns.wep`/`.wps` reader — a bigger blast radius, deferred to its own pass.

---

## Sequencing (incremental, each step independently green)

- **Phase A — build the seam + onboard PMKID through it.** Add `Campaign`
  protocol + `CampaignSpec` + registry. Make `derive_buttons`/mutex/dispatch/tick
  registry-aware. Onboard PMKID as the 5th campaign *via the registry only*.
  Existing four keep their hand-written blocks → **zero behaviour change to WEP/
  WPS/WPA3/PBC**, full risk isolation. This both ships PMKID and proves the lane.
- **Phase B — migrate the existing four, one at a time.** Each: write its
  `CampaignSpec`, delete its hand-written block, confirm behaviour-preserving +
  tests green. WPS PBC declares `button_id=None` (mutex-only, no button).
- **Phase C (optional) — `Campaigns` → dict**, deleting the per-campaign fields.

---

## Open decisions for the Lead (these fork the implementation)

1. **Is PMKID a first-class campaign** (shows in headline/card like WEP/WPS) or
   **log-only** (locks + Stop, but no headline real-estate)?
2. **Stop semantics on teardown**: does Stop / leave still fire the leaving-deauth
   + `clear_fake_mac` so we never leave the card armed? (I believe yes — teardown
   must be honest about radio state.)
3. **Progress logging mechanism** (ties straight into this): the attack emits a
   semantic `PmkidPhase` enum that `pmkid_log.py` maps to markup (keeps the engine
   markup-free, consistent with the `PmkidFail` enum you just shipped) vs a raw
   markup `log=` callback like WPS uses today. Recommend the enum.
4. **Do Phase A only now** (ship PMKID on the new seam, leave the four), or **commit
   to Phase A+B** (migrate everything) as one effort?
