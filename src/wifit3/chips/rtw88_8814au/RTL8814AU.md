# RTL8814AU (Alfa AWUS1900) — Recon & Bring-up Plan

**Status:** RECON ONLY — no code yet. This doc is the plan; nothing below is
hardware-verified. Treat every fact as a hypothesis until it carries a `[HW]`
stamp.

Card: Alfa **AWUS1900**, AC1900, **4T4R** (4 RF paths), 2.4 + 5 GHz.
Chip: Realtek **RTL8814AU**, kernel module `rtw88_8814au`, `RTW_CHIP_TYPE_8814A`.
Family: **rtw88** (modern), shares `chips/rtw88_base/`.

---

## Build status

- **M1.a (FW extract + verify)** — ✅ DONE (offline). Pcap-extracted blob is a
  byte-for-byte match to linux-firmware. See §1.3.1.
- **M1 (FW upload + FW_READY)** — ✅ **DONE 2026-05-26, HW-VERIFIED.** First-try
  pass on the AWUS1900. `test_hw_8814au.py` cold boot:
  - `REG_SYS_CFG1 = 0x04441135` → **CUT_B**, cut_mask 0x04 (note for M3: PHY/RF
    tables are cut-gated — this sample is a B-cut).
  - `REG_MCUFW_CTRL` pre-FW `0x00602001` → post-upload `0x00606078` →
    post-validate `0x0060e078` (IMEM/DMEM DW_OK+CHKSUM_OK, FW_DW_RDY, FW_INIT_RDY).
  - 68256 bytes (DMEM 5792 + IMEM 62464) uploaded in 44 ms; no EALREADY cycle.
  - Enumerated at **bcdUSB 0x0200 (USB 2.0)** — see Known Gaps #7; matters for
    M5 RX throughput, not M1. Host needed Zadig→WinUSB binding for `0bda:8813`.
- **M2 (TRX init: queue mapping + FIFO + LLT + H2C)** — ✅ CODE COMPLETE,
  offline-verified (imports; FIFO math reproduces the kernel reserved-page
  invariant rsvd_boundary == rsvd_drv_addr = 1986; pubq 1858; txdma_pq_map
  0xf5b0 for 3-bulkout; 537 tests pass). **Awaiting HW gate** —
  `test_hw_8814au.py --phase mac_init` (LLT auto-init must clear + H2C ring
  verifies). Scope: `rtw_init_trx_cfg` only (`fifo.py`). The rest of
  `rtw8814a_mac_init` — the `mac_tbl` load + EDCA/SIFS/beacon timing — and
  `rtw_drv_info_cfg` are deferred: EDCA/mac_tbl belong with TX (M6), drv_info
  (RX physts + rxdesc-len quirk) with RX (M5). Bulk-OUT count is detected at
  runtime (`count_bulk_out_eps`) → selects `rqpn_table_8814a` row.
- **M3.a (init tables + 4-path RF access)** — ✅ CODE COMPLETE, offline-verified.
  All 7 tables extracted (`extract_init_tables.py`): mac/agc/bb + rf_a/b/c/d,
  ~29k u32s, phy_cond markers balanced (IF==ENDIF, IF+ELIF==neg for each).
  Chip-local `rf.py` (direct read + sipi write, 4 paths) — see Decisions #1.
  MAC-table replay dispatches 143 writes; RF read/write address math verified
  for all 4 paths; 537 tests pass. **Awaiting HW gate** —
  `test_hw_8814au.py --phase tables` (replay MAC table, read back sample regs).
- **M3.b (phy_set_param: BB/RF enable + conditional table loads + 4-path RF
  readback)** — ✅ CODE COMPLETE, offline-verified. `phy_set_param` (phy.py):
  BB/RF domain enable (4 paths), MAC+BB+AGC+RF(A-D) table loads via the walker,
  A->B/C/D RCK copy, RX-PSEL bracket. Skips EFUSE/tuning bits (crystal_cap,
  config_trx_path CCK antenna, DIG, pwrtrack, init_rfe_reg) — not needed for RF
  bring-up; deferred to M5/M6. **Awaiting HW gate** — `--phase phy`: RF_RCK1_V1
  must read back consistent + non-garbage on all 4 paths.
  - **Caveat — rfe_option is a placeholder (=1) until M4.** The AGC/RF tables are
    rfe-gated (IF/ELIF chains on rfe 0x01..0x0b). Verified the walker selects
    rfe-specific *data* correctly (rfe=2 vs rfe=3 load different values; the
    identical dispatch *count* of 265 is because every branch has equal entry
    count — NOT a walker bug). A wrong rfe loads a valid-but-suboptimal gain
    variant; M4 pins the real value from EFUSE.
- **M4 (EFUSE read)** — ✅ CODE COMPLETE, offline-verified. `efuse.py`: grant +
  1024-B physical dump + word-enable de-map to the 512-B logical map + parse
  `struct rtw8814a_efuse` fields (rfe_option 0xCA, rf_board_option 0xC1,
  xtal_k 0xB9, USB MAC 0xD8). `rfe_option` resolved per `rtw8814a_read_rfe_type`
  (bit7→USB=1, else raw). Wired into `driver.connect()` (read before
  phy_set_param) and `phy_set_param` now uses the **real** rfe_option, retiring
  the M3.b placeholder. De-map verified on synthetic 1-byte + 2-byte-header
  blocks; rfe resolution verified; 537 tests pass. **Awaiting HW gate** —
  `--phase efuse`: decode rfe/MAC/xtal, assert MAC non-garbage.
- **M3.c (channel tune, 20 MHz)** — ✅ CODE COMPLETE, offline-verified. `chan.py`
  ports rtw8814a_set_channel for 20 MHz: switch_band (rfe pinmux 2G/5G + CCK/TX/
  RX-psel + bw_reg adc/agc), switch_channel (per-path RF_CFGCH + CLKTRK fc_area
  + AGC sub-band), cck_tx_dfir, set_bw_mode. **Deferred** (not needed for 20 MHz
  monitor RX): bb_swing/pwrtrack (TX power → M6), adc_clk (A-cut only; no-op on
  our B-cut), spur_calibration (per-spur-channel NBI/CSI notch), 40/80 MHz.
  First tune uses `force_band=True` to establish rfe pinmux (we skip the
  kernel's init_rfe_reg in phy_set_param). Wired into `driver.set_channel` +
  initial ch1 tune in connect(). HW gate (`--phase channel`) = the tune SEQUENCE
  executes cleanly across 2G/5G/5G-high. **Functional validation is M5** (RF
  actually receiving on the tuned channel).
  - **[HW] These PHY/channel registers are write-and-forget — do NOT validate by
    readback.** Confirmed against the cold-boot pcap: the kernel NEVER reads back
    RF_CFGCH / CCK_CHECK / CLKTRK / AGC_TABLE (it writes them blind), and on
    hardware several don't read back as written (RF_CFGCH→const 0xEA;
    CCK_CHECK/CLKTRK/AGC→const 0x575/0xa/bit7 regardless of channel). RF *writes*
    do land (proven by the RF 0x1c RCK readback via the same write path).
  - **[WIRE] The three captures are init-only** (all end ~frame 5260, right after
    FW init + airmon start; identical 58 write-addrs, zero channel-reg writes).
    So there is NO pcap ground truth for channel tune OR RX — both must be
    validated live, not by pcap-diff. (Relevant for M5: RX desc decode comes from
    kernel rx.c + rx_common.py + the 8822bu sibling, not a pcap.)
- **M5 (RX / monitor)** — ✅ CODE COMPLETE, offline-verified. `rx.py`:
  `mac_init_for_rx` (RXFLTMAP + RX_DRVINFO_SZ + rxdesc-len quirk + promiscuous
  RCR_MONITOR 0xf410400f + USB burst) — the RX-side MAC init deferred from M2;
  `apply_monitor_rcr`; `iter_bulk_frames` over the shared 24-byte rx_pkt_desc
  decoder. Driver wires the shared `RxReaderThread` (reader-thread, not
  event-loop polling, per [[project_rx_loop_ui_starvation]]) + endpoint probe,
  stops it before USB release.
  ✅ **DONE 2026-05-26, HW-VERIFIED**: beacon captured + decoded end-to-end
  (bulk-IN → 24-B rx_desc → MPDU → parser → SSID), CR alive 0x4ff, RCR landed
  0xf410400f. Also confirms M3.c's tune on-air.
  - **[RESOLVED] Sensitivity** — was 1 beacon/9s (closest AP only); root cause
    was the CCK packet-detect threshold sitting at the insensitive table default
    (kernel tunes it via a dynamic watchdog we don't run). Pinned REG_CCK_PD_TH
    to LV0 + enabled 2R-CCA/MRC → 64 BSSIDs/9s. See `rx.tune_monitor_cck_sensitivity`.
  - **[OPEN] Intermittent cold-boot RX (~50%)** — binary per cold boot: either
    ~60 beacons or exactly 0 (never partial), independent of channel. Reads
    time out (chip delivers nothing), MAC is alive (CR=0x4ff) and RCR/sensitivity
    are correct in both cases. **Ruled out:** bulk-IN pipe (clear_halt+drain,
    `rx.prime_bulk_in`), reference clock (`crystal_cap` → AFE_CTRL3, now written).
    Binary per-boot ⇒ an init-time RF state that comes up good/bad each cold boot
    — normally pinned by calibration. The only calibration we skip is IQK
    (`rtw8814a_phy_calibration` → `do_iqk`, TX-IQ/LO-leakage; large port,
    uncertain for an RX symptom). Next step: diagnostic register-diff of a good
    vs bad boot to pinpoint, before committing to the IQK port.
  - **[follow-up] RSSI** still the -100 placeholder (needs rtw8814a_query_phy_status).
  - **[BUG fixed] test phase-gating** — `--phase rx` had skipped fw/validate/
    mac_init/efuse (missing from the `needs_*` sets), so the MAC was never
    powered → CR read 0xEA. Replaced with an ordered chain (run everything up to
    the target phase). NOT a driver bug — driver.connect() always ran M1→M5 in
    order; the EFUSE grant-off "fix" made on the wrong theory was reverted.
- **M6 (TX inject)** — not started. TX desc + deauth → handshake recapture.
- **M7 (monitor-mode / no-RX-filter verification)** — proves the card is truly
  promiscuous, not just passing broadcast + own-MAC. Gate: capture frames whose
  **addr1 (receiver) is a unicast MAC that is neither broadcast/multicast nor our
  own** — i.e. traffic addressed to OTHER stations. Beacons (addr1=broadcast)
  do NOT count. PASS if such frames appear. Cross-cutting concern (several cards
  silently filter): should generalise into a shared check across all drivers
  per [[project_driver_gap_audit]].

## 0. TL;DR for the lead

The single most important finding: **the 8814A's closest already-ported sibling
is the 8822BU, not the 8812AU.** They share `RTW_WCPU_3081` + the iDDMA segmented
firmware path. The legacy 8812au/8821au (8051 + MCUFWDL) path is the *wrong*
reference for the firmware/MAC bring-up.

Practical consequence:
- **Firmware + MAC bring-up** ≈ adapt `chips/rtl8822bu/` (firmware.py, mac.py).
  Mostly reuse, plus one new segment (EMEM, see §3).
- **PHY + RF** is where the real 4×4 delta lives: `rtw8814a_table.c` is **23,930
  lines** (8× the 8812a's 2,812). That's the bulk of the work, but it's
  *mechanical* — flat-u32 tables ported 1:1 per [[feedback_constants_from_source]].
- **`rtw88_base/rf_sipi.py` only knows path 'a'/'b' today** — 8814A needs C & D
  (it `raise`s ValueError otherwise). Concrete, small base-layer change.

---

## 1. Verified recon facts

### 1.1 Chip spec — `rtw8814a_hw_spec` (`rtw8814a.c:2180`)

| Field | Value | Note for us |
|---|---|---|
| `.wlan_cpu` | `RTW_WCPU_3081` | **RISC core, not 8051** → iDDMA FW path (same as 8822b/8821c/8822c) |
| `.fw_name` | `rtw88/rtw8814a_fw.bin` | single blob, **no WoWLAN fw** |
| `.tx_pkt_desc_sz` | 40 | TX desc 40 B (8822b is 48; 8812a is 40) — **verify against pcap** |
| `.rx_pkt_desc_sz` | 24 | matches `rtw88_base/rx_common.py` 24-B decoder |
| `.rx_buf_desc_sz` | 8 | |
| `.phy_efuse_size` | 1024 | EFUSE read (M4) |
| `.txff_size` | (2048-10) × `TX_PAGE_SIZE` | FIFO partition (M2) |
| `.rxff_size` | 23552 | |
| `.band` | 2G \| 5G | |
| `.max_power_index` | 0x3f | |
| `.sys_func_en` | 0xDC | |
| `.rf_base_addr` | `{0x2800, 0x2c00, 0x3800, 0x3c00}` | **4 paths** A/B/C/D |
| `.rf_sipi_addr` | `{0xc90, 0xe90, 0x1890, 0x1a90}` | **4 paths** A/B/C/D |
| `.rf_tbl` | `{rf_a, rf_b, rf_c, rf_d}` | 4 RF init tables |
| `.pwr_on_seq` | `card_enable_flow_8814a` | port table (M2) |
| `.pwr_off_seq` | `card_disable_flow_8814a` | warm-reattach only; **don't** replicate per [[feedback_warm_reattach]] |
| `.usb_tx_agg_desc_num` | 3 | `REG_AUTO_LLT_V1` config |

### 1.2 The WCPU_3081 / iDDMA firmware path

`mac.c:982 _rtw_download_firmware` branches on CPU type:
- `rtw_chip_wcpu_8051()` → `__rtw_download_firmware_legacy` (MCUFWDL — what 8812au uses)
- else (3081) → `__rtw_download_firmware` → `start_download_firmware`
  (`mac.c:697`) → segmented `download_firmware_to_mem` per segment via
  `iddma_download_firmware` (`mac.c:574`).

Firmware header (`validate_fw_hdr`, `mac.c:~410-438`) declares **three** segments:
`dmem_size`, `imem_size`, and **`emem_size`** (present iff `fw_hdr->mem_usage &
BIT(4)`). Each segment gets a 4-B checksum appended (`FW_HDR_CHKSUM_SIZE`).

`rtw_chip_wcpu_3081` also gates, in `mac.c`:
- `REG_H2CQ_CSR = BIT_H2CQ_FULL` after `MAC_TRX_ENABLE` (`mac.c:1125`)
- the modern `__priority_queue_cfg` (FIFOPAGE_INFO regs) vs legacy RQPN (`mac.c:1295`)
- reserved-page layout incl. H2CQ/CPU-instr/fw-txbuf pages (`mac.c:1166`)
- `init_h2c()` H2C ring setup (`mac.c:1301`, no-op on 8051)
- `rtw_drv_info_cfg` "rxdesc len = 0" `REG_TRXFF_BNDY+1 |= 0xF` quirk (`mac.c:1378`)

### 1.2.1 Port gotcha — FW-upload TX descriptor size [SRC]

The wire shows a **40-byte** TX descriptor per FW chunk (= `tx_pkt_desc_sz`),
which is what drives both `build_fw_tx_pkt_desc` and the iDDMA source offset
(`download_firmware_to_mem`, mac.c:650 `desc_size = chip->tx_pkt_desc_sz`).
BUT `send_firmware_pkt` (mac.c:550) computes its ZLP-avoidance `%512` decision
against the kernel's **hardcoded `#define TX_DESC_SIZE 48`** (mac.c:528), NOT the
chip's real descriptor size. For the 8822b these happen to be equal (48); for
the 8814a they differ. Our `firmware.py` keeps them separate: `TX_PKT_DESC_SZ`
(40) for the descriptor + iddma offset, `FW_DLFW_ZLP_TXDESC` (48) for the ZLP
check. For the current blob no chunk actually triggers the +1, but the split is
the faithful port and is robust if the FW changes.

### 1.3 Chip-ops surface — `rtw8814a_ops` (`rtw8814a.c:2050`)

Relevant to scan/monitor/inject (coex/BT ops omitted — not needed):

| op | 8814a fn | maps to |
|---|---|---|
| `power_on/off` | `rtw_power_on/off` (generic) | `rtw88_base/power_seq.py` + ported table |
| `phy_set_param` | `rtw8814a_phy_set_param` | BB/RF init — **M3**, the big one |
| `mac_init` | `rtw8814a_mac_init` | **M2** |
| `read_efuse` | `rtw8814a_read_efuse` | **M4** |
| `query_phy_status` | `rtw8814a_query_phy_status` | RSSI decode — **M5 (RX)** |
| `set_channel` | `rtw8814a_set_channel` | 4-path tune — **M3** |
| `read_rf/write_rf` | `rtw_phy_read_rf` / `..._sipi` | `rtw88_base/rf_sipi.py` **+ path C/D** |
| `set_tx_power_index` | `rtw8814a_set_tx_power_index` | **M6 (TX)** |
| `fill_txdesc_checksum`| `rtw8814a_fill_txdesc_checksum` | `rtw88_base/tx_common.py` — **verify XOR layout for 40-B desc** |

### 1.3.1 Firmware upload — VERIFIED [WIRE]

Extracted from capture-1 and byte-diffed against the linux-firmware blob
(`scripts/rtw88_8814au/extract_rtw8814a_fw.py`):

- **BYTE-FOR-BYTE MATCH**: pcap-reassembled FW == `rtw8814a_fw.bin[64:]`.
- Upload endpoint: **bulk-OUT EP 0x02** (out_ep[0]) — the only OUT pipe with
  >1 KB chunks during bring-up.
- TX descriptor on each chunk: **40 bytes** (confirms `.tx_pkt_desc_sz`).
- Segments: **DMEM 5784 B** (@0x80200000) then **IMEM 62456 B** (@0x80000000),
  each + 8-byte checksum on the wire. **No EMEM.**
- 18 chunks total, pcap frames **279..897** (inside the init window 1..5266).
- The 64-byte `rtw_fw_hdr` never appears on the wire — driver reads it to learn
  segment sizes/dst-addrs, then uploads bodies only.

### 1.4 Captures (ground truth)

`usb_dumps/captures_rtw88_8814au/` — 3 pcaps + `*_logs/main.log`.
- **capture-1 = cold boot.** `pcap_slicer` shows `<hardware_plugin_and_initialization>`
  = **frames 1–5266** (this is where FW upload lives). Channel sweep reaches
  ch165 → confirms dual-band 4×4.
- capture-2 = likely warm boot (kernel skipped FW load — usual pattern).
- capture-3 = second cold/attack capture.

> ⚠️ The `aireplay-ng.log` in these capture dirs contains **real BSSID/client
> MACs** from the capture environment. Per [[feedback_no_ssids_in_commits]],
> those never enter committed code, comments, or this doc. Placeholders only.

---

## 2. Reuse map — what we already have vs. net-new

| Concern | Source of truth | wifit3 reuse | Net-new for 8814a |
|---|---|---|---|
| Vendor control xfer | `rtw88_base/transport.py` | ✅ full | thin `transport.py` subclass |
| iDDMA segmented FW upload | `rtl8822bu/firmware.py` | ✅ DMEM+IMEM+H2CQ+checksum | **+ EMEM segment** (§3) |
| power_seq runtime | `rtw88_base/power_seq.py` | ✅ runtime | port `card_enable_flow_8814a` table |
| phy_cond walker | `rtw88_base/phy_cond.py` | ✅ (scalar-rfe) | confirm 8814a rfe encoding |
| RF SIPI read/write | `rtw88_base/rf_sipi.py` | ⚠️ path a/b only | **add path C/D** (addrs in §1.1) |
| RX desc decode | `rtw88_base/rx_common.py` | ✅ 24-B desc | confirm RSSI/phy-status offsets |
| TX checksum | `rtw88_base/tx_common.py` | ✅ XOR | **verify 40-B desc field layout** |
| MAC init (modern) | `rtl8822bu/mac.py` + `mac.c` | ✅ H2CQ/prioq/init_h2c | 8814a `mac_init` specifics |
| BB/AGC/RF tables | `rtw8814a_table.c` (24k lines) | — | **port 1:1** (M3, mechanical bulk) |
| 4-path channel tune | `rtw8814a_set_channel` | — | **net-new** (M3) |
| EFUSE | `rtl8822bu` efuse path | partial | 8814a `read_efuse` (M4) |

---

## 3. Known gaps & risks (audit before declaring any milestone done)

1. ~~**EMEM segment.**~~ **RESOLVED — no EMEM.** Parsed the real blob
   (`assets/rtw8814a_fw-linux_firmware.bin`): `mem_usage=0x08` (BIT(4) clear), so
   only **DMEM (5784 B @ 0x80200000) + IMEM (62456 B @ 0x80000000)** — the exact
   two-segment shape `rtl8822bu/firmware.py` already uploads. Signature `0x8814`,
   v33.6.0, computed size 68320 == file size. M1 is near-pure 8822bu reuse.
   Note: segment dst addresses come from the FW header fields, not hardcoded —
   confirm M1 reads `dmem_addr`/`imem_addr` from the blob (kernel does).
2. **bulkout_num.** `priority_queue_cfg` selects `page_table[2/3/4]` by USB bulk-OUT
   endpoint count. AWUS1900 likely exposes 4 bulk-OUT. **Confirm endpoint
   descriptors from the pcap / live device** before M2.
3. **rf_sipi path C/D.** Base layer raises on path != a/b. Must extend before any
   RF write on path C/D (M3). Keep path A/B behaviour byte-identical for the
   existing 8812au/8821au/8822bu drivers — this is a shared file.
4. **Always-monitor deviation** per [[feedback_monitor_mode_deviation]]: kernel
   inits for STA mode. After init, M5 must set RCR / RX_FILTR_CFG for promiscuous
   monitor and skip address-match. Audit explicitly; don't assume kernel defaults.
5. **RX loop on event loop** per [[project_rx_loop_ui_starvation]]: build the RX
   reader on the shared reader-thread helper (`chips/rx_reader.py`) from day one,
   not the polling pattern.
6. **40-B TX desc.** `tx_common.py` XOR checksum assumes a desc layout; 8814a is
   40 B. Verify field offsets vs `rtw8814a_fill_txdesc_checksum` before M6.
7. **USB speed** per [[feedback_usb_speed_check]]: AWUS1900 is USB-3 branded —
   probe `bcdUSB` + `wMaxPacketSize` before assuming anything about URB sizing.

---

## 4. Milestone plan

Each milestone ends with a **hardware-test gate**: agent prepares
`scripts/rtw88_8814au/test_hw_8814au.py`, user unplugs/replugs, runs it, the
script self-reports PASS/FAIL. A milestone is **DONE only when verified on
hardware** per the session loop in CLAUDE.md. Methodology: tiny first step,
pcap-diff every step before declaring done ([[feedback_bringup_methodology]],
[[feedback_port_completeness]]).

### M1 — Firmware upload + FW_READY ACK
*Goal: blob lands in the 3081 core and the chip reports ready. No PHY.*
~Demoable, mirrors 8822bu M1.

- **M1.a** Offline: linux-firmware blob is in `assets/rtw8814a_fw-linux_firmware.bin`
  (header parsed — DMEM+IMEM, no EMEM, see §3.1). Extract the blob from capture-1
  (mirror `scripts/rtl8812au/extract_rtw8812a_fw.py`) and byte-verify it matches
  the linux-firmware copy. Pin exact FW-upload frame sub-range via `pcap_slicer`.
- **M1.b** Scaffold `chips/rtw88_8814au/`: `driver.py` (`SUPPORTED_IDS`,
  `SUPPORTED_CHANNELS`, `from_usb_device`), `transport.py`, `constants.py`,
  `firmware.py`. Register in `wlan/manager.py:_all_drivers()`.
- **M1.c** Port power-on seq (`card_enable_flow_8814a`) + reg backup/restore.
- **M1.d** Adapt iDDMA upload (reuse 8822bu — DMEM+IMEM only, dst from header);
  H2CQ; `download_firmware_validate`.
- **🔌 HW gate:** `test_hw_8814au.py` uploads FW, polls `REG_MCUFW_CTRL`, prints
  PASS on FW_READY (`0xC078` per `mac.c:285`).

### M2 — MAC init + FIFO/queue config
*Goal: TRX engine alive, LLT init OK, queues mapped. Still no PHY/RX.*

- **M2.a** Confirm `bulkout_num` from device descriptors → page_table/rqpn index.
- **M2.b** Port `rtw8814a_mac_init` + modern `__priority_queue_cfg` + `init_h2c`
  + `rtw_drv_info_cfg` (incl. rxdesc-len quirk).
- **M2.c** FIFO partition (`rtw_set_trx_fifo_info`, 3081 reserved-page layout).
- **🔌 HW gate:** script runs M1→M2, asserts `REG_AUTO_LLT_V1` auto-init
  completes + `check_hw_ready` passes; no error path hit.

### M3 — PHY/BB/RF init + channel tune (the 4×4 bulk)
*Goal: PHY parameterised, all 4 RF paths up, can tune to a channel.*
Split into three independently HW-gated sub-milestones (lead's call — stop &
verify often to avoid churn on the largest milestone).

- **M3.a — tables.** Port `rtw8814a_table.c` (mac/agc/bb/rf_a/b/c/d) 1:1 →
  `*_tbl.py`. Also extend `rtw88_base/rf_sipi.py` for path C/D (additive; a/b
  byte-identical — no impact to other rtw88 drivers).
  - **🔌 HW gate:** replay MAC+BB tables, read back a sample of written regs,
    assert they stuck (table-replay smoke test; no RF/channel yet).
- **M3.b — PHY param.** Port `rtw8814a_phy_set_param` (phy_cond walker + table
  replay + 4-path RF table load via SIPI).
  - **🔌 HW gate:** run full phy_set_param, read back RF regs on all 4 paths,
    assert non-garbage / expected init values.
- **M3.c — channel.** Port `rtw8814a_set_channel` (4-path tune).
  - **🔌 HW gate:** tune ch1 / ch36 / ch149, read back channel regs per path.

### M4 — EFUSE read
*Goal: read rfe_option / power-by-rate / chip cuts; feed PHY init.*

- **M4.a** Port `rtw8814a_efuse_grant` + `rtw8814a_read_efuse` (1024-B phy efuse).
- **M4.b** Wire EFUSE values into M3 init (rfe_defs selection).
- **🔌 HW gate:** dump decoded EFUSE, sanity-check rfe_option/MAC addr non-garbage.

### M5 — RX (sniff) + monitor mode
*Goal: live frames in the TUI scanner.*

- **M5.a** RX desc decode via `rx_common.py`; confirm RSSI/phy-status offsets
  against `rtw8814a_query_phy_status`.
- **M5.b** Monitor-mode filter rewrites (RCR/RX_FILTR_CFG) per gap #4.
- **M5.c** RX reader-thread (shared helper) per gap #5.
- **🔌 HW gate:** scan ch1/ch6, assert N beacons / M BSSIDs in a fixed window
  (the 8821au "27 BSSIDs/8s" style check), pcap-diff vs airmon capture.

### M6 — TX inject (deauth) → full attack stack
*Goal: deauth recaptures a handshake live — the "DONE" bar for every chip.*

- **M6.a** Port TX desc build + verify 40-B XOR checksum (gap #6).
- **M6.b** `set_tx_power_index` (EFUSE-sourced, regulatory) + arm TX queues.
- **M6.c** Inject deauth on EP for mgmt; confirm on-air with a 2nd known-good card.
- **🔌 HW gate:** deauth a test client, recapture EAPOL M1+M3 / PMKID live.

After M6: drop the row into `NEXT-STEPS.md` supported-hardware table, mark DONE
with date.

---

## 5. Decisions (lead, resolved)

1. ~~**rf_sipi path C/D** → extend `rtw88_base/rf_sipi.py` in place.~~
   **REVERSED in M3.a → chip-local `rf.py` instead.** The "extend in place" call
   assumed 8814a reads RF like its 8812au/8821au cousins. It doesn't:
   `rtw8814a_ops.read_rf = rtw_phy_read_rf` is a **direct MMIO read**
   (`read32(rf_base_addr[path] + addr*4)`), same as the 8822b — NOT the 3-wire
   HSSI/PI/SI read that `rf_sipi.py` implements for the 8812a/8821a. So 8814a
   gets its own `rf.py` (direct read + sipi write, 4 paths); the shared file is
   untouched (lower risk than the original plan). `.write_rf` is the shared
   `rtw_phy_write_rf_reg_sipi` semantics, just indexed by `rf_sipi_addr[path]`.
   [Decision confirmed with lead during M3.a.]
2. **EMEM** → **none.** Confirmed by parsing the real blob (§3.1). M1 = DMEM+IMEM
   only, near-pure 8822bu reuse.
3. **M3 granularity** → **split into M3.a/.b/.c** (tables / phy / channel), each
   with its own HW gate. Stop-and-verify per sub-step to minimise churn.

---

## 6. Citations index

- `data_dumps/rtw88-source-v6.18/rtw8814a.c` — chip ops, hw_spec
- `.../rtw8814a_table.c` — BB/AGC/RF init tables (23,930 lines)
- `.../rtw8814au.c` — USB ID table (lead VID:PID `0x0bda:0x8813`)
- `.../mac.c` — iDDMA FW download (697), wcpu_3081 gates (281/1125/1166/1378)
- `.../main.h:1181` — `enum rtw_wlan_cpu` (3081 vs 8051)
- wifit3 `chips/rtl8822bu/firmware.py` — iDDMA reuse reference
- wifit3 `chips/rtw88_base/{rf_sipi,rx_common,tx_common,power_seq}.py`
