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
> - Full **`odm_dm_init` RX seed** (dig / cck_pd / env_monitor[nhm·clm·fahm] / adaptivity / ra_info),
>   gate 9509→9556.
> - **`set_channel`** (switch_channel + bandwidth + band-switch + **PSD spur eliminator** +
>   **per-channel TXAGC**) — `verify_channels` 35/35/34 hops on cap-1/2/3.
> - Runnable **driver** (`driver.py`/`bringup.py`/`rx.py`); `test_hw.py --phase init` runs clean on HW.
>
> **Frontier = op ~9556: the IQK** (`halrf_iqk_8822b.c`). Continue the cold-gate port from here.
> Remaining: IQK + LCK, the one-time per-channel DPK, TX descriptor (build-only), RX phy-status parse.
>
> **The loop (proven — ~12 functions done this way):**
> 1. `uv run python scripts/verify_pcap.py rtl8822bu_dkms` → prints `FRONTIER -> op #N: <op>`.
> 2. Resolve the register (`grep define REG_… halmac_reg2.h`; RF reads = direct BB read at
>    `{0x2800,0x2c00}[path]+(addr<<2)`, RF writes = 0xC90/0xE90), then `grep -rn` the value/reg in the
>    vendor source to find the fn. The replay feeds every read, so read-dependent loops reproduce.
> 3. Port it (new fns in `cal.py` or a new module; reuse `sipi`). Chain into `bringup.cold_bringup`.
> 4. Re-run the gate (advances), `uv run ruff check`, commit (one fn/commit, no AI trailer).
> 5. When the BB completes packets, `test_hw.py --phase beacon` checks RX (passive; the card is
>    plugged in + WinUSB-bound). RX is currently 0 frames — the IQK is the confirmed blocker (below).
>
> **Hard rules:** cleanroom — port only from `usb_dumps_new/captures_rtl88x2bu/driver-source/`; do NOT
> open `chips/rtl8822bu/`, `chips/rtw88_base/`, or `scripts/rtl8822bu/`. Never fire live 802.11 TX. No
> AI-authorship trailer. Always `uv run python`. Stage only your files. Gate every milestone.
>
> **cap-2/3 caveat:** the gate is **cap-1-authoritative from `config_trx_mode` (op ~9467) onward** —
> cap-2/3 diverge there on a stale `central_ch_8822b` module-global (a benign cross-capture artifact,
> not a port bug; see "Coverage gaps"). Everything earlier is byte-clean on all three.

## Status

| Area | State |
|---|---|
| Cold init (chip-ID/EFUSE/power/FW/MAC/BB/RF) | ✅ byte-for-byte cap-1/2/3, ends op ~9410 |
| DM-init RX seed (dig/cck_pd/env_monitor/adaptivity/ra_info) | ✅ gate 9509→9556 (cap-1) |
| set_channel (+ spur eliminator + TXAGC) | ✅ 35/35/34 hops |
| **IQK + LCK** | ⬜ frontier op ~9556 — confirmed RX blocker |
| per-channel DPK · TX inject · RX phy-status | ⬜ remaining |

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

## Post-PHY: DM-init RX seed + per-channel cal (frontier = IQK)

The capture past op 9410 is RF calibration. The **all-channel scan** (op ~9700→29542) pre-cals every
channel ×2 bands ×2 passes (IQK setup → per-channel DPK 2.4G/5G → TSSI → settle ch 1). **Lead
decision: do NOT replay the scan — port the per-channel unit once and run it on-demand from
`set_channel`**, gated against a per-channel slice.

**DM init (`odm_dm_init` `[SRC] phydm.c:1789`) — PORTED in full** (`cal.py`, gate 9509→9556):
`halrf_init`(aac_check) → `rfe_init` → `common_info_self_init` (cck_setting + rf_path_rx + somlrxhp
`0x19a8`) → `dig_init` (get_igi 0xC50, big_jump 0x8C8) → `cck_pd_init` (type1: 0xA0A=0x83) →
`env_monitor_init` (ccx_hw_restart 0x994; nhm/clm/fahm — 11 thresholds `th[i]=((igi-14)<<1)+4i` into
0x998/0x99c/0x9a0/0x994 + 0x990 + 0x1c38/0x1c78/0x1c7c/0x1cb8) → `adaptivity_init` (EDCCA
0x944/0x8a4/0x520/0x524; forgetting-factor + decision-opt are no-ops, edcca_mode≠ADAPT) →
`ra_info_init` (ARFR 0x494/0x498/0x4a4/0x4a8).

**Frontier op ~9556 = the IQK** (`halrf_iqk_8822b.c`, read-dependent RF image-rejection cal): `R 0x10`
macbb-backup → `0x0c1c`/`0x198c` RF-mode. LCK + the one-time per-channel DPK follow it.

**RX status (HW, 2026-06-15):** the cold init + full DM init run clean on the card, but
`test_hw.py --phase beacon` shows **0 frames / 0 beacons** — the BB hears RF energy (FA 0xf48 / CCA
0xf08 climb) but no frame lands in the RXFF (`bulk_in` returns 0 bytes). The DM init was necessary but
**not sufficient**; the **IQK is the confirmed RX blocker**. The RX path is fully open: `REG_CR`=0x04ff,
`RCR`=0x9000380F, `RXFLTMAP0/1/2`=0xFFFF, bulk-IN 0x84 (`rx.py` decodes the 24-byte rx_pkt_desc,
FCS-stripped).

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
- **Deferred:** per-channel **DPK** (the *real* read-dependent pre-distortion in the cold scan, op
  ~9900+) — rides with TX. Gate skips: the 2 band crossings (unported BT-coex `0xCBC` precedes TXAGC)
  + a few slice artifacts (window head lands mid-cal).

## Coverage gaps (verified one axis only)

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
