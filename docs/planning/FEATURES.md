# Wifit3 — Features & QoL Backlog

Known bugs live in `BUGS.md`.

---

### About page / Check-for-updates

If the user has internet connection, it's trivial to query
[the releases page](https://github.com/derv82/wifit3/releases) to fectch the latest version,
compare with the current version, and show a Toast notification about the newest version,
clicking Toast notification -> opens releases page.

We could automate this as well (opt-**in**), in Preferences: `[x] Automatically check for updates`

------------

### Vault tool: WPA-Sec submission / polling

[wpa-sec.stanev.org](https://wpa-sec.stanev.org/) is a free distributed cracking pool: submit a
handshake with an API key, poll for a result. pwnagotchi already does exactly this
([dadav's `wpa-sec.py` plugin](https://github.com/dadav/pwnagotchi-custom-plugins/blob/master/wpa-sec.py)).
Raised in [D#25](https://github.com/derv82/wifit3/discussions/25).

Why it fits: turnaround is reportedly bimodal (minutes, or never), which makes it a *free*
background attempt to fire off the moment a capture lands. Especially valuable for mobile /
low-power users who have no GPU to run hashcat against. Costs nothing while it waits.

Shape: a `VaultTool` (`vault/tools/base.py`) whose `launch()` is an HTTP POST rather than a
subprocess, and whose `poll_status()` queries the site instead of tailing a log. The ABC already
anticipates this ("executes local process ... or API request"), so no new subsystem is needed.

Open items:
- **Ask the WPA-Sec operators for permission before shipping this.** Not optional.
- API key storage (Preferences? SUbmission form? separate secrets file?).
- Opt-**in** only: This uploads the user's captured traffic to a third party.
- Dedupe: Don't re-submit a capture already submitted, and survive a restart.

### Vault: keys in `.meta`, every cracked PSK saved

The Vault pane already judges keys (CURRENT / NOT VERIFIED / STALE) and cracks only uncracked
hashlines. Left:

- Store recovered PSKs / PINs / WEP keys in the AP's `.meta` instead of `_*.txt` files. Keep
  reading the old `_*.txt` files forever; stop writing them.
- Save every PSK a job cracks. Hashcat is killed at the first potfile line and CPU Crack stops at
  the first matching line, so a job over lines holding two different passwords saves only one.
- Exhaustion (`.meta` `exhausted`) is only recorded when a wordlist runs to the end, so a crack
  records nothing. Record the wordlist that cracked them as exhausted only if the hint needs it.
- Dim "challenge (no M3/M4)" after `HANDSHAKE CAPTURED` in the scanner / focus logs.

### Show probed SSIDs

The CPU Crack / Hashcat launch modals show `Probed SSIDs (152)` but never which ones. A small
`Show` button beside it opens a modal with a read-only multi-line box of this session's probed
SSIDs (not their variants), with `Copy` and a primary `Close`.

------------

### Vault: any `.pcap`/`.cap` dropped into `captures/`

Users with captures from other tools (airodump `capture-01.cap`, etc.) could drop them into
`captures/`, open Vault, and launch their cracker with their wordlists. Today the index only
recognises `<ssid>_<bssid>_<epoch>_<kind>.<ext>` names. Reading BSSID and ESSID from the file
itself (a pcap's beacon; `dot11/` already parses frames) would also fix pcap-only APs whose
SSID the filename can't hold (emoji, non-ASCII → `____`).

------------

### Auto-Decloak Known APs

Opt-**in** only: `[ ] Auto-decloak known APs` in Preferences (`ui/pref.py`) plus
`Config.auto_decloak_known = False` (`persist/config.py`).

We already persist the SSID of every AP we have decloaked (`Config.decloaked_ssids`, BSSID ->
SSID). When a card is tuned to a channel that carries one of those BSSIDs, and that AP is
currently hidden, one directed Probe Request names it again. That is a 0.15s operation on a
radio that is already on the right channel, and the answer is already in the config file, so
there is no reason to make the user open the Decloak modal and press a button to re-learn
something we know.

Shape (undecided, several open questions):
- Fire from Scanner, or at least from Focus.
- One probe per channel hop: when `set_channel` completes, check whether any known-but-hidden
  BSSID lives on the new channel, send one directed probe, and mark it tried for the session.
  Never retry within a session, so a silent AP costs 0.15s once.
- Log it at INFO: `auto-probing {bssid} with SSID "{ssid}"`, and mention it once during scanner
  initialization (`treelog.branch`) so the user knows it is on.

Open questions, all from the hop loop:
- Fixed channel vs hopping. One probe per `set_channel` never fires for a user who is dwelling
  on one channel, which is exactly when they are most likely to want it.
- Campaign contention. Decloak *is* a campaign, so an auto-probe has to arbitrate against
  `Campaign.active` the same way, and it is not obvious whether it should yield or interrupt.
- Where the per-session attempt set lives. `Scanner` is the wrong owner if Focus also probes.

------------

### EAP-MSCHAPv2 / PEAP via Evil Twin

Most enterprise Wi-Fi is PEAP-MSCHAPv2, which cracks with hashcat `-m 5500` (DES half near-
instant via crack.sh): recovering the *domain* credential is a far higher value than a PSK,
PEAP wraps MSCHAPv2 in TLS, so it **can't be captured passively**. Stand up an Evil Twin 
so the client auths to *you*.

Some things we'll need:
- target-ESSID beacons,
- RADIUS/EAP state machine
- cert handling.

When a second hashcat mode lands (`-m 4800`/`5500`), the save layer needs a per-attack
(mode + line-format) map instead of the hardcoded `-m 22000`.

------------

### (Windows Performance) `timeBeginPeriod(1)` for timer granularity

On Windows `asyncio.sleep` floors at the 15.6 ms timer tick (a 2 ms sleep takes 15.6 ms, 102.4 ms
takes 109.1). Calling `timeBeginPeriod(1)` at startup measured 2.6 ms and 103.6 ms. It could cut
latency in the loops that sleep short intervals, but it raises the system timer rate, which can
cost CPU and battery.

Untested in the app. To adopt: compare CPU with and without it on the Scanner and on Focus
(about 5 minutes each), then on a slow device; keep it only if CPU doesn't change.

------------

### AutoDeauth settings modal

AutoDeauth has no options today. Give its button a launch modal, like EvilTwin, with
its settings persisted in `Config` (not in Preferences).

- Checkboxes to select deauth methods (some disabled when PMF Required).
- Option to deauth from every card at once, offered only when 2+ cards are plugged in.
   - Idea proposed by xxcosita3czxx in #114

------------

### Focus: Deauth dropdown

Replace the `Deauth all` button with a `Deauth` dropdown:
- `Deauthenticate`
- `Channel-Switch Announce (CSA)`
- `BSS Transition Mode (BTM)`
- `Deauth from all X cards`, only when 2+ cards are plugged in.

------------

### Remappable keybindings

A modal with its own `keybinds` section in `Config`, opened from one `Edit Keybindings` 
row in Preferences. Low priority.

Grid with each row having the functionality, current keybinding, "Default" button.

------------

### Settings as a full-screen view

Promote Preferences from a modal to a full-screen (or partial-screen, slide-up like the Vault) view,
with room for categories.

Not yet: Preferences is deliberately small (core settings users rarely change: color theme, 
where and how to save captures, how to auto-crack).

Do this only when absolutely necessary; prefer a per-feature modal over a new Preferences row.

------------

## Undecided

### Captive Portal

Evil Twin + a fake router login page: punt the client onto an open twin, serve a password form,
the user types the real PSK, then verify it against a captured handshake.

Asked for repeatedly: [issue #55](https://github.com/derv82/wifit3/issues/55) (well-written, with mockups),
and it was the 3rd or 4th such request; [D#34](https://github.com/derv82/wifit3/discussions/34)
covers the same ground from the plugin-system angle.

**For:**
- Demand is real and growing: several independent requests, plus follow-ups asking for a
  *generic* (non-brand-specific) form page à la airgeddon.
- It's the only practical vector left when the PSK is a modern random ISP default: offline
  cracking is mathematically dead there, and not every such router exposes WPS ([D#61](https://github.com/derv82/wifit3/discussions/61)).
- Not blind phishing: the entered password is verified against a real captured handshake, so
  the attack self-confirms instead of guessing.
- Someone already built it: #55's author ran a modified wifit3 and offered the source; laxdog's
  `feat/eviltwin-improvements` was already headed at open-AP + portal-HTML cloning (D#34).
  *(#55 also has a MEGA link to a binary+source drop)*

**Against:**
- Wrong layer, wrong target. Captive Portal is a *client*-side phishing attack; wifite/2/3 has
  always been *AccessPoint*-based attacks at the 802.11 layer.
- Enormous scope. Evil Twin currently tops out at Auth/Assoc/M1 (roughly Layer 2). A portal
  means a whole working AP: DHCP, DNS, IP routing, TCP, HTTP, TLS certs, ports 80/443, client
  session lifecycle, and per-client captive-portal-detection quirks.
- It's a subsystem to maintain forever, natively, without `hostapd` or `apache`; both of which
  are ridiculously complex for a reason. Plus per-brand HTML templates that go stale.
- It pushes wifit3 way up a stack the maintainer isn't well-versed in.
- *"we're an 802.11 auditing tool, not an entire wifi attack stack."*

**Where this stands:** still leaning **no**, mostly on the scope/maintenance argument, not on the
demand one. Issue #55 is intentionally left open to keep gauging demand.

------------

## Deferred / Chopping Block

### Multi-threaded CPU Crack

CPU Crack runs on one CPU thread. Not doing it now:
- The default list is small (~5s on one core).
- Pegging every core on mobile/embedded devices is a real UX/battery risk.
- Can't ship without testing on old, slow mobile/embedded devices with all cores busy.

Revisit only if users report the one-thread limit is their bottleneck.

### Key version 3 nonce correction for hashcat before 8.0.0

hashcat before 8.0.0 never cracks a key version 3 (AES-CMAC) `WPA*02` line whose stored ANonce
drifted from the real one: its nonce error correction edits ANonce bytes 63-66 instead of 64-67
(`OpenCL/m22000-pure.cl`, v7.1.2). hashcat 8.0.0 fixed it.

Workaround: write one line per corrected ANonce (captured, +/-4, both byte orders: 17 lines) with
the NC bit (`0x80`) cleared. On a drifted test line, 7.1.2 goes from Exhausted to `Recovered 1/17`.

Cost, one GPU, 29M-word list, 17 lines vs 1:
- 7.1.2: no measurable change (82-83 s vs 81 s).
- 8.0.0: +25% (29-31 s vs 23-24 s), where its own correction costs nothing.

So only for hashcat before 8.0.0: a temp hashfile at launch, after reading the local
`hashcat --version`. Never in the stored `.hc22000`.

Not observed: 0 of 67 crackable `WPA*02` lines needed it.

### WPS improvements - Low priority (who even has a vulnerable WPS router?)

The WPS engine is built, offline-proven, and HW-validated (full PIN crack on AirLink). Gaps:
- **Lock-cycle matrix** — only AirLink soft-lock tested; exercise no-lock, long cooldowns, hard-lock.
- **PixieDust (PRNG seed recovery)** — Null Secret, Static Secret, Ralink/MediaTek LFSR and
  RTL819x glibc timestamp search landed natively in `campaigns/wps/pixie.py` (#70). eCos "simple"
  is ported but kept out of `DEFAULT_MODES`: its 2^25 seed sweep measures 13.5s on a high-end x86
  laptop, and the legacy ARM phones this has to stay usable on are several times slower.
- **Gate `ECOS_SIMPLE` behind a preference** — one opt-in bool on `Config`
  (`persist/config.py`) plus a Checkbox section in the Ctrl+P modal (`ui/pref.py`), tooltip saying
  the search is very slow. eCos has no cheap pre-filter: nonce[0] fixes only the top 7 seed bits,
  so ruling an AP *out* costs the whole 2^25 sweep (13.5s laptop) on every capture. That is the
  footgun. The other modes self-limit — Null/Static are table lookups, Ralink is one deterministic
  LFSR rewind, and RTL819x rejects 15/16 of APs on the nonce top-bit test, then bounds the probe
  to a +/-1 day seed window (0.7s laptop) and only pays its 49s E-S1 sweep once the nonce is a
  confirmed glibc match, i.e. when the PIN is about to fall out. Worth timing that 49s on a phone
  before deciding whether it needs the pref too.
