# RTL8822BU — vendor/DKMS port (playbook)

> ## NEXT AGENT — START HERE
>
> **Goal: a 100% faithful byte-for-byte port of the morrownr vendor stack. Faithfulness over
> beacons** — a fully faithful port that yields 0 beacons is the win; a <100% port that "works" is
> not. Don't chase RX symptoms; reproduce the wire.
>
> **Current state (honest): cold init faithful; runtime PARTIAL.** `verify_pcap` byte-verifies only the
> cold init (op 0–9855, ~33% of the captured ops). The runtime wire (opmode block, hop tails/DPK, the
> DIG/FA watchdog, spur re-eval, TX) is unported. See "Faithfulness scoreboard". Do NOT say "100%
> faithful" — say what the gate actually covers.
>
> **Done & gate-verified (cap-1 byte-for-byte unless noted):**
> - **The ENTIRE vendor chip init `rtl8822b_init` — reproduced byte-for-byte to op 9855.** This is the
>   whole `verify_pcap` deterministic span: chip-ID → EFUSE → 2× power/FW/MAC → BB/AGC/crystal → RF-A/B →
>   **`odm_dm_init` in full** (dig / cck_pd / env_monitor / adaptivity / ra_info RX seed + the RF-cal tail
>   `cfo_tracking_init` → `rf_init` → **`dc_cancellation`** RX-DC-offset → **`tx_current_calibration`** →
>   **`get_pa_bias_offset`** → **`psd_init`**) → the **`rtl8822b_init` tail** (**`phy_bf_init`** MU-MIMO
>   seed `txbf.py`, **wifi-only coex** antenna/RFE `coex.py`, **`init_misc`** CAM/RCR/sec-en/TXQ/AMPDU).
> - **`set_channel`** (switch_channel + bandwidth + band-switch + **PSD spur eliminator** +
>   **per-channel TXAGC**) — `verify_channels` 35/35/34 hops on cap-1/2/3.
> - **`enable_monitor`** (`mac.py`) — the airmon monitor RX-enable (`set_opmode_monitor`): MSR no-link,
>   RCR=**0x90000001**, `config_rx_info(PHY_SNIFFER)`, `DRVINFO_SZ|=0x80`, RXFLTMAP0/1/2=0xFFFF. **Gate-
>   verified 20/20** vs the capture's monitor switch (`verify_pcap` sliced-window check) — the very op
>   the first RX frame lands after. Wired into `driver.connect()` + `test_hw`.
> - **RX decode + jaguar2 phy-status RSSI** — verified vs 9292 real bulk-IN frames (median -66 dBm).
> - Runnable **driver** (`driver.py`/`bringup.py`/`rx.py`); `test_hw.py --phase init` runs clean on HW.
>
> **Frontier = op 9855: the OS interface-up / opmode state machine** (`hw_var_set_opmode` — MSR/network-
> type `0x4e`, MAC addr `0x610/0x614` from EFUSE, beacon ctrl `0x550`, RX-filter `0x6a2`). This is the
> driver's **connect()/`WlanInterface` layer** (mode-dependent managed↔monitor), NOT chip bring-up — a
> design-level port (discuss before execution). After it: op ~9873 the **per-channel cal scan** (airodump
> hops, `verify_channels`' domain), then **op 28910 = aireplay-ng TX injection** — the intentional gate
> stop (a different program's traffic, the legitimate waiver). So the full driver-constructed wire up to
> injection is covered by `verify_pcap` (chip init → 9855) + `verify_channels` (the sweep).
>
> **IQK is FW-offloaded — confirmed.** `[SRC] hal_dm.c:75 phydm_fwoffload_ability_init(_, PHYDM_RF_IQK_
> OFFLOAD)`. So `phy_iq_calibrate_8822b` takes the FW-H2C path; the software IQK engine (`0x1b00`)
> appears in **0 of 29542 ops**. The per-channel FW-IQK in `set_channel` is un-ported (an H2C subsystem).
>
> **The loop (proven — ~16 functions done this way):**
> 1. `uv run python scripts/verify_pcap.py rtl8822bu_dkms` → prints `FRONTIER -> op #N: <op>`.
> 2. Resolve the register (`grep define REG_… halmac_reg2.h`; RF reads = direct BB read at
>    `{0x2800,0x2c00}[path]+(addr<<2)`, RF writes = 0xC90/0xE90), then `grep -rn` the value/reg in the
>    vendor source to find the fn. The replay feeds every read, so read-dependent loops reproduce.
> 3. Port it (new fns in `cal.py`/`chan.py`; reuse `sipi`). Chain into the shared path (`cold_bringup`
>    for cold init; `set_channel` for per-channel cal).
> 4. Re-run the gate (advances), `uv run ruff check`, commit (one fn/commit, no AI trailer).
> 5. `test_hw.py --phase beacon` checks RX (passive; card plugged in + WinUSB-bound). **Live monitor RX
>    WORKS** (ch6 → 7 APs/383 bcn; hop 1-13 → 25 APs/447 bcn) since the `switch_band`-on-initial-tune
>    fix (`8c2e907`) — see "RX = 0 frames — RESOLVED". The remaining work is faithfulness, not RX.
>
> **Hard rules:** cleanroom — port only from `usb_dumps_new/captures_rtl88x2bu/driver-source/`; do NOT
> open `chips/rtl8822bu/`, `chips/rtw88_base/`, or `scripts/rtl8822bu/` (`scripts/rtl8822bu_dkms/` is
> yours). Never fire live 802.11 TX. No AI-authorship trailer. Always `uv run python`. Stage only your
> files. Gate every milestone.
>
> **cap-2/3 caveat:** the gate is **cap-1-authoritative from `config_trx_mode` (op ~9467) onward** —
> cap-2/3 diverge there on a stale `central_ch_8822b` module-global (a benign cross-capture artifact,
> not a port bug; see "Coverage gaps"). Everything earlier is byte-clean on all three.

## Faithfulness scoreboard — what "pcap-faithful" actually covers

**Do not call this port "pcap-faithful" without this qualifier.** It means exactly one thing:
`verify_pcap` reproduces the **cold init** (op 0–9855, ~33% of the 29 542 captured control ops)
byte-for-byte. That is the only span with a standing 100% gate. The runtime wire (op 9855 → ~28910,
where aireplay TX begins) is partial or unported:

| Span | State |
|---|---|
| Cold init, op 0–9855 | ✅ byte-for-byte (`verify_pcap`) |
| `set_channel` hop **prologue** | ✅ byte-for-byte (`verify_channels`, 132–776 ops/hop) |
| `enable_monitor` (20 ops) | ✅ slice-verified (`verify_pcap`) |
| initial channel-set / `switch_band` | ✅ 165 ops, offline replay — **no standing gate** |
| opmode block (op 9855) — MAC-addr | ✅ ported (`set_mac_addr`, EFUSE, replay-verified) |
| opmode block — LED (`0x4a`/`0x4e`) | ⏭️ SKIPPED by lead decision — cosmetic async pinmux indicator (`LedControlUSB` timer; `0x62→0x28` not byte-reproducible) |
| opmode block — BCN_CTRL / RXFLTMAP1 | ⚠️ managed-vif defaults `enable_monitor` overrides (RXFLTMAP1→0xFFFF) — monitor-inert, not ported |
| **`phydm_watchdog`** (RX members) — `fa_cnt`→`reg_reset`→`cck_pd`→`dig`→`adaptivity` | ✅ ported + wired (2 s loop in connect()): `fa_cnt` replay-verified 20/20; DIG/cck_pd/adaptivity 15 unit tests. Un-freezes IGI (0xC50/0xE50), CCK-PD (0xA0A), EDCCA (0x8A4) |
| `phydm_watchdog` — get_dbg_port_info | ⚠️ ADAPT-mode/diagnostic only — dormant in the CE NORMAL default (adaptivity_dbg_port 0x209 set-but-unused on 8822b) |
| `phydm_watchdog` — tx_power_tracking, cfo_tracking, ra_info | ⚠️ monitor dead-code (TX / association-specific; never run unlinked) — not ported, documented |
| TX descriptor (`build_inject_txdesc`, inject_frame) | ✅ ported — `update_txdesc` MGNT branch, **byte-matches the captured aireplay injector** (251 deauth TX frames, only HW seqctl varies); 8 unit tests |
| TX descriptor | ⛔ unported |

RX works, but the continuous runtime adaptation the capture runs (DIG/FA/spur) is not reproduced: the
golden capture holds **24 bcn/s median, 90+/s peak** while hopping; our RX measures lower. The honest
one-line status is **"cold init faithful; runtime partial,"** never "100% faithful."

**Opmode-block scope finding.** The opmode block (f19967–19999) is *not* HALMAC chip init — it is the
kernel net/mlme/cfg80211 + LED layer bringing up a default **managed** vif, which airmon then overrides
with monitor. Evidence: `0x4a`/`0x4e` are the HALMAC GPIO-pinmux **WL_LED** indicator
(`pinmux_wl_led_sw_ctrl` = `0x4e` BIT3; the full init pulls in the pinmux mode engine), driven by the
**async** `LedControlUSB` handler; `0x550=0x1c` is a 1-byte BCN_CTRL RMW (not the flat `InitBeaconParameters`
write16); `0x6a2=0x0001` is a managed mgmt filter that `enable_monitor` overwrites with `0xFFFF` ~400
frames later in the same capture. The only chip-state piece functional for wifit3 (a monitor/injection
tool that implements its *own* net layer) is the **MAC-addr — ported**. Open question for the lead:
byte-replay the kernel's managed-vif interface-up (port the mlme RMWs + the pinmux LED engine for a
cosmetic light), or treat it as out-of-scope like aireplay's TX? Until decided, it is flagged, not
silently skipped.

## Post-Port Checklist (planning/PORTING.md) — steps 1–6

1. **Waivers** — cold init (→op 9855) + airmon monitor entry reproduce single-cursor with zero
   waived ops; the only frontier is the OS opmode block (op 9855), outside the chip-init gate. ✅
2. **Skip audit** — documented, evidence-backed skips: LED (cosmetic async pinmux), BCN_CTRL/RXFLTMAP1
   (managed-vif, `enable_monitor`-overridden), watchdog `tx_power_tracking`/`cfo`/`ra` (TX/assoc
   dead-code in monitor), `get_dbg_port_info` (ADAPT-mode dormant). ✅
3. **Capture coverage** — cap-1 (cold boot) passes full single-cursor; cap-2/3 diverge at op 9468 on a
   **warm-reload stale `central_ch` software static** (not on the wire, not HW-readable) — our cold-boot
   `central_ch=0` is the faithful model, not a bug. ✅
4. **TX byte-diff** — `build_inject_txdesc` byte-matches the capture's 251 aireplay deauth TX descriptors
   (`update_txdesc` MGNT branch; MACID=1/G_ID=63/RTY_LMT/SW_DEFINE all source-pinned). ✅
5. **Async producers** — the always-on PHYDM DIG watchdog is *dispatched* (2 s loop, `connect()`), not
   stripped; `cfo`/`ra`/`tx_pwr` correctly don't run in monitor. ✅
6. **Recal cadence** — per-channel recal in `set_channel_bw` (band/channel/bandwidth/TXAGC); periodic
   DIG recal dispatched; the per-hop `_io_lock` prevents a cancelled tune leaving a stale channel. ✅
7. **Hands-on break-it pass** — the human's step (replug/soak/live-TX); `deauth_hw.py` is the TX tool.

## RX = 0 frames — RESOLVED (live monitor RX works)

**Root cause:** `set_channel_bw` ran `switch_band` only on a 2.4↔5 crossing, so the first tune
(`prev_ch=None`) skipped it. Cold init leaves the synth in 5 GHz, so the first 2.4 GHz tune *is* a
band change — and `switch_band` wires the 2.4 GHz RX path to the antenna (CCK enable `0x808[28]`,
iFEM RFE switch `0xCB0/0xCA0`, `0x8CC/0x8D8`). Without it the antenna stays unswitched → BB hears
only the noise floor → every frame fails FCS. Fix (`8c2e907`): run `switch_band` whenever
`(prev_ch is None or prev_ch>14)` differs from `ch>14`. HW: ch6 → 7 APs/383 bcn; hop 1-13 → 25 APs/
447 bcn.

The byte-for-byte gate stayed green because the *initial* channel-set is a capture window neither
`verify_pcap` (stops at op 9855) nor `verify_channels` (iw.log hops, all `prev_ch` set) slices — a
[[gate_not_faithful]] seam. If RX regresses, `test_hw --rxstats CH [--rcr 0x90000301]` tallies
`rx_pkt_desc` categories + RSSI + `RF_0x18` without the good-frame filter.

## Status

| Area | State |
|---|---|
| Cold init (chip-ID/EFUSE/power/FW/MAC/BB/RF) | ✅ byte-for-byte cap-1/2/3, ends op ~9410 |
| DM-init RX seed (dig/cck_pd/env_monitor/adaptivity/ra_info) | ✅ gate 9509→9556 (cap-1) |
| DM-init cal tail (cfo/rf_init/dc_cancel/txcurrent/pa_bias/psd_init) | ✅ gate 9556→9815 (cap-1) |
| rtl8822b_init tail (phy_bf_init / wifi-only coex / init_misc) | ✅ gate 9815→9855 (cap-1) |
| set_channel (+ spur eliminator + TXAGC) | ✅ 35/35/34 hops |
| `enable_monitor` (set_opmode_monitor, the monitor RX-enable) | ✅ gate 20/20 vs the airmon switch (op 10075) |
| RX decode + phy-status RSSI | ✅ verified vs 9292 real bulk-IN frames (median -66 dBm) |
| initial managed opmode (op 9855→9872) | ⬜ the OS's default vif setup — the monitor driver skips it (uses enable_monitor) |
| **per-channel cal scan** (kfree-noop → tx-pwr → FW-IQK) | ⬜ op ~9873 — verify_channels' domain (FW-IQK un-ported) |
| Live RX (monitor beacons) | ✅ HW: ch6 → 7 APs/383 bcn (8s); hop 1-13 → 25 APs/447 bcn. Fixed by switch_band-on-init (8c2e907) |
| TX inject descriptor (build-only) | ⬜ remaining — no injector in the passive capture to byte-diff |

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

**RX status (HW): pending re-test with the faithful `enable_monitor`.** The prior HW run showed 0
frames / 0 beacons with the driver's **ad-hoc** monitor RCR (`RCR`=0x9000380F, RXFLTMAP=0xFFFF) — but
`bulk_in` returned **0 bytes** (the HW wasn't DMA-ing RX to the bulk-IN at all). That ad-hoc path is now
replaced by the **vendor-faithful `enable_monitor`** (`set_opmode_monitor`, gate-verified 20/20), which
does what the ad-hoc path omitted: MSR no-link, `RCR`=**0x90000001** (not 0x9000380F),
**`config_rx_info(PHY_SNIFFER)`** (DRVINFO size 5 + `0x7d4[9]` sniffer bit), and **`REG_RX_DRVINFO_SZ`
|=0x80** (the DRVINFO-present flag). The missing DRVINFO/sniffer config is the prime RX-0 suspect: a
wrong RX-desc/DRVINFO size readily stalls the RX-DMA. In the capture, the first RX bulk-IN frame lands
the op *right after* this sequence. **Next: re-run `test_hw.py --phase beacon` on the card** (ask first
per the don't-auto-test-beacons rule) to see if the faithful enter lights up RX. Proven solid: cold
init (full `rtl8822b_init`) byte-for-byte, and `rx.iter_frames` decoding all 9292 real capture frames
to valid beacons/probes (median -66 dBm) — so the rx_pkt_desc walk + phy-status parse are correct.

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

- **`rtl8822b_init` tail (op 9815→9855) — now PORTED** (`txbf.py`/`coex.py`/`mac.init_misc`): the
  MU-MIMO/sounding seed (`phy_bf_init`: `0x14c0/0x167c/0x1680/0x45f/0x1c94`), the **wifi-only** coex
  antenna/RFE seed (`ex_hal8822b_wifi_only_hw_config`: `0x4c/0xcb4/0x974/0x1990/0xcbc/0x70` + gnt
  `0x1704/0x1700` — this dongle has `EEPROMBluetoothCoexist=false`, so the **full BT-coex stack is not
  in this capture** and is correctly not ported), and `init_misc` (CAM/RCR/sec-en/TXQ/AMPDU). MU-MIMO
  sounding/precoding itself only fires with TX+association (out of scope for passive RX).
- **OS interface-up / opmode (op 9855→9872) — driver-connect layer, un-ported.** `hw_var_set_opmode`
  (MSR/network-type, MAC addr from EFUSE `0x610/0x614`, beacon ctrl `0x550`, RX-filter `0x6a2`) is the
  mode-dependent (managed↔monitor) `WlanInterface`/connect() setup, not chip bring-up — a design-level
  port (Lead: discuss before execution). It is also the **RX-DMA/monitor-enable region** that is the
  candidate RX=0 blocker — to diff next when RX is chased. The per-channel **FW-IQK** (H2C,
  `[SRC] hal_dm.c:75` IQK offload) and the **aireplay-ng TX injection** (op 28910+, the gate's
  intentional stop — a different program's traffic) are likewise out of the chip-init gate.
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
