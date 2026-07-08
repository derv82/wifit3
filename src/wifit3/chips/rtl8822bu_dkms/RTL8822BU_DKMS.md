# RTL8822BU (8822bu_dkms)

A cleanroom port of the morrownr vendor stack (`rtl88x2bu` 5.13.1). RTL8822B silicon: 2T2R,
2.4 + 5 GHz 802.11ac, HALMAC + PHYDM, Jaguar-2 phystatus, firmware-based. Its own chip-local
modules — it does NOT reuse `rtw88_base` or `rtl88xxau_base`; expect a separate HAL from the
8812au/8821au/8814au siblings.

## Status

- Cold init (chip-ID/EFUSE/power/FW/MAC/BB/RF): byte-for-byte on cap-1/2/3, verified offline.
- `set_channel` per-hop prologue + TXAGC + spur eliminator: byte-for-byte, 35/35 hops.
- `enable_monitor` (the airmon monitor RX-enable): slice-verified 20/20 vs the capture.
- RX decode + jgr2 phystatus RSSI: verified vs 9292 real bulk-IN frames (median -66 dBm).
- Live monitor RX: working on hardware (ch6 -> 7 APs/383 bcn; hop 1-13 -> 25 APs/447 bcn).
- TX descriptor (`build_inject_txdesc`): byte-for-byte 251/251 vs the captured aireplay injector;
  deauth + 4-way handshake confirmed live by the user.
- Honest one-line status: **cold init byte-for-byte; runtime mostly ported.** The runtime wire
  (op 9855 -> ~28910) is partial — `set_channel`, `enable_monitor`, the PHYDM watchdog and the TX
  descriptor are ported; the continuous DIG/FA/spur adaptation the capture runs is not fully
  reproduced (the golden capture holds 24 bcn/s median while hopping; ours measures lower).
- Not ported (by evidence-backed choice): the OS managed-vif opmode block (op 9855 — LED pinmux,
  BCN_CTRL, managed RX-filter; the monitor driver uses `enable_monitor` instead), per-channel
  FW-IQK (an H2C subsystem), and thermal TX-power tracking (`phydm_rf_watchdog`; sustained-flood
  only).
- **Never re-assert "byte-for-byte" without a fresh byte-diff** — two real bugs (RX antenna mux,
  TX G_ID) hid behind exactly that unverified claim.

## Gotchas

**2.4 GHz RX deafness was the wrong antenna mux.** The wifi-only coex band-notify
(`hal8822b_wifi_only_switch_antenna`) sets the RX antenna mux `0xCBC[9:8]` per band (2.4 GHz = 2,
5 GHz = 1). It was un-ported (mis-deferred as a "BT-coex no-op"), so cold init left `0xCBC[9:8] = 0`
(neither path) and 2.4 GHz ran on the wrong antenna — a flat ~15-20 dB deficit on *every* 2.4 GHz
signal, hitting both CCK-1M and low-rate OFDM beacons. 5 GHz was fine because its path is the
cold-init default. The fix is `chan._wifi_only_switch_antenna`, called from `set_channel_bw` on a
band change after the channel set. The OFDM-beacon deficit was the tell it was a band-wide RX-path
problem, not CCK demod; IGI was a red herring (the "improvement" from forcing IGI down was just the
run-to-run instability of the wrong-antenna path).

**First-tune RX needs a band switch.** `set_channel_bw` only ran `switch_band` on a 2.4<->5
crossing, so the first tune (`prev_ch=None`) skipped it. Cold init leaves the synth in 5 GHz, so
the first 2.4 GHz tune *is* a band change — and `switch_band` wires the 2.4 GHz RX path to the
antenna (CCK enable, iFEM RFE switch). Without it the BB hears only the noise floor and every frame
fails FCS. Fix: run `switch_band` whenever `(prev_ch is None or prev_ch>14)` differs from `ch>14`.
The byte gate stayed green because the initial channel-set is a window neither `verify_pcap` (stops
at op 9855) nor `verify_channels` (iw.log hops, all `prev_ch` set) slices.

**Intermittent cold-boot 2.4 GHz synth wedge.** ~20% of cold boots leave the 2.4 GHz synth
unlocked (RF18 bit15 set after the initial tune); every 2.4 GHz hop then catches 0 frames until a
5->2.4 re-cycle. This is *not* a port miss — the bring-up wire is byte-for-byte and the vendor
reproduces the same ops; it's a HW synth-lock fault the kernel's tight transfer pacing avoids and
userland USB intermittently hits. Recovery is the chip's own 5->2.4 re-cycle, **but only after the
synth has settled** — an immediate bounce does nothing. `driver._heal_cold_synth` (after
`enable_monitor`) sleeps 0.3 s, re-cycles, re-checks, up to 4x. Validated 80/80 cold-boot soak OK
(was ~20% deaf); no-op on clean boots.

**TX G_ID must be BMC-keyed, not hardcoded.** `build_inject_txdesc` originally hardcoded G_ID=63,
which broke unicast (targeted-deauth) descriptors — 0/128 match. It's now 63 for broadcast / 0 for
unicast, giving 251/251 byte-for-byte vs the captured injector.

**Re-runnability is a clean reset every boot, not warm-skip.** `mac_pwr_switch_usb_8822b` detects
already-on and returns UNCHANGE; `rtw_halmac_poweron` then forces a card_dis_flow OFF->ON cycle.
The OFF->ON cycle has no cold capture (proven by HW double-run). Open: whether it recovers on
Windows+WinUSB.

**No software IQK runs anywhere** — the IQK engine (`0x1b00`) is in 0 of 29542 ops; IQK is
FW-offloaded (H2C). There is also **no DPK** on 8822b (no `dpk_track_8822b`).

**This card needs a strong reference AP to judge RX.** The vendor hits 9-10 bcn/s on a busy ch1;
our wrong-antenna baseline averaged ~5. Capture% (beacons caught / advertised beacon-interval) is
the RSSI-independent metric to A/B against.

**cap-2/3 caveat.** The gate is cap-1-authoritative from `config_trx_mode` (op ~9467) onward —
cap-2/3 diverge there on a stale `central_ch_8822b` module-global (the captures share a loaded
module via replug, no rmmod; cold-boot cap-1's `central_ch=0` is the correct model). Benign
cross-capture artifact, not a port bug. Everything earlier is byte-clean on all three.

**Cleanroom.** Port only from `usb_dumps_new/captures_rtl88x2bu/driver-source/` (HALMAC + PHYDM).
Do NOT open `chips/rtl8822bu/`, `chips/rtw88_base/`, or `scripts/rtl8822bu/` — reading them
produces a hybrid. The shared gate engine `scripts/rtw88_pcap_replay.py` is fine (family tooling).

## Orientation

RTL8822B is a 2T2R dual-band 11ac chip; register IO is the Realtek `bRequest=0x05` vendor control
xfer with a `0x4E0` page-switch mirror after every ON-section access (reproduced in `transport.py`).
USB IDs `2357:0138` (Archer T3U Plus), bulk-OUT 0x05 (FW/TX), bulk-IN 0x84 (RX).

Start at `bringup.cold_bringup` — the canonical sequence (shared by the driver + `verify_pcap`):
chip-ID -> EFUSE -> power/FW/MAC -> BB/AGC/RF tables -> full `odm_dm_init` (`cal.py`, including the
RF-cal tail `dc_cancellation` / `tx_current_calibration` / `get_pa_bias_offset`) -> the
`rtl8822b_init` tail (`txbf.py` MU-MIMO seed, `coex.py` wifi-only antenna/RFE, `mac.init_misc`).
Per-channel work is `chan.set_channel_bw` (switch_channel + bandwidth + band-switch + PSD spur
eliminator + per-channel TXAGC in `txpower.py`). Monitor RX-enable is `mac.enable_monitor`. RX
decode + RSSI is `rx.py` (jgr2 phystatus). TX is `build_inject_txdesc`. SIPI RF primitives: RF read
= direct BB read at `{0x2800,0x2c00}[path]+(addr<<2)`; RF write packs into `0xC90`/`0xE90`.

Names match the vendor C — grep `driver-source/` to cross-reference.

## Scripts

- `verify_pcap.py rtl8822bu_dkms [<cap>]` — the cold-init byte gate (op 0->9855).
- `verify_channels.py <cap>` — per-hop `set_channel_bw` byte-diff (35/35/34 hops).
- `verify_initial_tune.py` — gates the initial 2.4 GHz channel-set + antenna notify (the seam the
  cold gate misses).
- `verify_strict_audit.py` — strict per-phase audit; 0 wrong-writes across all 35 hops.
- `test_hw.py --phase open|init|beacon` — live HW smoke; `--rxstats CH` tallies rx_pkt_desc
  categories + RSSI + RF18 without the good-frame filter.
- `cck_diag.py --channel N --dwell S` — live rate-split per-AP capture% vs beacon-interval
  (`--watchdog`, `--igisweep`, `--bssid`, `--set`, `--cckpd`, `--scan`).
- `cck_capref.py` — the vendor's own bulk-IN from the capture's 15 s FIXED-CH1 window (the A/B ref).
- `soak_2g.py` — automated cold-boot synth-wedge repro (each connect() is a cold cycle, no replug).
- `poll_probe.py` / `synth_lock_probe.py` — HW poll-loop convergence / synth-recovery-strategy bench.

## Debug log

### 2026-06-16 — 2.4 GHz RX deafness root cause (wrong antenna mux)

The cold-boot pcap reproduced byte-for-byte, but on hardware 2.4 GHz RX was deaf ("data flies at
1400/s, beacons die at 0-1/s") while 5 GHz worked. `cck_diag.py` + `cck_capref.py` (same APs, same
environment, ch1) showed the vendor catching 73-89% of beacons where we caught 1-55% — and crucially
the deficit hit OFDM-6M beacons (3%/1% vs the vendor's 79%/73%) as hard as CCK-1M, ruling out a
CCK-demod/filter bug. Ruled out by HW measurement: CRC/demod corruption (0 crc_err/0 icv_err every
rate), the CCK-PD threshold `0xA0A`, and airtime contention (quiet ch3 still ~55% on the strongest
AP). The cause was `0xCBC[9:8]=0` — the un-ported wifi-only antenna mux. Forcing `0xCBC[9:8]=2`
lifted NETGEAR2G 0% -> 59%, RSSI +20-40 dB (-53, near the vendor's -48), APs heard 9 -> 23. Fixed +
committed as `chan._wifi_only_switch_antenna`. Secondary open item: with the right antenna the
strongest APs now arrive ~-41 dBm and saturate at the frozen `dig_init` IGI — the DIG watchdog must
run to back gain off to land NETGEAR2G at the 8-10 bcn/s target.

### 2026-06-16 — severe Pcap Replay audit (G1-G19) — antenna mux was the sole RX bug

A source diff against `driver-source/` enumerated every place the port might diverge from the vendor
wire (the antenna mux taught us "deferred/dead-code" labels lie). All gaps resolved: every other
candidate is proven no-op / inert-unlinked / telemetry / TX-side / superseded-by-`enable_monitor` /
HW-verified-converges for monitor RX. The watchdog "real gaps" (G1 MRC weighting `0x98c`, G11
htstf-mumimo `0x8d8[17]`, G13 DIG damping) are no-ops in monitor: their register values already
match the vendor's monitor values, `rssi_min` stays low unlinked, and DIG-damping is `!is_linked`-
gated. `verify_strict_audit` confirmed initial-tune + all 35 hops replay byte-for-byte with zero
wrong-writes — no second antenna-mux-class bug. The DARK hop tails decode entirely to the 2 s PHYDM
watchdog cycle (the recurring `0xfa4/0xfb4/0x280/...` tail is the watchdog, **not** DPK — 8822b has
none). `dc_cancellation` live-poll integrity HW-verified (`poll_probe.py`): idle reached both paths,
DC comp applied from the live `0xFA0` read.

### 2026-06-16 — TX descriptor: G_ID hardcode broke unicast

The old "byte-for-byte, only seqctl varies" TX claim was never actually byte-checked, and was wrong:
G_ID was hardcoded 63, which broke unicast/targeted-deauth descriptors (0/128 match). BMC-keying it
(63 bcast / 0 unicast) gives 251/251 byte-for-byte vs the captured aireplay injector (33 bcast + 218
unicast). Deauth + 4-way handshake then confirmed live by the user (client dropped + reconnected,
M1-M4 captured, crackable M2 reachable; TX and concurrent RX both work). The only standing TX gap is
thermal power-tracking (`phydm_rf_watchdog`), which matters only for sustained injection.

### 2026-06-16 — intermittent cold-boot 2.4 GHz synth wedge

~20% of cold boots delivered 0 frames on every 2.4 GHz hop (5 GHz always fine). The "slow hop /
tune starves the RX window" theory was refuted by measurement (`hop_timing.py`: cold init 0.54 s,
per-hop tune 7-58 ms, 2.4 GHz delivers 88-684 beacons while hopping). HW measurement pinned it to
RF18 bit15 set after the initial tune (synth unlocked, pinned at 5 GHz); no RF18 write un-sticks it.
The chip's own 5->2.4 re-cycle recovers it, but only after a settle — `_heal_cold_synth` heal fired
3x back-to-back with no settle and stayed deaf; a bounce after a 0.3 s settle re-locks every time.
`soak_2g.py` validated 80/80. Not a port miss (bring-up wire is byte-for-byte); a HW synth fault
userland USB pacing hits.

### 2026-07-08 — HW sweep: DIG-saturation refuted, cold-synth heal holds 0/60, capture% at parity

Three BUGS.md ⏳ items swept on hardware (busy urban 2.4 GHz; strongest AP in range −53 dBm on ch1).

**Strong-AP DIG saturation — refuted at achievable signal levels.** The strongest AP in range
(CCK-1M, −53 dBm, ch1) captures at 78% (7.7/9.8 bcn/s) with the frozen
`dig_init` IGI seed (0x20). An IGI sweep 0x10→0x40 (`cck_diag --igisweep`, one tune/environment) is
**flat** — 5.8–8.7 bcn/s, no monotonic trend, zero dead seconds — so backing gain off does not help
a strong AP here; there is no saturation signature. The PHYDM watchdog (`--watchdog`) converges IGI
*down* to 0x1d (more gain), not off, and leaves capture unchanged (77% vs 78%). This is faithful: the
unlinked/monitor DIG (the only branch wifit3 reaches — never associated) clamps IGI to [0x1c, 0x22]
and steps by FA count toward the floor; the RSSI-based "back gain off" boundaries live in the
linked-STA branch that never runs in monitor, and the vendor is identical. The 78%-vs-ideal gap is
airtime contention on a busy ch1 (435 OFDM + 241 CCK in the 1–13 scan; OFDM bursts to 185–321/s in
the per-second correlation), not gain. The exact ~−41 dBm near-AP case is untested (no AP that strong
in range), but the proposed fix mechanism is proven ineffective and the DIG is already
vendor-faithful. Cleared from BUGS.md; supersedes the 2026-06-16 "DIG must back gain off" expectation.

**Matched-load capture% — within ~6 pts of the vendor reference.** Live 78% on the contended ch1 vs
the vendor's own recorded ch1 window (`cck_capref.py`: top CCK AP ~8–10 bcn/s ≈ 84%) — but that
reference is *also* a busy channel (130–456 OFDM/s), so 78-vs-84 is a comparable-load comparison and
the gap reads as environment. A pristine matched-load number still wants a genuinely quiet ch1, which
this RF environment doesn't offer; the residual stays in BUGS.md, downgraded.

**Cold-boot 2.4 GHz synth wedge — heal holds 0/60.** `soak_2g.py --runs 60` (each `connect()` a full
cold OFF→ON cycle): 0/60 boots 2.4 GHz-deaf, every run 58–107 ch1 beacons. `_heal_cold_synth` (the
settled 5→2.4 re-cycle) holds across a 60-cycle soak (was ~20% deaf pre-fix). Cleared from BUGS.md.
