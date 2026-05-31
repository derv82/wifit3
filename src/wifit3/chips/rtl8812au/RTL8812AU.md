# RTL8812AU — verified facts (M1 scope)

## Known limits & resolved gaps

### Resolved — HW-verified on the AWUS036ACH (2026-05-31)

- [x] **RX polling loop** — reader-thread. On-loop read+parse moved to the
  shared `chips/rx_reader.py` RxReaderThread (`_rx_read_once`/`_rx_dispatch`).
  Confirmed: scan + capture run for minutes under TUI load.
- [x] **RX filter / monitor mode (ToDS capture)** — rtw88xxa init left REG_RCR
  byte0 `0x0E` (AAP clear) → ToDS dropped (only M1/M3). `apply_monitor_rx_filter`
  writes the monitor `0xf410400f` from `_finish_attach` (both paths). [WIRE
  captures_rtw88_8812au frames 6891-6901]. Confirmed: M2/M4 captured live.
- [x] **RX-DMA aggregation left un-armed** — the FW default page accumulator
  wedged bulk-IN after ~5 s of traffic (clean cliff, control plane alive).
  `configure_rx_aggregation` ports `rtw_usb_dynamic_rx_agg_v2` (monitor/disable
  values) and arms it once at attach. [WIRE captures_rtw88_8812au capture-1
  frame 7649]. Confirmed: the consistent 5 s cliff is gone.
- [x] **SIPI PI-mode bit read from wrong reg on path B** — `rf_sipi.read_rf`
  took the PI/SI mode-select bit from `REG_3WIRE_SWA` for both paths; the kernel
  reads it from `REG_3WIRE_SWB` for path B (`rtw88xxa.c:1248`), which picks the
  correct read-back register. Wrong-reg reads corrupted masked RF
  read-modify-writes on path B (2T2R only; 8821a is path-A-only). `[SRC]`

### Known hardware limit — RF-synth hop-death

Sustained dual-band channel hopping eventually wedges the RX path. This is an
**rtw88-inherited hardware limit, not a userland bug** — the in-tree driver has
it too.

- **Reproduction**: single channel survives 30 min+; dual-band 0.25 s hopping
  dies after seconds-to-minutes (non-deterministic). `[HW]`
- **Signature**: bulk-IN goes silent while the control plane stays alive — RF18
  reads back the correct channel, false-alarm/CRC counters read 0. Points at the
  RF synthesizer (VCO/PLL) losing lock under thermal drift, not the MAC/DMA.
- **Why the kernel doesn't hit it as hard**: `rtw8812a_do_lck` (VCO LC
  re-calibration) is gated behind `rtw_phy_pwrtrack_need_iqk` — a thermal drift
  of ≥8 from the efuse cal baseline (≈37). Hopping keeps the chip ~32–42, so the
  drift gate never trips and the VCO is never re-centered.
- **Mitigation shipped** (`dynamic.py`): DIG watchdog + thermal pwr-track + LCK,
  with `do_lck` **decoupled from `need_iqk`** — run on a fixed cadence and at
  each band entry. Re-centering the VCO delays the wedge (~2–4×) but does not
  eliminate it.
- **No userland recovery**: BB reset and full PHY re-init were both tested and
  fail to revive the pipe. The driver logs one warning and the user replugs.
  Sustained 0.25 s dual-band hopping is at this chip's hardware envelope.

### Attack-stack hardware results (2026-05-31)

Full attack pass on the AWUS036ACH (matrix cells in `VERIFICATION.md`):

- **Deauth** ✅ multiple clients. **Handshake** ✅ — M1+M2 and M2+M3 captured.
  **PMKID** ✅ — passive + active extract. **WPS** ✅ — PIN and PBC.
- **WEP** — Replay ✅; ChopChop ✅ (<2 min); **Fragmentation ✗** — could not forge
  a valid ARP after ~2 min of rounds.

**Fragmentation lead (cross-family pattern).** Frag forging fails here, on the
RTL8822BU, and on the AR9271 — three different chip families — and works only on
the RTL8821AU (its dev card). Because it spans families, the leading suspect is
now the **shared frag daemon** (seed-selection-by-length / oracle matching / the
sw-seq it assumes), not this chip's TX desc alone. Full analysis + the
disambiguating probe live in `engine/attacks/wep/README.md` § "Known issue —
Fragmentation works only on the RTL8821AU." Replay + ChopChop (single-frame) work
here, consistent with a fragment-sequence-or-seed-specific gap.

**Scan/UI note.** When channel-hop wedges the RX (the hop-death above), the
Scanner gives no feedback — targets fade to dark then the list empties, no
banner. The driver logs the warning; surfacing it in the UI is tracked in
`NEXT-STEPS.md` § Small bugs / QoL.

Cleanroom-RE'd from `data_dumps/rtw88-source-v6.18/` + cold-boot pcap
`usb_dumps/captures_rtw88_8812au/capture-1.pcap`. Every claim here cites
either `[SRC]` (kernel source) or `[WIRE]` (pcap observation). Anything
not below is a hypothesis.

## Device identity

- **VID:PID**: `0BDA:8812` (first entry of `rtw_8812au_id_table` in
  `rtw8812au.c:11`). 30 other PIDs claim the same chip; we add them as
  users surface them. `[SRC]`
- **USB speed on the AWUS036ACH**: **HighSpeed (USB 2.0)**, NOT
  SuperSpeed despite the box. `[WIRE]` (probed locally 2026-05-17):
  - `bcdUSB = 0x0200`
  - bulk `wMaxPacketSize = 512` (HS max; SS would be 1024)
  - no SuperSpeed Endpoint Companion descriptors
  - PyUSB `dev.speed = 3` (= `LIBUSB_SPEED_HIGH`, NOT `LIBUSB_SPEED_SUPER` = 4)
- **Endpoints** (Interface 0, vendor-specific): bulk-IN `0x81`,
  bulk-OUT `0x02`/`0x03`/`0x04`, interrupt-IN `0x85` (64B @ bInterval=1).
  All bulk EPs `wMaxPacketSize = 512`. `[WIRE]`

The rtw88 driver has `rtw_usb_switch_mode` to opt INTO USB 3.0, but it's
not called on default cold boots, so the card stays on HS. We never call
it either, side-stepping all WinUSB SuperSpeed concerns (NRDY/ERDY,
RAW_IO, etc.).

## Architecture overview

- **rtw88 88xxA family**, sibling to RTL8821AU. Shares `rtw88xxa_power_on`,
  the legacy MCUFWDL FW path, the bitfield-rfe `phy_cond` walker, and the
  88xxA SIPI / power_seq runtime with 8821au. `[SRC] rtw88xxa.c`
- **2T2R** (vs 8821au's 1T1R). `rf_sipi_addr = {REG_LSSI_WRITE_A,
  REG_LSSI_WRITE_B}` in `rtw8812a_hw_spec`. M2+ work needs to write RF18
  on both paths (mirror 8822bu's pattern). `[SRC] rtw8812a.c:1071`
- **Bands**: 2.4 GHz + 5 GHz (`band = RTW_BAND_2G | RTW_BAND_5G`). `[SRC] rtw8812a.c:1057`
- **wlan CPU = 8051** (`wlan_cpu = RTW_WCPU_8051`), so FW upload uses
  the **legacy MCUFWDL** path (same as 8821au), NOT the modern iDDMA
  path used by 8822b/c/8814a. `[SRC] rtw8812a.c:1042`
- **TX desc**: `tx_pkt_desc_sz = 40` (same as 8821au, NOT 48 like 8822bu).
  `rx_pkt_desc_sz = 24` (same as the rest of rtw88). `[SRC] rtw8812a.c:1043`
- **FIFO**: `txff_size = 131072`, `page_size = 512`, `rsvd_drv_pg_num = 9`
  → `rsvd_boundary = 247` (vs 8821au's 248). `[SRC] rtw8812a.c:1050`

## M1: cold-boot through FW_READY_LEGACY

### Bring-up sequence (verified against `rtw88xxa_power_on` ordering)

1. **8812a-specific RF reset** (`rtw88xxa.c:1036..1041`). 4 byte-writes,
   both RF paths out of reset:
   - `write8(REG_RF_CTRL=0x1F, 5)`
   - `write8(REG_RF_CTRL=0x1F, 7)`
   - `write8(REG_RF_B_CTRL=0x76, 5)`
   - `write8(REG_RF_B_CTRL=0x76, 7)`
2. **mac_pre_system_cfg** — clear `REG_RSV_CTRL`, pick LDO_SEL vs SPS_SEL
   based on `REG_SYS_CFG1` bit 24. Same as 8821au. `[SRC] mac.c:62`
3. **`card_enable_flow_8812a`** — `CARDDIS_TO_CARDEMU` (13 entries) +
   `CARDEMU_TO_ACT` (9 entries). Ported 1:1 from
   `rtw8812a_table.c:2259..2373`. Filter by `INTF_USB`. `[SRC]`
4. **mac_init_system_cfg_legacy** — `REG_CR=0xFF` → 2ms → `REG_HWSEQ_CTRL=0x7F`
   → 2ms → `REG_SYS_CLKR |= BIT_WAKEPAD_EN` → `REG_GPIO_MUXCFG &= ~BIT_EN_SIC`
   → `REG_CR=0x02FF`. Same as 8821au. `[SRC] mac.c:355`
5. **pre_fw_init** — `set_trx_fifo_info` (pure Python; returns
   `rsvd_boundary=247`) → **`llt_init(247)`** (256 LLT writes) →
   `REG_TXDMA_OFFSET_CHK |= BIT_DROP_DATA_EN`. `[SRC] rtw88xxa.c:1055..1067`
6. **`en_download_firmware_legacy(True)`** — reset 8051, set `BIT_MCUFWDL_EN`,
   clear `BIT_ROM_DLEN` in `REG_MCUFW_CTRL`. `[SRC] mac.c:835`
7. **`download_firmware_legacy(fw_bytes)`** — strip 32B legacy header,
   upload each 4096B page via 196/8/1 byte control-OUT chunks to
   wValue=0x1000+offset. Pre-arm `BIT_FWDL_CHK_RPT`, then poll it for
   up to 1s after the last byte. `[SRC] mac.c:892 + usb.c:168`
8. **`en_download_firmware_legacy(False)`** — clear `BIT_MCUFWDL_EN`.
9. **`download_firmware_validate_legacy`** — set `BIT_MCUFWDL_RDY`,
   clear `BIT_WINTINI_RDY`, toggle wlan_cpu off/on, poll
   `REG_MCUFW_CTRL & FW_READY_LEGACY (0xC6) == 0xC6`. Pass = M1 done.
   `[SRC] mac.c:924`

### Firmware blob

- **Asset path**: `src/wifit3/chips/rtl8812au/assets/rtw8812a_fw.bin` =
  canonical `linux-firmware/rtw88/rtw8812a_fw.bin` (27030 bytes total =
  32B `rtw_fw_hdr_legacy` + 26998B body).
- **Full-blob SHA-256**: `abdcca4e8bf76ebfba23d433de310ffefebd0ff9d01990639d4cd9602b32b71a`
- **Body SHA-256** (`bytes[32:]`): `41f2dd4f3ab132ffaebfc6dfe50a9bcd74807e5fcab7bdf511e4bcb31ad90cc8`
- **Byte-verify vs Kali pcap** ✓ confirmed 2026-05-17. The body bytes in
  the linux-firmware blob are byte-for-byte identical to the
  pcap-extracted body from
  `usb_dumps/captures_rtw88_8812au/capture-1.pcap` frames 201..729
  (control-OUT, `bRequest=0x05`, `wValue in [0x1000, 0x1FFF]`,
  concatenated in pcap order).
- **Wire layout** = 7 pages: 6 × 4096B full pages + 1 × 2422B tail.
- **Re-extraction**: `python scripts/rtl8812au/extract_rtw8812a_fw.py` reproduces
  the body from the pcap (writes body-only file; not used by the runtime
  loader, which now reads the canonical linux-firmware blob directly).

### Pcap frame map (capture-1, cold boot)

`[WIRE]` from `scripts/rtl8812au/extract_rtw8812a_fw.py`:

| page | start frame | body offset | size |
|-----:|------------:|------------:|-----:|
| 0    | 201         | 0x000000    | 4096 |
| 1    | 289         | 0x001000    | 4096 |
| 2    | 377         | 0x002000    | 4096 |
| 3    | 465         | 0x003000    | 4096 |
| 4    | 553         | 0x004000    | 4096 |
| 5    | 641         | 0x005000    | 4096 |
| 6    | 729         | 0x006000    | 2422 |

Cold-boot phase = frames 1..3144 per `scripts/pcap_slicer.py`.

## M5: warm reattach (FW-warm tier)

Landed 2026-05-17 alongside M1.

**`is_chip_warm(transport)`** in `mac.py` returns True if
`(REG_MCUFW_CTRL & FW_READY_LEGACY) == FW_READY_LEGACY` (= 0xC6) on a
fresh USB claim. That's the exact bit pattern
`download_firmware_validate_legacy` leaves behind after M1, so reading
it back tells us "FW is still running from a previous session".

`RTL8812AUDriver.connect()` now branches:

- **Cold path** (`is_chip_warm` returns False): runs the full M1
  `_cold_bring_up` — `mac_power_on → pre_fw_init → download_firmware →
  validate`.
- **Warm path** (`is_chip_warm` returns True): logs the state, marks
  `is_warm=True`, returns immediately. No power_seq, no FW upload, no
  CPU reset.

Test harness behaviour:

- `phase_open` prints `REG_MCUFW_CTRL` + the warm/cold verdict.
- `phase_fw` short-circuits to a one-line ACK on a warm chip; the
  cold path lives in `_phase_fw_cold`.
- `phase_validate` is idempotent — runs the CPU reset + FW_READY poll on
  warm or cold (handy for "are you SURE this FW is still healthy?").

**Tier scope today**: only "FW-warm". The full "MAC-warm" tier (also
ANDing in `BIT_MACTXEN|BIT_MACRXEN` of REG_CR) will land with M2, once
the post-FW MAC init actually sets those bits. Until then, even after a
warm reattach the chip has no MAC TX/RX enabled — so M5 alone doesn't
let us RX or TX; it just removes the FW-upload tax from the dev loop.

**If a warm reattach misbehaves** (e.g. control transfers stop
responding): unplug, wait a few seconds, replug, rerun. Per
`[[feedback_warm_reattach]]`, we do NOT try to run `pwr_off_seq` from
userland — empirically it leaves WinUSB pipes wedged.

## M2+M3 bundle: MAC init → PHY init → channel → RX beacons

Landed 2026-05-17. Built atop M1 + M5 in a single bundled milestone per
"slice looser" guidance. Demoable end-state: `--phase beacon` sees Wi-Fi
networks on channel 1.

### Files added/touched

| File | Purpose |
|---|---|
| `scripts/rtl8812au/extract_init_tables.py` | Parses `rtw8812a_table.c` → 5 asset modules. |
| `assets/{mac,agc,bb,rf_a,rf_b}_tbl.py` | Auto-generated flat u32 tables (224..864 u32s each). |
| `mac.py` (+= `post_fw_mac_init` + 9 helpers) | REG_CR enables, queue tables, EDCA, beacons, ARFB. |
| `mac.py` (`ChipState`, `probe_chip_state`) | M2-c: 3-tier warm-state (COLD / FW_WARM / FULLY_WARM). |
| `phy.py` (new) | BB/RF table loaders + `switch_band_2g_20mhz` + `post_mac_init_phy`. |
| `chan.py` (new) | `set_channel_2g_20mhz` for 2T2R (RF18 writes on both paths). |
| `rx.py` (new) | Jaguar phy_status RSSI parser + rx_common re-exports. |
| `driver.py` (rewritten) | Full bring-up: cold + FW-warm + fully-warm + RX loop. |
| `scripts/rtl8812au/test_hw_rtl8812au.py` | + `--phase channel`, `--phase beacon`. |

### M2 deltas vs 8821a worth noting

| Topic | 8821a (1T1R) | **8812a (2T2R)** |
|---|---|---|
| FIFO `rsvd_drv_pg_num` | 8 | **9** |
| Page table index | `[2]` (2 bulkout) | **`[3]` (3 bulkout)** |
| `REG_PBP` write | — | **0x30 = PBP_512(TX) \| PBP_64(RX)** |
| `REG_AMPDU_MAX_TIME` | `0x5E` | **`0x70`** |
| `REG_FAST_EDCA_CTRL` | `0x03087777` | **(not written)** |
| `REG_FWHW_TXQ_CTRL` post | set to `0x80` | **clear BIT(7)** |
| `tx_aggregation` | writes DWBCN0 *and* DWBCN1 | **DWBCN0 only** |
| `usb_tx_agg_desc_num` | `6` | **`1`** |

### M3 deltas

| Topic | 8821a (1T1R) | **8812a (2T2R)** |
|---|---|---|
| Pre-pwr_seq | — | **RF reset both paths (REG_RF_CTRL=5,7; REG_RF_B_CTRL=5,7)** |
| `phy_bb_config` crystal mask | `0x00FFF000` | **`0x7FF80000`** |
| `phy_rf_config` | load `rf_a` only | **load `rf_a` + `rf_b`** |
| `switch_band(2G)` extras | `set_ext_band_switch_2g` (8821a-only) | **`BWINDICATION=1`, `PDMFTH 17:13=0x17`, `PDMFTH 3:1=0x04`, `CCASEL=0`** |
| RFE pinmux helper | `_phy_set_rfe_reg_24g` (8821a-version) | **`_phy_set_rfe_reg_24g_8812a` (rfe-option 0 path)** |
| `_switch_channel` | path A only | **iterate paths A+B, with CCA-frozen RF18 writes + `_phy_fix_spur` between rf_mod_ag and channel writes** |
| `post_set_bw_mode_20mhz` `L1PKTH` | `8` (1T1R) | **`7` (2T2R)** |
| `_set_channel_rf_20mhz` | path A only | **paths A+B** |

### Warm-state tiers (M5 + M2-c)

| State | REG_MCUFW_CTRL ∋ FW_READY | REG_CR ∋ MACTXEN+MACRXEN | Driver action |
|---|---|---|---|
| `COLD` | no | no | Full bring-up (M1+M2-b+M2-d+M3-a). |
| `FW_WARM` | yes | no | Skip M1; run M2-b + M2-d + M3-a. |
| `FULLY_WARM` | yes | yes | Skip everything; reattach + RX. |

`_finish_attach` runs a 1.5 s bulk-IN smoke test on the fully-warm path
and surfaces "please replug" if the pipe is wedged (rare on Windows
where WinUSB can't always recover pipe state from a previous session).

### Test harness phase chain (after M3-b)

```
open  →  fw  →  validate  →  mac_init  →  phy  →  channel  →  beacon
                                                              ^ pass = 1+ BSSIDs
```

Each phase prints its pass-line. Re-running the harness on a warm chip
auto-skips the upload + validate work — saves ~1 s per iteration during
debug.

## M4: TX inject (deauth) — PASSED 2026-05-17

Demo-verified: 10/10 deauth bulk-OUT bursts sent to NETGEAR2G, target client
(user's phone) reconnected — confirmed by re-capturing the 4-way EAPOL
handshake on the same channel after the deauth burst.

### Files

| File | Purpose |
|---|---|
| `tx.py` (new) | 40-byte tx_pkt_desc builder (W7 XOR checksum), MGMT-queue qsel encoding, queue→bulk-OUT EP mapper for 3-bulkout AWUS036ACH, `build_deauth_frame` helper. |
| `driver.py` (updated) | `inject_frame` + `_arm_tx_queues` workaround (see below). |
| `scripts/rtl8812au/test_hw_rtl8812au.py` | `--phase tx` + `--target-bssid`/`--target-client`/`--tx-count` CLI flags. |

### Per-chip wire layout

- `tx_pkt_desc_sz = 40` (same as 8821a, *not* 48 like 8822bu).
- `old_datarate_fb_limit = True` (`rtw8812a.c:1083`) → W4 `DATARATE_FB_LIMIT = 0x1F`.
- TX checksum: XOR of 16 u16 words into W7[15:0] (family-shared via
  `rtw88_base.tx_common.fill_txdesc_checksum`).
- Deauth frame is 26B (FC=0xC000, dur=0, RA=client, TA=BSSID, BSSID, seq=0,
  reason=7). Total bulk-OUT payload = 40B desc + 26B MPDU = 66B.

### Endpoint mapping (3-bulkout AWUS036ACH)

`bulk_out = [0x02, 0x03, 0x04]` in descriptor order. Per
`rqpn_table_8812a[3]`:

| Queue | DMA lane | EP index | EP addr |
|---|---|---|---|
| BEACON/HIGH | HIGH | 0 | 0x02 |
| MGMT | NORMAL | 1 | **0x03** |
| BK/BE | LOW | 2 | 0x04 |

### `_arm_tx_queues` quirk

⚠ Empirically discovered 2026-05-17: on 8812au, REG_RQPN / REG_RQPN_NPQ
/ REG_TXDMA_PQ_MAP all read back as 0 between `post_fw_mac_init`
(which writes them) and a subsequent TX attempt, *even though* nothing
in our code path writes 0 to them. The bulk-OUT then NAKs every MGMT
frame indefinitely (USB ETIMEDOUT after 200ms × 10 frames).

Workaround: re-arm by re-running `init_queue_reserved_page +
init_tx_buffer_boundary + init_queue_priority` right before injecting.
Costs ~3 control writes per inject burst, idempotent.

After re-arm:
- `REG_RQPN = 0x00D61010` (BIT_LD_RQPN bit 31 auto-clears, bits 0..23
  persist: pubq=0xD6=214, lq=0x10=16, hq=0x10=16)
- `REG_TXDMA_PQ_MAP = 0xE5F0` (VOQ→HIGH, VIQ→HIGH, BEQ→LOW, BKQ→LOW,
  MGQ→NORMAL, HIQ→HIGH)

Suspects for what clears the queue state mid-bring-up:
1. **2T2R rf_b table load** — only 8812au does this; 8821au is 1T1R.
2. **8812a-specific phy_bb_config writes** (RF_B_CTRL reset, AFE_CTRL3
   crystal_cap mask 0x7FF80000 vs 8821a's 0x00FFF000).
3. Some other side-effect of the 4 extra inline pokes in 8812a's
   `post_mac_init_phy` that 8821a doesn't have.

Not pinned down yet. M-LATER: bisect which write clears the queue
state, fix at source so the workaround can come out.

## MX bundle (post-M4 polish) — 2026-05-17

### MX-a: queue regs are write-only on 8812au (explained)

Bisect output (2026-05-17 hw test):

```
[Q-bisect] pre-post_mac_init_phy            RQPN=0x00000000  PQ_MAP=0x0000
[Q-bisect] post mac_tbl                     RQPN=0x00000000  PQ_MAP=0x0000
[Q-bisect] post phy_bb_config               RQPN=0x00000000  PQ_MAP=0x0000
[Q-bisect] post phy_rf_config               RQPN=0x00000000  PQ_MAP=0x0000
[Q-bisect] post switch_band_2g_20mhz        RQPN=0x00000000  PQ_MAP=0x0000
[Q-bisect] post inline-pokes                RQPN=0x00000000  PQ_MAP=0x0000
[Q-bisect] post drv_info_cfg                RQPN=0x00000000  PQ_MAP=0x0000
```

All zeros from the very first checkpoint — meaning the regs read back
as 0 EVEN BEFORE `post_mac_init_phy` runs. Best explanation:
`REG_RQPN`, `REG_RQPN_NPQ`, `REG_TXDMA_PQ_MAP` are **write-only "load"
registers** on this chip. Writes latch the queue config into internal
hardware state; readback always returns 0. The chip needs the
`BIT_LD_RQPN` "commit" gesture **close to TX time** — without a fresh
commit before bulk-OUT, MGMT queue NAKs every frame.

`_arm_tx_queues` in `driver.py` re-issues the three writes. **2026-05-30:**
moved from before-every-`inject_frame` to ONCE at `_finish_attach` (the shared
cold+warm tail). Hw-confirmed on a cold boot — one commit at attach survives to
TX-time, 10/10 deauths went out, so the per-frame re-arm was redundant. This is
hardware behavior (write-only load regs), not a code bug — nothing to "fix at
source"; the bisect is closed.

### MX-b: rate-aware CCK RSSI parser ✓

`parse_jaguar_phy_status_rssi` (in `rx.py`) now branches on rate per
`rtw88xxa.c:1518` `rtw88xxa_query_phy_status`:

- **CCK** (DESC_RATE0..3): extract `lna_idx` (w1[15:13]) + `vga_idx`
  (w1[12:8]), feed through `rtw8812a_cck_rx_pwr` lookup (8 LNA branches,
  each with its own VGA-based slope).
- **OFDM / HT / VHT**: extract `gain_a` (w0[6:0]) + `gain_b` (w0[14:8]),
  formula `rssi = gain - 110`, take `max` across active paths (2T2R).

Also threaded `RxPktStat` through `rx_common.iter_bulk_frames`'s
`phy_status_rssi` callback so future chips can also rate-branch.
8821au's own `iter_bulk_frames` is unaffected (kept its OFDM-only
parser; rarely shows noisy RSSI in practice on the slower card).

### MX-c: EFUSE read ✓ (hw-verified 2026-05-30)

Verified on the AWUS036ACH cold boot: read took 131 ms, rfe_option resolved
to 3 (IFEM-ext, the card's real routing), ext_lna/pa_2g+5g all 1, crystal_cap
0x0e, real MAC — and the values feed phy/RF config so the beacon sweep now
surfaces multiple BSSIDs incl. weaker ones (the earlier "only the nearest AP"
sensitivity gap is closed).

New `efuse.py` ports `rtw88xxa_read_efuse` + `rtw8812a_read_amplifier_type`
+ `rtw8812a_read_rfe_type`. Pipeline:

```
_efuse_grant(on)              # REG_EFUSE_ACCESS + SYS_FUNC_EN + SYS_CLKR
_switch_efuse_bank_wifi
dump_physical_efuse_map       # 512 reads via REG_EFUSE_CTRL[BITS_EF_ADDR]
parse_logical_efuse_map       # word-header walker → byte-addressable map
extract @ {0xB9 xtal_k, 0xBC pa_type, 0xBD lna_2g, 0xBF lna_5g,
           0xC1 rf_board_option, 0xCA rfe_option_raw, 0xD0 mac_addr}
_classify_amplifier           # derive ext_pa_2g, ext_lna_2g, etc.
_resolve_rfe_option           # final rfe_option per kernel logic
_efuse_grant(off)
```

Driver's `connect()` now reads EFUSE after `_claim` and replaces
`self._efuse` (was `EfuseDefaults()`) with the real values. Falls back
to defaults if the read times out, with a warning. The MAC address is
also populated from the EFUSE.

Harness: `--phase efuse` runs the read in isolation + prints all raw +
derived values. `--phase all` uses EFUSE values for phy init unless
`--rfe / --ext-lna / --ext-pa` CLI overrides are given.

Confirmed on hardware (see the header note): rfe_option reads back 3 on the
AWUS036ACH and feeds phy_bb_config + switch_band. EfuseDefaults() stays as the
cold-boot fallback if the read ever times out.

### MX-d: 5 GHz support ✓ (awaiting hw test on ch36+)

`phy.py`:
- `_phy_set_rfe_reg_5g_8812a` — all 6 rfe_option cases per
  `rtw8812a_phy_set_rfe_reg_5g` (rtw88xxa.c:874).
- `_poll_txpkt_empty` — 2.5ms poll for HI+MGT queues drained before
  band switch.
- `switch_band_5g_20mhz` — 8812A else-branch of `rtw88xxa_switch_band`
  (CCK_CHECK set, RXPSEL reset, BWINDICATION=2, PDMFTH 17:13=0x15 /
  3:1=0x04, CCASEL=1, rfe_reg_5g, TXPSEL=0, CCK_RX=0xF, basic_rates =
  OFDM 6/12/24M only).

`chan.py`:
- `set_channel_5g_20mhz(channel)` — wraps `_switch_channel` +
  `_post_set_bw_mode_20mhz` + `_set_channel_rf_20mhz` (all of which
  already understand 5G channel ranges via the fc_area / rf_mod_ag
  lookups).

Driver:
- `SUPPORTED_CHANNELS = list(range(1, 14)) + list(CHANNELS_5G_NON_DFS)`
  (= 22 channels: 2.4 GHz 1..13 + non-DFS 5 GHz 36/40/44/48/149/153/157/161/165).
- `set_channel` detects band crossing, calls `switch_band_*_20mhz`
  before `set_channel_*_20mhz`. `current_band_is_2g` tracks state.

DFS channels (52..144) excluded from `SUPPORTED_CHANNELS` since
wifit3 doesn't implement DFS clearance, but `set_channel` will accept
them if explicitly requested (channels are in `CHANNELS_5G_ALL`).

## What M1..M4 + MX does NOT do

Out of scope, will be M2+:

- **Post-FW MAC init** (`rtw88xxa_power_on` lines 1083..1175). REG_CR
  MAC_TXEN/RXEN, queue tables, EDCA, beacon params, AMPDU. Needed for
  RX/TX to actually deliver frames.
- **PHY init** — mac/agc/bb/rf_a/rf_b tables (~5000 register writes via
  `rtw_parse_tbl_phy_cond` family walker, which already exists in
  `rtw88_base/phy_cond.py` with the bitfield-rfe `chip_id` we use).
- **2T2R RF path B**. SIPI primitives in `rtw88_base/rf_sipi.py` are
  already path-parameterised. Apply to all RF18 / channel-tune writes
  in M6.
- **Channel tune**, RX descriptor decode, TX descriptor build, warm
  reattach. All deferred.

## Known WinUSB caveats (mostly NOT applicable, kept here for context)

The AWUS036AXML (MT7921AU) bring-up uncovered several WinUSB / USB 3.0
SuperSpeed quirks. Since the AWUS036ACH negotiates as HS, **none of them
should bite us** — but documenting in case M2+ ever flips the chip to SS
mode (via `rtw_usb_switch_mode`):

- WinUSB serializes bulk-IN URBs one at a time unless `RAW_IO` is set.
  `libusb 1.0.27` added `LIBUSB_OPTION_WINUSB_RAW_IO` to fix this; our
  bundled `libusb_package` ships `1.0.26`, so we *can't* enable RAW_IO
  without bumping the dependency.
- USB 3.0 NRDY/ERDY mishandling caused the MT7921AU FW_SCATTER 4-packet
  bulk-OUT stall. Doesn't apply at HS.
- Bulk-OUT ZLP terminator needed when transfer length is a multiple of
  `wMaxPacketSize`. At HS `wMaxPacketSize=512`; legacy MCUFWDL chunks
  are 196/8/1, never hit the boundary.

See `feedback_usb_speed_check.md` in memory for the broader rule.
