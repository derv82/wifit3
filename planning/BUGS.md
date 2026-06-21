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
in `WpsEnrollee.run()`. (Decided against the ~700 ms resend: it inflates the ~20-EAPOL
cleanliness metric we gauge active-monitor ports by. A fix must NOT add to the count —
e.g. a single resend only *after* the first timeout, or a faster outer retry.)

## 5 GHz injection broken on multiple cards — wrong TX rate for the band

Two cards confirmed (human, vs an AC66U 5 GHz band): PAU0F (mt7921au) and AWUS036ACM
(mt76x2u). On each, WPS-PBC **and** PMKID FAIL on 5 GHz but WORK on 2.4 GHz; 5 GHz **RX**
is fine, so it isolates to **TX/inject** — RX hears the AP, our frames never land.

**mt76x2u — root cause confirmed:** `tx.py` hardcodes every injected frame to CCK 1 Mbps
(`_TXWI_RATE_CCK_1MBPS = 0x0000`). CCK is 2.4 GHz-only, so on 5 GHz the rate is invalid and
the PHY drops the frame. Fix: peep the channel (>=36 → 5 GHz) and pass an OFDM rate to
`build_txwi` (need the mt76x02 OFDM-6 Mbps rate value from source). CCK was a deliberate
2.4 GHz choice (basic rate the AP always accepts), so keep CCK on 2.4, OFDM on 5.

**mt7921au — separate flavor:** its `tx._tx_rate_val` already has a `band_5ghz` OFDM
branch, so the CCK story doesn't apply — its 5 GHz failure is a different bug (band flag
not reaching, wrong rate value, or more than rate). Needs its own look.

**Not universal:** rtl8812au_dkms (AWUS036ACH) injects fine on 5 GHz (WPS-PBC + PMKID both
worked first try). So the *inject* gap is per-driver (mt76x2u + mt7921au), not fleet-wide.

## mt76x0u — weak 5 GHz RX (sensitivity, not TX/ACK)

On 5 GHz, mt76x0u receives but desensitized: `beacon_watch --bssid <ap> --channel 157`
yields ~3 beacons/s (mean 3.3, max 7) where a strong nearby AP should give ~10/s, and the
nearby home AP doesn't crack the top-10 list. Inject, PMKID, and active-station WPS-PBC all
work on 5 GHz — it is purely the RX sensitivity. Likely incomplete 5 GHz RX calibration
(LNA / AGC / per-channel gain) in the port. 2.4 GHz RX is fine. Needs its own look.

**Verification blocked:** no offline gate (verify_pcap CHECK 4 only matched 2.4 GHz
aireplay) and the agent can't fire live 5 GHz TX. To close: extend `capture.py` to record
5 GHz inject (`--bssid2g/--bssid5g`, `--channel2g/--channel5g`), capture a 5 GHz aireplay
session, byte-diff the driver's 5 GHz inject, then human HW-test. Cross-cutting:
active-station WPS on 5 GHz fails on any card whose 5 GHz inject is broken; every
active-station HW test so far is 2.4 GHz.

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
