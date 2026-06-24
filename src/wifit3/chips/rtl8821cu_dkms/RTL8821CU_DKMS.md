# RTL8821CU (8821cu_dkms)

A self-contained port of the Realtek vendor/DKMS driver (`rtl8821cu-5.12.0.4`, in the capture
bundle). RTL8821C silicon: 1T1R, 2.4 + 5 GHz 802.11ac, HALMAC + PHYDM, Jaguar-2 phystatus,
firmware-based. Its power tables and init are specific enough that it doesn't share a base with
other drivers.

## Status

- Cold init and firmware boot: working on hardware.
- **RX coin toss — CAUSE IDENTIFIED: `dm._dc_cancellation` (see LIVE-LEAD/ROOT-CAUSE bullet below);
  fix pending.** Skipping the cal makes BOTH bands reliable. Dead = chip-side RX: RX FIFO never fills
  (`RXFF_PTR=0`) though the demod runs (FA flood) = front-end uncalibrated = analog. Note two
  things were conflated historically: (1) **2.4 GHz also has the RF18-bit16 gate** — on a cold tune
  the bit stays SET so ch1 needs a 5→2.4 GHz band switch to RX; but the bigger 2.4 GHz killer is
  dc_cancellation (6/9 dead with it, 0/9 without). (2) the per-boot variability on BOTH bands is
  dc_cancellation, not RF/MAC config (which reads byte-identical good-vs-dead).
- **Eliminated this round (2026-06-24 cont'd 3), with hardware data — do NOT re-chase these:**
  - **DC-offset cancellation OUTPUT** (`0xc10`/`0xc14`): benign — byte-identical between a 346-frame
    GOOD and a 0-frame DEAD launch, and disabling it (`0xa9c[20]=0`) does not help. BUT *running* the
    cal IS the lead — see LIVE LEAD below. (I first marked DC fully "ruled out" on the output alone;
    that was premature — corrected.)
  - **IQK**: NOT missing. `bNeedIQK` is zero-init and set TRUE only on link/AP/TDLS/sreset/MCC events
    (traced every set-site); cold boot / monitor entry / PHY init never set it, so the vendor runs no
    IQK in monitor mode either. The vendor also posts RX URBs only AFTER `rtw_hal_init`
    (`rtw_intf_start` → `rtl8821cu_inirp_init`), so no concurrent-RX-during-init in the kernel.
  - **Missing settle delays / write pacing**: NOT the story. The kernel capture has ~no explicit
    inter-op waits — 24 gaps ≥0.3 ms in all of init+airmon, all explained (6.7 s idle before airmon,
    RF-LSSI `0xc90` pacing, the `stop_ic_trx` 1 ms idle-poll). Our synchronous libusb transfers are
    if anything SLOWER than the kernel's ~40 µs op cadence (`airmon_rx_onset.py`).
  - **vs-capture byte divergences are BENIGN card-identity.** `cold_divergence.py` (real silicon vs
    the capture, op-by-op) finds ~36 *consistent* divergences present on GOOD launches: GPIO
    (`0x40/0x4c/0x4e/0x64`), RFE/antenna mux (`0x1080`), coex GNT (`0x73`), chip-version bits, + the
    live DC cal. The capture is from a slightly different board/antenna config; our per-card RMWs
    correctly differ there. The byte-gate hides this (it feeds captured reads) but it does NOT cause
    RX death. So "the bytes differ on silicon" is true but benign — not the bug.
- Where RX first arrives in the capture: airmon stage, frame 17261 (`pcap_slicer.py` window
  7673–17766; `airmon_rx_onset.py`), right after a normal channel/bw tune we DO reproduce.
- **Front-end instrumented on DEAD launches (`dead_frontend.py`, 10 DEAD / 6 GOOD on 5 GHz):** the
  RF synth/PLL word reads byte-IDENTICAL good-vs-dead (`RF18=0x13d24`, `RFca=0x80000`,
  `RFb0=0xff0f8`, `RFb8=0x80a00`) and so does the MAC RX path (`r808`/`r838`/`CR=0x6ff`/
  `RCR=0x90000001`). On DEAD launches the OFDM false-alarm counter FLOODS (`ofdmFA` up to `0x7057`
  vs ~`0x100` on GOOD) and DIG reactively backs IGI off to `0x22` — the demod is triggering on noise
  and never locking a real packet, with `RXFF_PTR=0`. So with identical RF + MAC config the RX gain
  is physically wrong ~60% of boots = an analog RX-gain/calibration whose result lands in NO readable
  register. No C2H (interrupt-IN ep 0x81) arrives before RX onset.
- **ROOT CAUSE (highest confidence reached) — running `dm._dc_cancellation` intermittently breaks RX
  on BOTH bands; skipping it fixes both (`dc_ab.py`, order-controlled, interleaved, same card).**
  Cal run → 5 GHz median 33 (2/9 dead), 2.4 GHz mostly DEAD (6/9 dead, median 0); cal SKIPPED →
  5 GHz median 121 (0/9 dead) AND 2.4 GHz median 133 (0/9 dead). The vendor runs the same cal with
  steady RX, so our byte-faithful port leaves the analog front-end wrong on silicon — gate-invisible,
  since the gate replays captured reads. It is an analog RESTORE, not the DC offset (disabling the
  comp `0xa9c[20]=0` does NOT recover RX). `dc_restore.py` localized what the cal leaves
  non-operational: **RF `0x3f` (LNA-path gain) = `0x281d` vs the operational `0x1f9d`** (3-wire
  `0xc00/0xe00`, ck320 `0x8b4`, IGI `0xc50` all restore fine; `0xa78`=0 and `0xa9c[20]`=1 differ but
  are vendor-faithful comp state, exonerated). `_lna_setting` writes the LNA gain through RF banks
  (`0xEF[19]`/`0xEE[12]`); on silicon RF `0x3f` ends at the cal value, not the operational gain —
  wrong LNA gain → demod floods on noise / can't lock → the dead symptom. FIX (Lead's call, byte-gate
  tension): (a) restore the operational LNA gain after dc_cancellation (re-apply RF `0x3f`=`0x1f9d` /
  re-run config_radioa's LNA path); (b) audit `_lna_setting` + `rf.write_rf`/`write_rf_masked`
  RF-bank handling vs the vendor — the LNA write likely leaks into the main bank on hardware (a
  gate-invisible bank bug); or (c) gate the cal off (simplest, needs a verify_pcap exception).
  Confidence the cal is the cause: HIGH (multi-batch, both bands, order-controlled). That RF `0x3f`
  is the exact mechanism: MEDIUM — localized, but the confirming run was all-GOOD so not yet
  correlated with a dead launch.
- `verify_pcap`: clean — but BLIND to (a) timing and (b) read-modify-write correctness, since it
  replays CAPTURED read values (a wrong RMW only diverges when the REAL chip reads differently). Both
  blind spots were checked this round; neither is the cause. See Gotchas.
- The card is NOT permanently wedged by soft re-inits — it recovers on the next launch with no
  replug (user-confirmed; an earlier "wedged, must replug" claim was wrong). Rapid back-to-back
  re-inits (<1 s rest) do raise the dead rate transiently; space launches ≥1.5 s.
- Not done: root-cause of the 5 GHz coin toss, ZeroCD discovery, warm reattach.

## Gotchas

**The card hides as a CD-ROM.** It enumerates as USB mass-storage ("ZeroCD") and must be
mode-switched to the Wi-Fi PID `0bda:c820` before any driver can bind. A user who plugs it in today
sees a CD-ROM and Wifit3 finds nothing, so the card is unusable end-to-end until the discovery
layer handles the switch. This is a manager-level problem that affects most Realtek USB adapters,
not just this one. The offline port and verify are unaffected — the pcap was captured already in
Wi-Fi mode.

**2.4 GHz RX hangs on one bit: RF18 bit16.** Set, every frame fails CRC; clear, the demod works.
The cold channel tune doesn't clear it, and an ordinary same-band hop can't either — the vendor's
channel-switch path never touches bit16. Only a band switch or an explicit warm rewrite clears it,
which is what `driver._relatch_2g_band` does after cold init. The vendor stack gets away with it
because airodump jumps to 5 GHz immediately, and the first 5→2.4 GHz transition clears the bit as a
side effect.

**This card's 2.4 GHz is genuinely weak**, even under the vendor driver — the kernel's own
fixed-channel capture shows only ~13–21 beacons over 15 s. Judge RX against a strong nearby
reference AP, not against this capture.

**`verify_pcap` cannot see timing.** A power-sequence (or PHY-table) `DELAY` emits no register op,
so it is invisible in the capture — a byte-faithful port, and the gate, will happily drop required
settle delays and still PASS. The bring-up coin toss was exactly this. When you hit a coin toss with
*identical* register state between good and dead launches (`bringup_cointoss.py` proves it: same
CR/RCR/RXDMA/filter, different RX outcome), the culprit is a skipped delay/poll, not a missed write.

## Orientation

Start at `bringup.cold_bringup` — it runs init → power sequence → firmware → MAC → BB → RF in the
kernel's order. Channel tuning is `chan.set_channel` (it only switches bands when the band actually
changes). RSSI is in `rx.decode_rssi`, which parses the jgr2 phystatus format — it's a Jaguar-2
chip, and decoding it as Jaguar-1 was an early mistake.

Names match the vendor C, so grep the bundle's `driver-source/` to cross-reference.

## Scripts

- `verify_pcap.py` — the cold-boot byte gate.
- `band_state_probe.py` — HW RX diagnostic; this is what isolated the RF18 bit16 gate.
- `driver_rx_diag.py` — re-run after a fresh plug to confirm 2.4 GHz comes back.

## Debug log

### 2026-06-24 (cont'd 5) — both bands fixed by skipping the cal; LNA gain (RF 0x3f) localized

`dc_ab.py` extended to measure both bands (5 GHz then ch1 after a band switch): skipping
`_dc_cancellation` fixes BOTH — 2.4 GHz goes 6/9 dead (median 0) → 0/9 dead (median 133), 5 GHz
median 33 → 121. So the cal is the dominant RX killer, not just a 5 GHz thing, and NOT the 2.4 GHz
cal's "purpose" (skipping helps 2.4 GHz, doesn't hurt it). `dc_restore.py` dumped the regs the cal
disturbs-and-should-restore, normal vs skip: only **RF 0x3f (LNA gain) = 0x281d vs operational
0x1f9d** stands out (plus the vendor-faithful comp state 0xa78=0 / 0xa9c[20]=1). `_lna_setting`'s
banked RF writes (0xEF[19]/0xEE[12]) leave the LNA gain at the cal value on silicon. Fix options in
the Status ROOT-CAUSE bullet (restore LNA gain / audit RF-bank handling / gate the cal off). Did NOT
commit a fix — byte-gate tension + it's a design call. Confidence: cal=cause HIGH; RF-0x3f=mechanism
MEDIUM (localizing run was all-GOOD, not yet correlated with a dead launch).

### 2026-06-24 (cont'd 4) — METRIC MOVED: running dm._dc_cancellation destabilizes 5 GHz RX

The first lead in 7 sessions that moved the metric. `dead_frontend.py` proved the 5 GHz dead state
is analog (RF synth/PLL `RF18/ca/b0/b8` + MAC RX path byte-identical good-vs-dead; dead = OFDM-FA
flood to `0x7057` + `RXFF_PTR=0`). `reader_ab.py` ruled out reader-start ordering (during 1/6 vs
quiet 2/6 dead — and "quiet" delivers fine, so the "start reader before RX-enable or the pipe
wedges" note is also unreliable). Then `dc_ab.py` (order-controlled A/B, real driver): with
`_dc_cancellation` SKIPPED, 5 GHz is consistently good (0/10 dead, tight 82–173 beacons); run
normally, 5 GHz is erratic (low tail 0–40, 1 dead) — same card, interleaved. Disabling only the comp
output (`0xa9c[20]=0`) does NOT recover it, so it's the cal's analog disturbance (LNA off/on, 3-wire
+ ck320 stop/restart, IGI→0x7e), not the written 0xc10/0xc14. Corrected my own premature "DC ruled
out (cont'd 3)" — that was output-only; running the cal is implicated. NEXT: test 2.4 GHz/CCK impact
of skipping (don't break its real purpose), then a band-aware / robustified fix (NOT a delete —
breaks the byte-gate + vendor-faithfulness).

### 2026-06-24 (cont'd 3) — eliminations: DC, IQK, timing all OUT; coin toss is 5 GHz + analog

Re-grounded the whole hunt against the pcap + live silicon (card recovers without replug — the
"wedged" claim was wrong). New tools: `airmon_rx_onset.py` (work backwards from the first RX frame +
inter-op gap scan), `cold_divergence.py` (drive the REAL driver on silicon, compare every read/write
to the capture op-by-op with tolerant resync, label GOOD/DEAD, diff), `bringup_hop.py` (continuous
dual-band hop like the app, with DC telemetry), `rf18_latch.py`.

Killed three leads with data: **DC cancellation** — `0xc10`/`0xc14` byte-identical on a 346-frame
GOOD and a 0-frame DEAD launch (the staged "diff the DC values" probe finally ran; they don't track
RX). The `stop_ic_trx` delay+fail-abort port (vendor `ODM_delay_ms(1)`/`PHYDM_SET_FAIL`) is real but
INERT here — `stop_fail=0` every launch, BB always idles — so it was reverted, not committed.
**IQK** — `bNeedIQK` zero-init, set only on link/AP/TDLS/sreset/MCC; never in monitor; vendor runs
no IQK either. (The "FW-offloaded IQK" doc is the 8822bu's, a different chip — it kept corrupting the
IQK question.) **Timing** — kernel init has ~no inter-op waits (24 gaps ≥0.3 ms, all explained); we
fire no faster than it. **vs-capture divergences** — ~36, consistent, all on GOOD launches =
card-identity (GPIO/RFE/coex/version + live DC); benign, not the bug.

Net: the 5 GHz coin toss is NOT in the digital read/write trace (good-vs-dead identical bar
`RXFF_PTR`/IGI) and NOT timing — it's an analog front-end bring-up that varies per boot. Next:
reproduce DEAD 5 GHz launches and instrument the front-end on them (PLL/synth lock, AGC/IGI, RX
gain); pursue the RF-0x18-tune-region separator `cold_divergence.py` flagged on its 1 DEAD sample.

### 2026-06-24 (cont'd 2) — the live DC-cancellation suspect; IQK status corrected

Chasing the "analog" conclusion: searched the vendor for the RF cal it runs that we don't.
**Correction — IQK is NOT disabled** (an earlier draft of this entry wrongly claimed it was). The
comment at `rtl8821c_phy.c:807` (`/*phy_iq_calibrate_8821c(...)*/`) is a vestigial refactor; the LIVE
call is `:808 rtw_phydm_iqk_trigger(adapter)` → `halrf_segment_iqk_trigger` (host-side, 8821C). It
fires inside the channel-set path gated on `bNeedIQK` (`HW_VAR_DO_IQK`). BUT `verify_pcap` passes
byte-exact through cold + 65 hops with no host-side IQK, and the `HW_VAR_DO_IQK` set-sites are
link/AP/TDLS/MCC/sreset/CAC-finish events — not plain monitor channel hops — so the captured monitor
session never triggered IQK. Open question: does init / the first channel-set set `bNeedIQK` (which a
cold capture might not show), i.e. does our truly-cold boot skip a one-time IQK the chip needs? Trace
the airmon/`rtw_hal_init` path for a `DO_IQK` before the first `set_channel_bwmode`. So IQK is a live
SECONDARY suspect, not ruled out. LCK (LC/VCO tank, `halrf_8821c.c:332`; lock-progress bit
`RF0x18[15]`, AACK busy bit `RF0xca[12]`) is not in the captured cold/hop path → periodic cal, not
the cold divergence; its lock bits are cal-in-progress flags, not a passive lock-detect.

The one per-boot-variable analog cal we DO run is `dm._dc_cancellation`: it measures a live DC offset
off the BB dbg port (TRX stopped, LNA off) and writes the path-A I/Q compensation to `0xc10`/`0xc14`.
A corrupted measurement (e.g. raced by the RX reader thread, which runs during cold_bringup) → wrong
compensation → DC-saturated ADC → demod sees garbage → RX dead, per boot. Invisible to `verify_pcap`
(the replay feeds back the captured measured value, so the gate can't see a bad LIVE one). Probe is
staged in `bringup_cointoss.py` (`0xc10`/`0xc14`/`0xa9c`/`0xc1c` in the dump) but couldn't run — the
card re-entered the degraded all-dead state (~40 soft re-inits this session). NEXT, on a fresh card:
(1) diff `0xc10`/`0xc14` good-vs-dead — if they differ, the DC cal is it; (2) test `bringup_timing.py
quiet` (no RX reader during init) — if quiet stabilizes RX, the reader is racing the DC measurement.

### 2026-06-24 (cont'd) — the DELAY fix is NOT the cure; RX is chip-side flaky

Fresh-plug validation: honoring the power-seq DELAY did not collapse the dead-rate. `bringup_bands.py`
(count ch1 then tune ch36 and count, per launch) shows why the earlier ch1-parked loops misled —
they conflated "2.4 GHz/RF18 dead" with "bring-up dead". Real picture: RX delivery is flaky
per-launch AND per-band AND across band switches (e.g. ch1 GOOD → ch36 dead after the switch; ch1
dead → ch36 GOOD; both-dead; both-good — all seen in 6 launches). Dead = `RXFF_PTR` stuck at 0 (the
chip's RX FIFO is not filling) with the PHY demod running (FA counters tick) and byte-identical MAC
config — so it is chip-side PHY/MAC RX, NOT the USB bulk-IN pipe (a pipe stall would still advance
RXFF_PTR) and NOT a MAC register. The register diff was then WIDENED to the RX-enable / TRX-stop BB
state (0x808 CCK-block, 0x838 OFDM-RX-CCA, 0xa04, 0x520, 0xc00, 0x900) — also byte-identical
good-vs-dead, so `dm.stop_ic_trx`'s revert is fine and the RX block is enabled in dead launches too.
Conclusion: across a launch doing ~78 frames/s and one doing 0, the ENTIRE register state is
identical; only RXFF_PTR (a consequence) and IGI (DIG) differ. A digital-config bug is ruled out —
this is ANALOG: the demod runs but can't lock onto real packets = an uncalibrated RX front-end. The
vendor runs IQK / RX gain-DC cal (init + around channel-set); this port implements none (`dm.py`
notes IQK is "triggered later" — but nothing triggers it). The DELAY fix stays (a real, correct fix —
the vendor does that delay) but is not the cure. Next: port the 8821C RX calibration (large; needs a
replug-friendly rig — the card degrades after ~8 soft re-inits, which confounds loop testing).

### 2026-06-24 — bring-up coin toss: the power-seq DELAY was a no-op

Bring-up RX was a coin toss — ~80%+ of cold boots came up dead on BOTH bands (a fresh plug too, per
the user), control path fully alive (registers read, FA counters tick) but the RX FIFO never filled.
`bringup_cointoss.py` reproduced it headlessly (e.g. 2 GOOD / 10 DEAD) and gave the decisive clue:
the RX-path register dump is IDENTICAL between good and dead launches (CR=0x6ff, RCR=0x90000001,
RXDMA_STATUS=0, filters all the same). Identical config + different outcome = a timing race, not a
missed register — which is why a "faithful" port that passes `verify_pcap` still failed.

The seam: the byte-gate matches the op SEQUENCE but is blind to delays between ops. Auditing the
bring-up for waits that emit no register op found it — `pwrseq._run_table` treated `_CMD_DELAY` as a
no-op ("replay strips it"), but `CARDEMU_TO_ACT` carries a 1 ms DELAY: the LDO settle after the
0x20[0]=1 power enable. Skip it → proceed before the rail settles → power-on lands marginal at
random. Vendor honors it at `halmac_common_88xx.c:3078` (`PLTFM_DELAY_US`). Fix: sleep offset us/ms;
gate stays green (a sleep is no op on the wire).

NOT HW-validated yet: by the time the bug was found the test card had degraded past its
~8-soft-reinit replug threshold (I was at ~34), so every launch was dead regardless and a 20 s rest
between boots didn't recover it. A fresh-plug `bringup_cointoss.py` run is owed. FW-download polls +
BB/AGC/RF tables were checked for other skipped delays — none. The degrade-after-N-boots behaviour
is a separate, still-open issue (likely its own missing reset on soft re-init).

### 2026-06-23 — 2.4 GHz RX root cause

The cold-boot pcap reproduces byte-for-byte, but on hardware 2.4 GHz RX was dead while 5 GHz
worked. `band_state_probe.py` (cold ch1 → 5 GHz → ch1) pinned it to RF18 bit16, perfectly
correlated with CRC failure across every run. The cold tune runs before RX-enable and the antenna
switch, and leaves the bit stuck set; the channel-switch path only ever clears BIT18/17 and byte0.
The fix replaced an earlier flaky 5 GHz-bounce (`_prime_2g_rx`) with a deterministic,
read-back-verified warm clear. Same session: RSSI had been decoding with a Jaguar-1 borrow;
switched to the real jgr2 format and it now reads sane (−60 to −84 dBm).

Not re-verified after the test card degraded (~8 cold boots without a replug, RF18 writes started
intermittently failing): fresh-plug 2.4 GHz revival, whether the phydm DIG watchdog sagging IGI
hurts beacon rate, and reader-vs-init USB ordering.
