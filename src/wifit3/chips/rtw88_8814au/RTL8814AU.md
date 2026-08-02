# RTL8814AU (Alfa AWUS1900)

Realtek RTL8814AU, 4T4R 2.4 + 5 GHz 802.11ac. rtw88-family (modern) port from the mainline
kernel `rtw88_8814au` source, sharing `chips/rtw88_base/`. RISC core (`RTW_WCPU_3081`) → iDDMA
segmented firmware path, so its closest already-ported sibling is the 8822BU, not the 8812AU.

## Status

Ported, RX hardware-validated, graded C in `docs/SUPPORTED-HARDWARE.md` (full attack matrix passes; RX path
complete, 0/100 cold boots deaf). The DKMS sibling `rtl8814au_dkms` is the manager default — it
fixes the weak mainline 2.4 GHz signal. TX/deauth works but at uncalibrated baseline power. Still
open: calibrated full-power TX and the 2.4 GHz RX weakness below.

## Gotchas

**This card's 2.4 GHz RX is weak/miscalibrated.** Measured once: a 5 GHz AP at −54 dBm while a
2.4 GHz AP read −82 dBm, with 2.4 GHz beacon rate 0.5–2/s vs ~10/s on 5 GHz. The low *rate* (not
just RSSI) points at real sensitivity loss, not a display bug — suspect the 2G RX/AGC/LNA setup in
`switch_band`, the 2G crystal_cap/spur path, or a 2G RSSI miscalc. Attacks still worked on 2.4 GHz
the same session, so RX functioned despite the weak reception. The DKMS sibling exists to fix this.

**No DIG watchdog meant ~50% cold boots came up RF-deaf.** The kernel runs
`rtw_phy_dynamic_mechanism` every 2 s, walking the OFDM initial gain index (IGI) from the
false-alarm count; we ran none of it, leaving IGI at the AGC-table default, so whether RX could
hear was a lottery on boot analog state. Fixed in `dynamic.py`: seed IGI to max coverage (0x1c) at
each bring-up + a 2 s FA-driven walk. This is *the* gain knob — the old "re-roll phy_set_param 8×"
retry loop was just re-rolling that lottery.

**RX aggregation must be ON for the parser to frame-align.** Without it `iter_bulk_frames` parsed
~0 frames (chip delivered 20 KB, parser saw ~1). `rtw_usb_dynamic_rx_agg_v1` makes the chip flush
bulk transfers on frame boundaries (each starting with an rx_pkt_desc), as the rx_handler requires.
See `rx.enable_rx_aggregation`. (Conversely, dropping `prime_bulk_in`'s clear_halt/drain mattered —
it desynced the active stream.)

**Start the RX reader BEFORE the RF-receiving probe.** `connect()` originally ran the 2 s
`rf_receiving_frames` probe with the reader not yet started, so ~1000 decoded frames backed up an
undrained bulk-IN → RX-DMA halts ("1 beacon then silent"). Drain throughout, as the kernel does.

**PHY/channel registers are write-and-forget — do NOT validate by readback.** Confirmed against the
pcap: the kernel writes RF_CFGCH / CCK_CHECK / CLKTRK / AGC_TABLE blind, never reads them, and on
hardware several read back constant regardless of channel (RF_CFGCH→0xEA, etc.). RF *writes* do land
(proven by the RF 0x1c RCK readback through the same path).

**This is a B-cut sample** (`REG_SYS_CFG1 = 0x044411b5`, cut_mask 0x04). PHY/RF init tables are
cut-gated, and `rtw8814a_adc_clk` correctly skips (A-cut only). Enumerates at USB 2.0 (bcdUSB
0x0200) despite the USB-3 branding — matters for RX throughput.

**RF reads are direct MMIO, not 3-wire SIPI.** Unlike the 8812au/8821au cousins,
`rtw8814a.read_rf = read32(rf_base_addr[path] + addr*4)` (same as 8822b), so 8814a got its own
chip-local `rf.py` (direct read + sipi write, 4 paths A/B/C/D) rather than extending the shared
`rtw88_base/rf_sipi.py`, which only knows paths a/b.

**Band-switch can come up deaf.** On a 2G↔5G change the RF front-end fails to re-lock ~30–50% of the
time (CCA=0, beacons stop entirely on the new band). `set_channel` verifies CCA>0 over a 40 ms
window and forces a fresh `switch_band` re-tune if deaf (≤4×); one re-lock recovers every case.
`switch_band` matches the kernel byte-for-byte, so this is probably genuine analog re-lock variance,
not a port gap — but that wasn't confirmed against a kernel band-transition capture.

**Firmware is two segments, no EMEM.** The blob has `mem_usage=0x08` (BIT(4) clear) → DMEM
(5784 B @ 0x80200000) + IMEM (62456 B @ 0x80000000) only; segment dst addresses come from the FW
header, not hardcoded. The pcap-extracted blob is byte-for-byte equal to `rtw8814a_fw.bin[64:]`.
Note the kernel keeps two TX-desc sizes: the real `tx_pkt_desc_sz` (40) for the descriptor + iDDMA
offset vs a hardcoded `TX_DESC_SIZE=48` for the `%512` ZLP-avoidance check; `firmware.py` keeps them
separate (`TX_PKT_DESC_SZ` vs `FW_DLFW_ZLP_TXDESC`).

**Intentional scope limits** (fine for this tool): 20 MHz only (no 40/80 MHz tune); non-DFS 5 GHz
only (36-48, 149-165); TX runs at BB/AGC-table baseline power, not the EFUSE-calibrated per-channel
chain, so it's weaker than aireplay at distance. We run zero runtime IQK — the kernel defers it to
`mgd_prepare_tx` (before TX), so monitor RX needs none; only a TX-quality nicety is missing.

## Orientation

Bring-up runs power-on → iDDMA FW upload → MAC init → EFUSE → PHY (BB/AGC/RF×4 tables) → channel
tune, mostly in `driver.connect()`. Firmware path adapts `chips/rtl8822bu/firmware.py`. The 4×4 bulk
is `*_tbl.py` (mac/agc/bb/rf_a/b/c/d, ~29k u32s ported 1:1 from `rtw8814a_table.c`), loaded by
`phy.phy_set_param` via the phy_cond walker. Channel tune is `chan.set_channel` (switch_band +
per-path RF_CFGCH + spur_calibration). RX desc decode + RSSI (jaguar phy_status, OFDM = 2nd-lowest
of 4 per-path gains −110) is `rx.py`; monitor RCR is `rx.apply_monitor_rcr`. The IGI watchdog is
`dynamic.py`. TX desc (40-byte, 10 u32, no DATARATE_FB_LIMIT) is `tx.py`. Names match the kernel C
in `data_dumps/rtw88-source-v6.18/` — grep there to cross-reference.

`rfe_option` comes from EFUSE (`efuse.py`, resolved per `rtw8814a_read_rfe_type`: bit7→USB) and gates
the AGC/RF table branches; an early placeholder (=1) loaded a valid-but-suboptimal gain variant.

## Scripts

- `test_hw_8814au.py` — per-phase cold-boot HW test (`--phase fw|mac_init|efuse|phy|channel|rx|...`).
- `extract_rtw8814a_fw.py` — reassemble + byte-verify the FW blob from the pcap.
- `extract_init_tables.py` — port `rtw8814a_table.c` flat-u32 tables to `*_tbl.py`.
- `repro_band_death.py` — CH44↔CH1 toggle that isolated the band-switch deaf-on-re-lock.
- `measure_rx_load.py` — headless RX-load harness (could not reproduce the user-visible silence).

## Debug log

### 2026-05-26 — cold boot RF-deaf ~50% + RX silence after channel hop

Init came up RF-deaf about half the time. Source review ranked causes; the primary was the missing
DIG/dynamic-mechanism watchdog — IGI stuck at the AGC default makes RX a lottery on boot analog
state, which is exactly why the 8× phy_set_param re-roll "worked after a few tries." Ported the DIG
watchdog (`dynamic.py`): 8/8 consecutive bring-ups came up first-try receiving (CRC-OK 119–351),
zero deaf, vs the prior ~50%. Also closed real divergences found in the same pass: `config_trx_path`
(CCK 2RX/MRC + path-B antenna) and crystal_cap→AFE_CTRL3 were skipped and are now in `phy_set_param`;
`rtw_phy_init` turned out to be pure software bookkeeping with no HW writes (its only role, the IGI
seed, is now `dynamic.dig_init`). Disconfirmed: missing IQK (kernel defers for monitor) and missing
adc_clk (A-cut only, we're B-cut).

The 8× re-roll and the band-switch CCA re-lock are acknowledged detect-and-recover band-aids, not
fully root-caused — but the cold-boot/agg + DIG fixes are verified state-matches. Kept the re-roll as
a safety net pending cold-physical-replug confirmation.

### 2026-05-26 — pcaps are init-only, then full captures cleared cadence

The first 3 captures were init-only (~5260 frames, ending before the first `iw set channel`) because
`capture.py` hardcoded `usbmon3` and the FW-loading adapter re-enumerates onto a different bus after
firmware boot — every capture stopped at the init burst. Fixed to `usbmon0` (all-buses). The fresh
full captures (init+airmon+hops+inject) cleared cadence decisively: the kernel `iw` fast-hops at
0.25 s capturing ~1250–1560 USB frames per hop, healthy at our exact cadence — so any post-hop death
is purely our userland `set_channel`/RX path, not relock-vs-dwell. The per-hop tune sequence matches
the kernel, which tunes RF_CFGCH live with no RX-DMA pause. The 536× `REG_AGC_TBL` per-hop writes are
the TX-power table, irrelevant to RX death. IQK fires once at bring-up (it's in the `rtw8814a_bb` BB
init table we already load 14/14, not a runtime op we lack); the runtime `do_iqk` appears only before
aireplay TX — matching mainline's deferral.

### 2026-05-26 — RX brought up + sensitivity/RSSI resolved

Beacon captured and decoded end-to-end (bulk-IN → 24-B rx_desc → MPDU → parser → SSID). Sensitivity
was 1 beacon/9s (closest AP only) because the CCK packet-detect threshold sat at the insensitive
table default; pinned REG_CCK_PD_TH to LV0 + enabled 2R-CCA/MRC → 64 BSSIDs/9s
(`rx.tune_monitor_cck_sensitivity`). RSSI ported from `rtw8814a_query_phy_status` (jaguar): own AP
~−28 dBm, neighbours −54..−86 dBm. Lesson from the intermittent-RX hunt: don't retire a diagnostic on
a 10/10 sample for a ~1/11 bug — the agg-off fix cut the rate 10/10 but attempt #11 still reproduced
it, which is how the undrained-probe cause surfaced.

### 2026-05-26 — band-switch death isolated and recovered

`repro_band_death.py` (CH44↔CH1) + a PHY-counter probe pinned channel-hop "death" to intermittent
RF deaf on band switch (CCA=0, energy detection upstream of gain — so "going to 5G revives 2G").
Ruled out per-hop extras (worse without), a missing register (switch_band matches kernel byte-for-
byte; bb_swing/pwrtrack are TX-only), and timing (80 ms settle didn't help). Fix: detect CCA>0 over
40 ms and force a fresh switch_band if deaf. HW: repro 0/15 + 0/12 (was 3-7/10); general band-
crossing `--hop` 0 no-frames/20 s (was 3-5).

### 2026-05-31 — 2.4 GHz RX weakness (HW)

Promoted to Gotchas. The "validated" RX held for 5 GHz and the cold-boot capture, but a 2G-specific
RX gap slipped through: 5 GHz AP −54 dBm vs 2.4 GHz AP −82 dBm, 2.4 GHz beacon rate 0.5–2/s vs ~10/s.
The low rate (not just RSSI) points at real sensitivity loss. Still open.

### M6 plan — calibrated full-power TX (parked)

Today TX injects at the BB/AGC-table baseline (uncalibrated), kneecapping range vs aireplay. Scope
(user-approved): port EFUSE power-by-rate base + by-rate offset + bb_swing + the `REG_AGC_TBL` write,
transmit at full calibrated power; deliberately skip the 8 regulatory power-limit variants (they only
*cap*), pwrtrack (thermal), and IQK-before-TX. The cold-boot pcap captured the kernel's exact
per-channel `REG_AGC_TBL` writes, so `set_tx_power_index` output can be diffed to prove the write
encoding (our values will be ≥ the pcap since we skip the cap — intended); HW confirmation needs RF.
EFUSE layout is resolved (`struct rtw8814a_efuse`: `txpwr_idx_table[4]` @ 0x10, 42 B/path =
2G(18)+5G(24), diffs little-endian signed 4-bit; plus bb_swing @ 0xc6/0xc7, thermal_meter @ 0xba).
Step 1 (EFUSE power-by-rate parse) landed; steps 2–6 (by-rate offset, computation, REG_AGC_TBL write,
bb_swing fold into `_switch_band`, wiring after phy_set_param + per set_channel) parked.
