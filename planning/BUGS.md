# Wifit3 — Known Bugs & QoL

Forward-looking. Defects and quality-of-life nits in *existing* behavior — things
to **fix**. New capabilities to **build** live in `FEATURES.md`; release-gating
work in `RELEASE-PLAN.md`; current per-card state in `../VERIFICATION.md`;
tech-debt / de-vibe (ugly-but-working code) lives in `RELEASE-PLAN.md` § Phase 5.

Tracked in-repo on purpose — offline, greppable, versioned alongside the code
that has the bug. When the repo goes public, GitHub Issues becomes the inbox for
community-filed reports; this stays the curated list.

Ordering is rough priority.

---

## Focus-entry channel tune sometimes doesn't take (0 beacons until re-enter)

Entering Focus on an AP occasionally shows 0 beacons/s; exiting to Scanner and
re-entering Focus on the same target then works (8–9/s). Confirmed cross-family —
RT3572 (Ralink) and MT7610U (MediaTek) — so the bug is in the **shared
Focus→stop-hop→`set_channel` path** (`wlan/interface.py` / `ui/screens/focus.py`),
not a driver. Likely a race/ordering issue: the channel set on Focus entry is
lost or overridden by the channel-hopper teardown, so the first tune doesn't
stick. Repro: Focus a known AP, watch for 0 beacons, then Focus→Scanner→Focus.

## Beacon count truncates past 10k

`10512` renders as `0512`. Auto-size the BEACONS column without breaking
right-alignment.

## WPS PBC auto-invade can monopolize the radio on timeout (Focus)

PBC auto-invade is ON by default and works well, but in Focus a PBC attempt that
times out keeps retrying for the rest of the AP's PBC window, and other attacks
are blocked for that span. Give it manual control — a **Stop PBC** button (and a
**Start PBC** when a window is open) — and/or bound the retry loop so a single
timeout can't hold the radio. Minor; deferred.

## WPA3 downgrade is weak today — two paths to a real one

The Focus **WPA Downgrade** button reads as dead because the current approach
genuinely is weak. Both viable approaches end at the same prize — the client's
**EAPOL M1 + M2** for a *WPA2* association (M2's MIC is all an offline PSK crack
needs; M3/M4 are gravy, and you can't forge M3 without the PSK anyway) — and both
work **only on WPA3-*transition*** APs (a pure-SAE client refuses WPA2). If the AP
sets **Transition-Disable**, both die.

**Path 1 — passive downgrade (what's implemented).** Forge WPA2-only beacons /
probe responses for the target's BSSID and let a client downgrade and run its WPA2
4-way *with the real transition AP*, sniffing it passively. Cheap — no AP to run —
but at the client's mercy: the real AP is advertising SAE on the same channel the
whole time, so a sane client just picks SAE and there's nothing to capture. (Also
never confirmed to actually inject on hardware — only docstring intent.)
`engine/attacks/wpa3_downgrade.py`.

**Path 2 — evil twin / rogue AP (the reliable build).** Isolate the client onto a
rogue AP — same SSID (+ BSSID, to impersonate), ideally a *different* channel so it
isn't fighting the real SAE beacon — advertising WPA2-only; accept the client's
auth + assoc, **send EAPOL M1 yourself** (a random ANonce, no secret), capture M2.
Deterministic: WPA2 is the only option offered. Can't finish (no PSK for M3),
doesn't need to. The RSNE-confirmation check at M3 may make the client abort the
connection — fine, M1+M2 are already in hand. In wifit3 this is a **minimal AP
responder in the inject path** (beacon + probe-resp + auth + assoc + M1 → catch
M2), *not* a shell-out to hostapd like Wifite2 (hostapd is Linux-only — it would
kill the Windows/cross-platform model). Feature-scale, not a tweak.

**Near-term QoL** on the current button regardless: disable/annotate it unless the
target is WPA3-transition, and log "passive — waiting for a natural reconnect
(minutes–hours)" on start so it stops looking broken.

## Bulk-IN read timeout treated as fatal on Windows (fleet audit)

A benign bulk-IN read timeout (no traffic this interval — every quiet channel
yields one) must return `None` so the shared `RxReaderThread` keeps going. Several
userland drivers' `transport.bulk_in` only map a **libusb** timeout to `None`
(`errno == 110` or the substring `"timeout"`), but the **Windows/WinUSB** backend
raises `[Errno 10060] Operation timed out` — errno `10060`, and `"timed out"` does
**not** contain `"timeout"` — so the timeout is re-raised and counted as a hard
error. After `max_errors` (5) consecutive, the reader **gives up and RX dies**. It
hides on busy 2.4 GHz (a successful read resets the counter before 5-in-a-row) and
bites on 5 GHz, whose many empty DFS channels produce long timeout runs. Fixed in
`rtl8814au_dkms` (commit on `dkms/8814au`: catch pyusb's `USBTimeoutError` type +
errno 110/10060 fallback). **Audit the other userland drivers'
`chips/<chip>/transport.py` `bulk_in`** for the same `errno==110`/`"timeout"`
pattern and apply the same fix (Windows users + quiet channels hit it on any of
them). Greppable: `errno.*110|"timeout" in`.

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

## Ralink bring-up divergences from the vendor wire (pcap replay-diff findings)

Surfaced by the per-driver pcap byte-diff verifier (`scripts/verify_pcap.py <chip>`, which
replays a port's bring-up against the vendor cold-boot capture). Both are faithfulness gaps
— the port emits a USB sequence the in-tree driver does not — found while bringing the
Ralink family onto the verifier. Neither is known to break RX/TX, but each is an un-audited
deviation worth closing.

* **rt2800usb: `load_firmware` writes `AUTOWAKEUP_CFG` on USB; the kernel doesn't.** Our
  `chips/rt2800usb/firmware.py` `load_firmware` opens with `write32(AUTOWAKEUP_CFG, 0)`, but
  the RT5592 USB cold-boot capture never issues it. In `rt2800lib.c` that write sits inside
  an `is_pci || is_soc` guard — PCI/SoC only — so the USB path should skip it. The 4096-byte
  rt2870.bin upload itself is byte-perfect (verified); only this preamble write diverges.
  Fix: gate the `AUTOWAKEUP_CFG` write out of the USB loader. Greppable: `AUTOWAKEUP_CFG`.

* **rt2500usb: `config_ant` ordering — kernel touches `MAC_CSR20` first (un-audited gap).**
  The verifier reproduces `init_registers` + `init_bbp` byte-for-byte (123 ops), then
  diverges: our `connect()` runs `config_ant` next (first op = read `PHY_CSR8` 0x04d0), but
  the capture's next op is `MAC_CSR20` (0x0428) — the kernel does a MAC-side step between
  baseband and antenna config that our port skips or reorders. Could be a missing register
  write (partial port) or a benign ordering difference; needs the `MAC_CSR20` step decoded
  against `rt2500usb.c`. Until then `config_ant`/`config_channel` aren't pcap-gated (they
  need their own anchored blocks in `scripts/rt2500usb/verify_pcap.py`).

---

> Not here: **driver wedge / replug warnings not reaching the UI** is a **release
> blocker** (hardware-failure UX), tracked in `RELEASE-PLAN.md` § 2c — it gates
> the alpha, so it doesn't sit in this backlog.
