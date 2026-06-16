# RTL8822BU — vendor/DKMS port (playbook)

> ## NEXT AGENT — START HERE
>
> **Goal: a 100% faithful byte-for-byte port of the morrownr vendor stack. Faithfulness over
> beacons** — a fully faithful port that yields 0 beacons is the win; a <100% port that "works" is
> not. Don't chase RX symptoms; reproduce the wire.
>
> **Done & gate-verified (cap-1 byte-for-byte unless noted):**
> - Full deterministic **cold init** (chip-ID → EFUSE → 2× power/FW/MAC → BB phy-reg/AGC → crystal →
>   RF-A/RF-B), ends op ~9410.
> - **`odm_dm_init` — PORTED IN FULL** (gate 9509→**9805**): the RX seed (dig / cck_pd /
>   env_monitor[nhm·clm·fahm] / adaptivity / ra_info) **and the entire RF-cal tail** — `cfo_tracking_init`
>   → `rf_init`(tx-pwr-track) → **`dc_cancellation`** (RX DC-offset cal) → **`tx_current_calibration`**
>   (TxA bias) → **`get_pa_bias_offset`** (PG PA-bias→RF). The remaining tail inits (antdiv / soml /
>   path_div / primary_cca / psd) are wire-silent on 8822b.
> - **`set_channel`** (switch_channel + bandwidth + band-switch + **PSD spur eliminator** +
>   **per-channel TXAGC**) — `verify_channels` 35/35/34 hops on cap-1/2/3.
> - Runnable **driver** (`driver.py`/`bringup.py`/`rx.py`); `test_hw.py --phase init` runs clean on HW.
>
> **Frontier = op 9805: TX-beamforming/MU-MIMO init** (`0x14c0`/`0x167c`/`0x1680`/`0x1c94`, confirmed
> `haltxbf8822b.c`), then **BT-coex HW init** and **monitor-mode MAC setup** (9805→~9872), then at
> op ~9873 the **per-channel cal scan** (the airodump hops). None of 9805→9872 is in `cold_bringup`:
> TX-beamforming + BT-coex are subsystems a passive-RX monitor driver does not use (marked skips, see
> "Coverage gaps"); the monitor MAC setup the driver does its own way. **Next real work: the per-channel
> scan** (Lead: port the per-channel unit once, run on-demand from `set_channel`, gate per-channel) +
> RX phy-status parse + TX descriptor (build-only).
>
> **IQK is FW-offloaded — confirmed.** `[SRC] hal_dm.c:75 phydm_fwoffload_ability_init(_, PHYDM_RF_IQK_
> OFFLOAD)`. So `phy_iq_calibrate_8822b` takes the FW-H2C path; the software IQK engine (`0x1b00`)
> appears in **0 of 29542 ops**. (The old "frontier 9556 = IQK" claim was an unverified guess — 9556 is
> `cfo_tracking_init`'s `0x10[6]`, long ported.) The per-channel FW-IQK in `set_channel` is un-ported.
>
> **The loop (proven — ~16 functions done this way):**
> 1. `uv run python scripts/verify_pcap.py rtl8822bu_dkms` → prints `FRONTIER -> op #N: <op>`.
> 2. Resolve the register (`grep define REG_… halmac_reg2.h`; RF reads = direct BB read at
>    `{0x2800,0x2c00}[path]+(addr<<2)`, RF writes = 0xC90/0xE90), then `grep -rn` the value/reg in the
>    vendor source to find the fn. The replay feeds every read, so read-dependent loops reproduce.
> 3. Port it (new fns in `cal.py`/`chan.py`; reuse `sipi`). Chain into the shared path (`cold_bringup`
>    for cold init; `set_channel` for per-channel cal).
> 4. Re-run the gate (advances), `uv run ruff check`, commit (one fn/commit, no AI trailer).
> 5. `test_hw.py --phase beacon` checks RX (passive; card plugged in + WinUSB-bound). **RX = 0 frames
>    even with the full `dc_cancellation`/TxA cal ported** — so the post-DM-init cal is NOT the RX
>    blocker the prior doc claimed. Real blocker unknown; likely the per-channel cal (kfree / FW-IQK)
>    or an RX-path config gap. Faithfulness is still the goal — port the per-channel cal regardless.
>
> **Hard rules:** cleanroom — port only from `usb_dumps_new/captures_rtl88x2bu/driver-source/`; do NOT
> open `chips/rtl8822bu/`, `chips/rtw88_base/`, or `scripts/rtl8822bu/` (`scripts/rtl8822bu_dkms/` is
> yours). Never fire live 802.11 TX. No AI-authorship trailer. Always `uv run python`. Stage only your
> files. Gate every milestone.
>
> **cap-2/3 caveat:** the gate is **cap-1-authoritative from `config_trx_mode` (op ~9467) onward** —
> cap-2/3 diverge there on a stale `central_ch_8822b` module-global (a benign cross-capture artifact,
> not a port bug; see "Coverage gaps"). Everything earlier is byte-clean on all three.

## Status

| Area | State |
|---|---|
| Cold init (chip-ID/EFUSE/power/FW/MAC/BB/RF) | ✅ byte-for-byte cap-1/2/3, ends op ~9410 |
| DM-init RX seed (dig/cck_pd/env_monitor/adaptivity/ra_info) | ✅ gate 9509→9556 (cap-1) |
| DM-init cal tail (cfo/rf_init/dc_cancellation/txcurrent_cal/pa_bias) | ✅ gate 9556→9805 (cap-1) |
| set_channel (+ spur eliminator + TXAGC) | ✅ 35/35/34 hops |
| RX decode + phy-status RSSI | ✅ verified vs 9292 real bulk-IN frames (median -66 dBm) |
| TX-beamforming / BT-coex / monitor MAC (op 9805→9872) | ⬜ skipped — TX/coex subsystems (not in cold_bringup) |
| **per-channel cal scan** (kfree-noop → tx-pwr → FW-IQK → coex) | ⬜ frontier op ~9873 — deferred per-channel cal |
| TX inject descriptor (build-only) | ⬜ remaining — no injector in the passive capture to byte-diff |
| Live RX (monitor beacons) | ⬜ 0 frames — RX-DMA/monitor-enable gap (cold init + decode proven OK) |

## Verified facts (ground truth)

- **USB** `[WIRE]`: `2357:0138` (Archer T3U Plus), single config; control ep0, **bulk-OUT 0x05**
  (FW/TX), **bulk-IN 0x84** (RX). Register IO = Realtek `bRequest=0x05` vendor control xfer (READ
  0xC0/WRITE 0x40, addr in wValue) `[SRC] include/usb_ops.h:19`. **0x4E0 page-switch mirror**: every
  ON-section access (`addr ≤ 0xFF` or `0x1000-0x10FF`) is followed by a 1-byte W to `0x4E0` carrying
  the IO low byte `[SRC] os_dep/linux/usb_ops_linux.c:171`; reproduced in `transport.py`.
- **Re-runnability** = clean reset every boot (not warm-skip). `mac_pwr_switch_usb_8822b` detects
  already-on (returns UNCHANGE); `rtw_halmac_poweron` then forces card_dis_flow OFF→ON
  `[SRC] hal_halmac.c:2744`. Ported in `mac.power_on`; the OFF→ON cycle has no cold capture (proven by
  HW double-run). *Open: whether it recovers on Windows+WinUSB.*

## Cold init (CLEARED — byte-for-byte cap-1/2/3; code + commits are the live record)

`bringup.cold_bringup` is the canonical sequence (shared by the driver + `verify_pcap`). Two cycles —
`hal_read_mac_hidden_rpt` then the real `rtl8822b_hal_init` — reaching op ~9410:

- **chip-ID** `chipid.py`: `R 0xFC`/`0xF1` (chip 0x0A=8822B, cut 0x3=D); USB3 intf-phy param
  `{0x0001,0xA841}` → `W 0xFF0D/0E/0C` `[SRC] halmac_usb_8822b.c:107`; `R32 0xF0/0xF4/0x68`.
- **EFUSE** `efuse.py` (read up front, before power-on): driver-side dump (`R 0x0A` autoload,
  WIFI-bank `R 0x35`, the 1024-byte `0x30` poll loop), PG-header → 768 B logical map. Decoded:
  rfe_type **3** (iFEM), crystal_cap **0x2F**, channel_plan **0xa5**, MAC, PA-bias. The PG tx-power
  block @ logical **0x10** is decoded in `txpower.py` (see TXAGC).
- **MAC power-on** `mac.power_on` `[SRC] hal_halmac.c:2705`: pre_init_system_cfg (pin-mux, enable_bb_rf
  off), the `card_en_flow` pwr sequence (`pwrseq.py`, filtered by USB + cut BIT4), init_system_cfg.
  Poll loops consume the recorded reads (cap-3 polls `0x0005` one extra time — proof it's dynamic).
- **Firmware** `firmware.py`: morrownr `array_mp_8822b_fw_nic` v30.20 (161240 B, in
  `assets/rtl8822bu_fw.bin` — **not** the linux-firmware blob). `download_firmware_88xx`: txfifo gate,
  interleaved reg save/set, 40 BEACON-qsel rsvd-page TX packets (48-byte desc + XOR-16 cksum) on
  bulk-OUT 0x05 → iDDMA to MCU, FW-ready poll `0xC078`. Gate replays merged ctrl+bulk.
- **MAC init + FW-info** `mac.py`/`firmware.py`: init_trx_cfg (queue map, txff page math
  rsvd_boundary 1996), protocol/edca/wmac (RCR), send_general_info (2 H2C: FW boundary + PHYDM
  rfe/cut/rf/ant/package), dump_h2cq readback, reg-H2C, C2H report read+discard.
- **BB + RF tables** `bb.py`/`rf.py`/`phy_cond.py`: PRE/POST `0x808[28:29]` (no-op here), phy-reg
  (1492 rows), AGC (10684 rows, 328 cut/rfe conds → 521 selected for rfe 3), crystal cap → 0x24/0x28,
  RF-A (402) + RF-B (353) masked RF writes via `phy_cond.walk`. tx-pwr-track table is software-only.

## Post-PHY: full `odm_dm_init` (PORTED, ends op 9805) + TX-bf/coex/monitor + the per-channel scan

The capture past op 9410 is `odm_dm_init`, then (op 9805+) TX-beamforming + BT-coex + monitor MAC setup,
then (op ~9873+) the **per-channel cal scan** — the vendor pre-cals every channel ×2 bands (the captured
monitor + 2.4/5 GHz hops drive it via `set_channel`). **Lead decision: do NOT replay the scan — port the
per-channel unit once and run it on-demand from `set_channel`**, gated per-channel by `verify_channels`.

**DM init (`odm_dm_init` `[SRC] phydm.c:1789`) — PORTED in full** (`cal.py`, gate 9509→9805):
- *RX seed* (→9556): `halrf_init`(aac_check) → `rfe_init` → `common_info_self_init` (cck_setting +
  rf_path_rx + somlrxhp `0x19a8`) → `dig_init` (get_igi 0xC50, big_jump 0x8C8; `big_jump_step1` latched
  into `DmState`) → `cck_pd_init` (type1: 0xA0A=0x83) → `env_monitor_init` (ccx_hw_restart 0x994;
  nhm/clm/fahm — 11 thresholds `th[i]=((igi-14)<<1)+4i`) → `adaptivity_init` (EDCCA
  0x944/0x8a4/0x520/0x524) → `ra_info_init` (ARFR 0x494/0x498/0x4a4/0x4a8).
- *RF-cal tail* (→9805): `rssi_monitor_init`(sw no-op) → **`cfo_tracking_init`** (crystal-cap-by-WiFi
  `odm_set_mac_reg(0x10,0x40,1)`) → **`rf_init`** (tx-pwr-track init; `get_swing_index` reads
  `0xc1c[31:21]`) → **`dc_cancellation`** (`phydm.c:3496`, the RX DC-offset cal: per path A/B — stop
  TRX → IGI 0x7E → stop 3-wire → park debug-port 0x200/0x202 → stop ck320 → read `0xFA0` → restore;
  then CCK-path DC comp `0xA9C[20]` + per-path I/Q offsets to `0xC10/0xC14`,`0xE10/0xE14`) →
  **`tx_current_calibration`** (`phydm_rtl8822b.c:240`, TxA-bias: RF `0xef`=0x200 + 12× RF `0x18`
  sweep reading RF `0x61`, keyed by efuse `0x3D7/0x3D8`=`0xF0` ⇒ no RF `0x30` correction) →
  **`get_pa_bias_offset`** (`halrf_kfree.c`, PG PA-bias 0x3D5=0xF2/0x3D6=0xF0 → signed offset folded
  into RF `0x3f` via the `0xef`[10] LUT). Remaining tail inits (antdiv / soml / path_div / primary_cca /
  psd) are **wire-silent on 8822b** (verified each). `DmState` carries the few values whose *writes*
  derive from cached reads (dig IGI + big_jump_step1, cck_new_agc, dbg-port priority, stop_ic_trx s/r).

**Frontier op 9805 = TX-beamforming/MU-MIMO init** (`hal_txbf_8822b_init`: `0x14c0`/`0x167c`/`0x1680`/
`0x45f`/`0x1c94`), then **BT-coex HW init** + **monitor-mode MAC setup** (→~9872), then op ~9873 the
**per-channel cal scan**. None of 9805→9872 is in `cold_bringup` (see "Coverage gaps"). **No software
IQK runs anywhere** — `0x1b00` (the IQK engine) is in 0 of 29542 ops; IQK is FW-offloaded
`[SRC] hal_dm.c:75`. The per-channel kfree no-ops on this card (`phydm_config_kfree` early-returns:
power-trim PG blank), so the per-channel cal that *does* run is tx-power (ported) + FW-IQK (H2C, un-ported).

**RX status (HW, 2026-06-16):** cold init + the FULL `odm_dm_init` (incl. `dc_cancellation`'s RX
DC-offset comp + TxA/PA-bias cal) run clean on the card, but `test_hw.py --phase beacon` still shows
**0 frames / 0 beacons** — BB hears RF energy (FA 0xf48 / CCA 0xf08 climb) but `bulk_in` returns **0
bytes** (not garbage — the HW isn't DMA-ing RX packets to the USB bulk-IN pipe at all).
**The cold init + RX decode are now PROVEN faithful/correct, so the blocker is downstream:** capture-1
holds **9292 real bulk-IN RX frames** the vendor driver received in monitor mode, and `rx.iter_frames`
decodes them all to valid beacons/probes with sensible RSSI (median -66 dBm) — so the rx_pkt_desc walk
+ phy-status parse are right. The post-DM-init cal is likewise NOT the blocker (full cal ported, still
0). **Remaining suspect = the monitor-mode RX-enable / USB RX-DMA setup** the driver does ad-hoc
(`test_hw` sets `RCR`=0x9000380F + `RXFLTMAP*`=0xFFFF) rather than porting the capture's airmon
monitor-entry sequence — that sequence (and the USB RXDMA aggregation/threshold in `init_usb_cfg`) is
the place to diff next. (Ruled out: the capture's monitor `REG_CR`=0x06ff vs the driver's 0x04ff is
only `BIT_MAC_SEC_EN`, the HW security engine — irrelevant to unencrypted-beacon RX.) Per the Lead's
"faithfulness over beacons", this is logged for the hands-on pass, not chased blind.

### Per-channel cal — in `set_channel`, gated by `verify_channels` (35/35/34 hops)

- **switch_channel** `chan.py`/`sipi.py`: RF18 channel + AGC `0x958` + clock `0x860` + CCK filter
  `0xA24/0xA28` + phase-noise `0xBE` + igi_toggle + ccapar (rfe-3 iFEM `0x82C/830/838`) + spur_reset.
  SIPI primitives: RF read = direct BB read at `{0x2800,0x2c00}[path]+(addr<<2)`; RF write packs
  `((addr&0xFF)<<20|data[19:0])` into `0xC90`/`0xE90`.
- **PSD spur eliminator — PORTED** (`chan._dynamic_spur_det_eliminate`): the read-dependent PSD sweep
  on spur channels (2.4G 5-8/13, 5G 153/161) + NBI notch (`0x87C`) + CSI notch (`0x880-0x89C` +
  `0x874[0]`). The replay feeds 0xF44, so the in-capture spur (ch6 PSD 0x197 ≥ 0x8D) drives the notch.
- **bandwidth** (`mac_switch_bandwidth` HALMAC cfg_ch_bw + `config_phydm_switch_bandwidth_8822b`,
  20 MHz) + **band switch** (both 2.4↔5 crossings; rfe-3 iFEM + SoML branch on `0x19a8[31]`).
- **TXAGC tx-power — PORTED** (`txpower.py`): `rtl8822b_set_tx_power_level` → per path × rate-section,
  `power_idx = phy_get_pg_txpwr_idx` (EFUSE PG base @ 0x10 + section/ntx BW20 diff), packed 4
  rates/dword at `0x1d00+(hw_rate&0xfc)`/`0x1d80`. **Faithfulness — build-config, not pcap-coincidence:
  the captured build sets `CONFIG_TXPWR_BY_RATE_EN=n` + `CONFIG_TXPWR_LIMIT_EN=n`
  `[SRC] driver-source/Makefile:130,132`** → by_rate folds to 0 (`phy_get_txpwr_target:5940`) and
  `phy_get_txpwr_lmt` early-returns → `power_idx = base`, **domain-independent** (verified base==wire,
  0 mismatches). PHY_REG_PG + the 7215-entry txpwr_lmt tables are inert in this build → correctly not
  ported. Caveat: no regulatory TX-power cap (matches the captured default).
- **Deferred per-channel cal:** after the ported retune + TXAGC, each hop runs **FW-IQK** (H2C image-
  rejection; 8822b has no software DPK) + the BT-coex band-notify. `verify_channels` lands `set_channel_bw`
  on that post-power boundary. Gate skips: the 2 band crossings (unported BT-coex `0xCBC` precedes TXAGC)
  + a few slice artifacts (window head lands mid-cal).

## Coverage gaps (verified one axis only)

- **TX-beamforming / BT-coex / monitor-MAC (op 9805→9872) — deliberately not in `cold_bringup`.**
  After `odm_dm_init` the vendor runs `hal_txbf_8822b_init` (MU-MIMO sounding/precoding: `0x14c0`,
  `0x167c`, `0x1680`, `0x45f`, `0x1c94`), BT-coex HW init (RFE/GPIO/`0x1700` LTE-coex), then monitor-mode
  MAC setup. TX-beamforming + BT-coex are **TX/combo subsystems a passive-RX monitor driver never
  exercises** (`# TODO untestable`-class skips, like a 2T2R card's unused 3T3R arms); the monitor MAC
  setup the driver re-does its own way (RCR=0x9000380F). **Un-audited risk:** the monitor-MAC RX-DMA
  writes (op 9847+) are not yet byte-diffed against the driver's monitor path — a candidate for the
  RX=0 blocker. The per-channel **FW-IQK** (H2C, `[SRC] hal_dm.c:75` IQK offload) is likewise un-ported.
- **per-channel kfree no-ops:** `phydm_config_kfree` early-returns on this card (power-trim PG blank,
  `!(pwrtrim->flag & KFREE_FLAG_ON)`), so the per-channel set_channel cal that runs is tx-power + FW-IQK,
  not kfree. (Distinct from the one-time `get_pa_bias_offset`, which *does* run — that's PG 0x3D5≠0xff.)
- **cap-2/3 from op ~9467:** `config_trx_mode`'s closing `phydm_rfe_8822b(central_ch_8822b)` is a
  no-op when the channel is 0 (or 15-35) but writes the iFEM table otherwise. `central_ch_8822b` is a
  **module global** `[SRC] phydm_hal_api8822b.c:34` set only by `config_phydm_switch_channel`; DM init
  runs before any channel switch, so it's 0 on the first cold boot (cap-1 — port reproduces it) but
  stale (ch 1, from the prior scan — the captures share a loaded module via unplug/replug, no rmmod)
  on cap-2/3, which emit the extra iFEM block and diverge. Benign cross-capture artifact → **cap-1 is
  authoritative from config_trx_mode on**. (The DM seed is identical on all 3 since `channel ≤ 14`.)
- **USB2-link branches** untested — all captures are USB3 (`REG_SYS_CFG2+3==0x20`
  `[SRC] halmac_usb_88xx.c:48`). The USB2 sides of `pre_init_system_cfg` `0xFE5B`, USB RXDMA/agg, and
  bulk-OUT sizing are source-ported-but-uncaptured.
- **Warm OFF→ON power cycle**: source-ported, no cold capture (cold boots return SUCCESS).

## Cleanroom rules

- Do **not** open `chips/rtl8822bu/`, `chips/rtw88_base/`, or `scripts/rtl8822bu/` (mainline + base +
  tooling — reading them produces a hybrid). The shared gate engine `scripts/rtw88_pcap_replay.py` is
  fine (family tooling, not driver code).
- Port from the **HALMAC + PHYDM (ODM)** vendor source. Sanctioned refs: that source + the sibling
  `chips/rtl8812au_dkms/`·`rtl8821au_dkms`·`rtl8814au_dkms` (a different HAL — expect new chip-local
  modules, not `rtw88_base`/`rtl88xxau_base` reuse).

## Gates + provenance

- **`verify_pcap.py rtl8822bu_dkms [<cap>]`** — replays `cold_bringup` vs the merged ctrl+bulk stream;
  prints reproduced-op count + `FRONTIER`. **`verify_channels.py <cap>`** — per-hop `set_channel_bw`
  diff (35/35/34). **`test_hw.py --phase open|init|beacon`** — live HW smoke. The engine feeds recorded
  reads back so read-dependent code reproduces; every write/bulk packet is byte-checked.
- Card: Archer T3U Plus v1 `2357:0138`, CUT_D, 2T2R, dual-band (Zadig→WinUSB on Windows; unbind kernel
  driver on Linux). Vendor: morrownr `rtl88x2bu` 5.13.1, source @
  `usb_dumps_new/captures_rtl88x2bu/driver-source/`. Captures: `capture-1/2/3.pcap` + `_logs/`
  (cold-boot, monitor, 2.4 then 5 GHz hops; `iw.log` has the per-channel `set channel` windows).

## Acceptance + working style

- Before "done": `planning/PORTING.md` § Post-Port Checklist (waiver review = zero waived init/airmon
  ops; skip audit = every un-emitted branch is a marked `# TODO untestable: <why>`; cap-1/2/3 coverage;
  async producers DIG/CCK-PD; per-hop recal + lock). Gate-green is necessary, not sufficient.
- Work inline in the main session (subagents only for parallel independent search). Gate + commit each
  milestone (one fn/commit, stage only your files, no AI trailer). Keep this doc current and concise
  with `[SRC]`/`[WIRE]` citations. TX `deauth_hw.py` is the human's trigger; the agent never
  live-injects.
