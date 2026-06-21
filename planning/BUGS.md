# Wifit3 — Known Bugs & QoL

---

## Tag + suppress EAP/Enterprise handshakes — High priority (correctness, small)

An EAP (enterprise) 4-way is captured and currently emitted as "crackable,"
but its PMK comes from the EAP/MSK exchange, not a passphrase.
hashcat `-m 22000` can't touch it. Extend the "crackability gate" (handshake.py) so
an EAP-negotiated 4-way is withheld + badged "EAP/Enterprise" rather than reported as
a capture.

---

## BUGS

### Hardware-failure UX — pre-alpha (release blocker)

Failures must surface *in the Textual UI*, not just in dev-only `wifit3.log`
(`$WIFIT3_LOG`). Three tiers:

1. **Fatal** (e.g. no libusb backend) — modal with message + stack trace; Copy +
   Quit. **Done-ish.**
2. **Functional** (card wedged → replug) — modal with message + *opt-in* stack
   trace; OK (→ splash, whose 1 s re-discovery poll auto-recovers on replug) or Quit.
   **Not started.** Init-time is the easy half — delete the drivers'
   `except Exception: return False` swallows so the cause propagates to one
   splash-worker catch; the runtime off-thread wedge (RX-reader thread, no `await`
   to `raise` into) is the hard, design-first half.
3. **Informational** (copied, deauth sent, handshake/PMKID/WEP) — non-blocking
   Textual toasts (already mirrored in the TUI LOG). **Not started.**

Human-in-the-loop confirms *if/when* each fires and its wording.

---

## WPS PBC auto-invade can monopolize the radio on timeout (Focus)

PBC auto-invade is ON by default and works well, but in Focus a PBC attempt that
times out keeps retrying for the rest of the AP's PBC window, and other attacks
are blocked for that span. Give it manual control — a **Stop PBC** button (and a
**Start PBC** when a window is open) — and/or bound the retry loop so a single
timeout can't hold the radio. Minor; deferred.

First-contact angle (shrinks these timeout windows): a zero-EAPOL timeout is usually a
lost EAPOL-Start (or auth/assoc) — the AP stays silent and the enrollee burns its full
~2–3 s wait before the outer retry. Resending EAPOL-Start every ~700 ms during
first-contact (before the first EAP-Request; never mid-exchange, where it would reset the
AP) recovers a lost first frame in <1 s instead of a whole retry. Small, isolated change
in `WpsEnrollee.run()`.

## 5 GHz injection likely broken on mt7921au (PAU0F) — unverified TX path (flag, not root-caused)

Observed (human, vs an AC66U's 5 GHz band): WPS-PBC **and** PMKID both FAIL on the 5 GHz
AP but WORK on 2.4 GHz, on the PAU0F. 5 GHz **RX** is confirmed (`MT7921AU.md`: beacons on
CH36/44/149/157), so this isolates to **TX/inject**: RX hears the AP, our injected frames
never land. The TX gate (`verify_pcap` CHECK 4) byte-matched only *2.4 GHz* aireplay
captures, so the `band_5ghz=True` branch in `tx._tx_rate_val` (OFDM rate swap) is
**unverified** — prime suspect. Cross-cutting: active-station WPS on 5 GHz fails on ANY
card whose 5 GHz inject is unverified, not just the PAU0F — and every active-station HW
test so far is 2.4 GHz. To chase: capture a 5 GHz aireplay TX, diff the driver's 5 GHz
inject path against it, fix the rate/path. Flagged, not yet root-caused.

## 5 GHz drivers under-list DFS channels the cards support (deferred — DFS ≈ empty air)

Every 5 GHz driver **except** `rtl8814au_dkms` advertises the byte-identical 9
non-DFS channels (`36,40,44,48,149,153,157,161,165`, DFS=0) — RTL8812AU / 8821AU /
8822BU / mainline-8814 / MT76x0U / MT76x2U / RT2800USB. That identical list across 7
unrelated chipsets is a copy-paste porting decision, **not** derived per-card: their
capture `iw.log`s show `iw set channel 52/100/144` returning **0** (mt76x2u, mt7921u,
rt5572 confirmed), i.e. the cards + regdomain *do* tune DFS. So those drivers refuse
channels the hardware supports. (`rtl8814au_dkms` lists all 25 incl. DFS 52–144,
byte-verified + live-hopped; it just excludes them from the *default* hop — see below.)

**Deliberately deferred, not urgent.** DFS (UNII-2, 52–144) is radar-shared so most APs
avoid it → usually empty; omitting it means faster hop cycles and few-to-no missed APs.
This is also why only `rtl8814au_dkms` hit the `bulk_in` Windows-timeout bug above — it
is the only driver that hops the empty DFS channels that produce long timeout runs.

**To add DFS later (per driver — NOT a blind list edit):** the porters who truncated the
list likely never exercised the DFS *tune paths*, so a driver with non-DFS-sized sub-band
tables would mis-tune (garbage / crash) if you just appended the channels. Do the 8814
treatment: (1) confirm `iw` accepted it in the capture (`return 0`), (2) byte-verify the
driver's `set_channel` reproduces the capture's DFS tunes, (3) then extend
`SUPPORTED_CHANNELS`. The DFS infra is already in place and stays: `wlan/channels.is_dfs`
(52–144), the scanner's non-DFS default hop, and the Channel-Filter `[d]fs` opt-in.

---
