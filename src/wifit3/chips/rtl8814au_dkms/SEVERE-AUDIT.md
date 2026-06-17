# RTL8814AU_DKMS — Severe Audit (IN PROGRESS — next agent, start here)

> ## NEXT AGENT — START HERE
>
> **Goal: a faithful port of `rtl8814au_dkms`** (the morrownr 8814au vendor stack), to the point
> where a single-cursor `verify_pcap` reproduces the **entire** cold-boot capture byte-for-byte
> (every op matched, the only waiver being aireplay-ng's TX).
>
> **Cleanroom.** Port ONLY from `usb_dumps_new/captures_rtl8814au/driver-source/`. Do **NOT** open
> `chips/rtw88_8814au/` (mainline) or `chips/rtw88_base/` — reading them produces a hybrid. The
> shared replay engine `scripts/rtw88_pcap_replay.py` is fine (family tooling, not driver code).
>
> **You are autonomous — do not block on the user.** The rules are written down:
> `planning/PORTING.md` → **"The verify is one monotonic walk"** + **"The full-capture replay — the
> operating rules."** Read those first; they are the method. The card (ALFA AWUS1900, `0bda:8813`)
> is plugged in + WinUSB-bound — **run on hardware freely** (RX, channel tune, scan, 30-min soak).
> The **only** thing you may not do is **live 802.11 TX / injection / deauth** — that is the user's
> hands. Everything else: go. Whatever gets us to faithful.
>
> ### Mindset (load-bearing)
> **Assume the port is NOT faithful — lazy, many ops skipped.** This session proved it (below).
> **Do not trust the port's comments** — *especially* any `skipped because X` / `no-op here` /
> `monitor dead-code` / `validated live` rationale, even when it sounds airtight. Treat every such
> comment as a **hypothesis to FALSIFY** against the vendor source + the wire. We have been burned
> exactly this way: `dig.py:17` says the watchdog is *"validated live, not via the byte-for-byte
> differ"* — and behind that comment was a **4-member gap**. The 8822bu's deaf 2.4 GHz hid behind a
> *"BT-coex no-op"* comment. Default-assume-wrong until silicon or source proves it.
>
> ### The plan
> 0. **This doc is the gap ledger.** Log every gap + verdict as you go (table at the bottom).
> 1. **Build a NEW single-cursor `verify_pcap`** to the full-capture methodology. The current
>    `scripts/rtl8814au_dkms/verify_pcap.py` is the **anti-pattern** — replace it (details below).
>    Reference shape (good structure, single-cursor + operational dispatch):
>    `scripts/rtl8188eus_dkms/verify_pcap.py`. *(It still waives the airmon dance, which the new
>    rule reverses — so it's the template, not a complete example.)*
> 2. **Walk the vendor source using the gate's frontiers as landmarks.** Each unaccounted op the
>    cursor stops on names the next thing to make faithful:
>    - **gap** (an op no handler reproduces) → **port it.**
>    - **inconsistency** (we emit a different byte, or skip a branch) → **make it consistent with the wire.**
> 3. **Only once the cursor PASSes across the WHOLE capture** do you know the *full* gap list. Fix
>    the set as one change, **then** HW-test once (scan + a 30-min dual-band soak — the dropout is
>    the **2.4 GHz** one). **Do NOT port one gap and soak-test** — that loop wastes hours and can't
>    surface the gap you can't see (PORTING.md names it).

## What this session found (don't re-derive — start from here)

### The current gate is the windowed anti-pattern
`scripts/rtl8814au_dkms/verify_pcap.py`:
- **Windowed** to frames `(5707, 30000)` — but **the capture runs to frame 85279.** ~55k frames
  (the whole runtime-watchdog phase) are outside the window entirely.
- **Stops at M3b-1**: reproduces 4451 ops (cold init → hal_init turn-on tail), then halts; ~7105
  ops *within its own window* (61%) are never replayed. No waivers are *named* — it just stops.
- **Slices the monitor entry** into an isolated 10-op block (`verify_monitor_block`), out of context.
- **Waives the airmon STA→monitor dance** (silently) — the new rule says reproduce it.
- **Never gates the watchdog.** `verify_channels.py` covers per-hop tunes only; the periodic
  watchdog ticks are byte-verified by nothing.

### CONFIRMED un-ported chunk: the runtime watchdog (prime 2.4 GHz dropout suspect)
`dig.py:watchdog_tick` runs **only** `phydm_dig` (FA→IGI). The vendor `phydm_watchdog`
[SRC phydm.c:1828-2226] runs every tick, additionally:

| Vendor member | Reg(s) | Our state |
|---|---|---|
| `phydm_dig` (FA→IGI) | 0xc50/e50/1850/1a50 | ✅ ported |
| `phydm_cck_pd_th` | 0xa0a | ❌ **seed-only** at init (`dm._cck_pd_init`), never updated — **2.4-GHz-specific** |
| `phydm_adaptivity` | 0x8a4 | ❌ seed-only (`dm._adaptivity_init`) |
| `phydm_env_mntr_watchdog` | 0x994 | ❌ seed-only (`dm._env_monitor_init`) |
| dynamic antenna-weighting | 0x98c/0x198c | ❌ **not ported at all** (we disable MRC ant-sel at init, `dm.py:46`) — **4T4R RX, the runtime analog of the 8822bu antenna-mux bug** |

The capture proves the vendor re-runs these at runtime: **347× `0x994`, 108× `0x8a4`, 216×
`0x198c`** in the operational tail (frames 15247-85279), none reproduced by us. **CCK-PD** (CCK
doesn't exist on 5 GHz — which is exactly why only 2.4 GHz drops) and **antenna-weighting** (4T4R)
are the two prime dropout suspects. **But this is the gap we KNOW** — build the full gate to find
the ones we don't, then fix the whole set together.

## Coordinates
- Card: ALFA AWUS1900, RTL8814AU **4T4R**, `0bda:8813`. Efuse (cap-1): `rfe_type=1`, `crystal_cap=0x23`.
- Captures: `usb_dumps_new/captures_rtl8814au/capture-{1,2,3}.pcap` — dev addr `{51, 53, 54}`. The
  airmon/FW-load phase starts ~frame 5707; capture-1 runs to **frame 85279**. `<cap>_logs/iw.log`
  has the per-channel `set channel` windows (use these to slice the hops; see PORTING.md airodump rule).
- Vendor source: `usb_dumps_new/captures_rtl8814au/driver-source/` (morrownr 8814au 5.8.5.1).
- Port: `src/wifit3/chips/rtl8814au_dkms/` — handlers: `firmware.bring_up`, `mac.phy_mac_config`/
  `mac_init_misc`/`hal_init_turn_on`, `bb.phy_bb_config`, `rf.phy_rf_config`, `chan.init_tune`/
  `set_channel_bw`/`set_rfe_reg_init`, `dm.init_hal_dm`, `dig.watchdog_tick`, `monitor.enter_monitor`,
  `efuse.read_chip_params`, `rx.iter_frames`.
- Gates: `scripts/rtl8814au_dkms/verify_pcap.py` (windowed anti-pattern — REPLACE), `verify_channels.py`
  (per-hop, sliced — FOLD into the single cursor), `verify_efuse_pcap.py` (probe efuse read).
- HW smoke: `scripts/rtl8814au_dkms/{test_hw,scan_hw,ab_scan}.py`.

## Known gaps ledger (fill as you walk; verdict = ported / fixed / proven-faithful / waived-named)

| # | Op / area (wire landmark) | Vendor [SRC] | Our state | Verdict |
|---|---|---|---|---|
| G1 | watchdog `phydm_cck_pd_th` (0xa0a) every tick | phydm.c:1828 | seed-only at init | **gap — port into watchdog_tick** |
| G2 | watchdog `phydm_adaptivity` (0x8a4) every tick | phydm.c:1830 | seed-only | **gap — port** |
| G3 | watchdog `phydm_env_mntr_watchdog` (0x994) | phydm.c:2226 | seed-only | **gap — port** |
| G4 | dynamic antenna-weighting (0x98c/0x198c) | phydm_rtl8814a | not ported | **gap — port (4T4R RX)** |
| G5 | airmon STA→monitor dance | (driver's writes) | waived silently | **reproduce, don't waive** |
| G6 | monitor entry verified out-of-context (10-op slice) | — | sliced | **dispatch inline in the single cursor** |
| G7 | frames 30000-85279 outside the gate window | — | never replayed | **extend the cursor to capture end** |
| … | (the gate's next frontier) | — | — | (walk it) |
