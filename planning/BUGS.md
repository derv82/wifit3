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

## WPA Downgrade reads as "dead" — it's a slow, niche wait-attack

The Focus **WPA Downgrade** button looks broken because nothing happens fast — but
it isn't. It's a probe-response spoof (forge the AP's BSSID/SSID/channel with a
WPA2-only RSN IE) that **waits** for a client to naturally reconnect and take the
WPA2 ad: it can't deauth-trigger (PMF blocks that on WPA3 clients), works only on
WPA3-*transition* APs, and pays off in minutes-to-hours. Two fixes: (1) set
expectations — disable/annotate the button unless the target is WPA3-transition,
and log "passive — waiting for a natural reconnect (minutes-to-hours)" on start;
(2) verify it actually injects on hardware (only the docstring's *intent* is
confirmed, never a live capture). `engine/attacks/wpa3_downgrade.py`.

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

---

> Not here: **driver wedge / replug warnings not reaching the UI** is a **release
> blocker** (hardware-failure UX), tracked in `RELEASE-PLAN.md` § 2c — it gates
> the alpha, so it doesn't sit in this backlog.
