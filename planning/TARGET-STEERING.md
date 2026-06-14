## Target Steering
Adjusting wireless card behavior dynamically to improve signal/capture quality. 

### Dynamic channel re-steering (handshake)

**Problem.** Focus stays glued to the entry channel. If the AP CSA-jumps or shows
stronger signal on another band, we miss it.

**Approach.** Periodically probe nearby channels (<100 ms each) and re-tune. Ties
into ESSID-based targeting (one logical AP, multiple BSSIDs across bands).

**Complexity.** Moderate — touches the Focus channel/hop logic.

### Target-based RX gain steering (focus)

**Problem.** When focused on one target, we passively accept whatever RX gain the
card's stock dynamic-gain loop lands on. On Realtek that loop is DIG — it drives
the initial gain (IGI, `0xc50`) to **minimize false alarms**, which is *not* our
goal. Our goal is to **maximize the target's frame rate**. Nobody does this —
airodump certainly doesn't; it takes whatever the driver gives. Biggest payoff is
the weak/marginal AP near the sensitivity floor — often exactly the handshake
we're straining to capture. (Purely RX-register tuning → passive, no TX.)

**Approach — same knob, different objective.** DIG already *is* a steering loop;
we want the same knob driven by target-beacons/sec instead of FA counters. The
kernel even has the hook: `odm_pause_dig` pins IGI and takes the automatic loop
out. So focus-steering = *pause the card's auto-gain, hill-climb its gain on the
target's observed frame rate, restore auto-gain on un-focus.*

Keep it from metastasizing across all ~13 drivers by splitting **the control loop
(generic, once)** from **the knob (tiny, per-card)**:
- An optional capability Protocol — `RxGainSteerable`: `gain_bounds()`,
  `get_rx_gain()`, `set_rx_gain(v)`. Realtek implements it as the IGI read/write we
  already have in the DKMS port (`0xc50`); setting gain pauses that card's DIG
  watchdog. Other families (rt2x00 link-tuner, mt76 AGC, ath9k) expose their own
  analogous gain/AGC watchdogs we could override the same way — each is ~3 methods,
  not a re-implemented loop.
- The hill-climb controller lives **once** in the Focus/`WlanInterface` layer:
  count the target's frames over a window, perturb gain ±1, keep the move if the
  rate improved, back off otherwise, with hysteresis. Card-agnostic.
- A driver that doesn't implement the Protocol simply gets no "steer" toggle —
  graceful, no steering code copied anywhere.

**The control realities (design around, don't wish away).** The feedback signal is
slow + noisy — ≤10 beacons/s, so telling 7/s from 8/s needs multi-second windows;
each hill-climb step costs seconds and the loop needs hysteresis/confidence or it
oscillates on noise. Per-frame RSSI (a sample every frame, not one per 100 ms) is a
faster proxy worth folding in. And the optimum drifts (RF is non-stationary), so
it's a *continuous* controller, not converge-and-stop.

**Honest caveat — gain steering helps weak targets, ~nothing for strong ones.**
The whole 8188eus beacon-rate hunt established that a strong AP's losses were
*external* (host load / USB power) with IGI already sitting more sensitive than the
kernel — steering it would move nothing. So this is a weak-target tool.

**Validate before building the abstraction.** Throwaway sweep first, reusing the
DKMS `0xc50` read/write + `beacon_watch` counting: pin IGI at each value
`0x1c…0x2a`, measure a **weak** AP's beacons/sec for ~10 s each, plot it. Peaked
curve (a best IGI that beats the DIG default) → real signal, build the
`RxGainSteerable` capability + generic controller (prove on the DKMS card, then
extend per-card). Flat curve → gain isn't the lever; drop it before writing any
cross-card layer. Pick a weak AP — the strong canary will read flat and tell us
nothing.

**Complexity.** Controller: moderate. Per-card: low (≈3 methods) *if* the
capability split holds. The validate-first sweep is cheap and decisive — do it
before committing.
