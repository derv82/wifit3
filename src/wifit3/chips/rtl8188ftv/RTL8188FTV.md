# RTL8188FTV

## Captured Wireless Card
- No-name `0bda:f179` 802.11b/g/n dongle — 1T1R, 2.4 GHz only, firmware-based.
- Enumerates at USB2 speed only (Bus 001). The monitor capture carries a single bulk-IN completion pipe (RA pipe, URB `0x81`); no bulk-OUT URBs appear in the bring-up/scan phase, so the TX lane count is only visible via `probe_endpoints` on live hardware.
- Capture: `driver_captures/captures_rtl8188ftv/capture-1.pcap` (one cold-boot session, 4995 driver-side ops).

## Linux Driver Source
- Link: mainline `torvalds/linux`, `drivers/net/wireless/realtek/rtl8xxxu/` — the 8188F fileops vector (`8188f.c`) + shared `core.c`/`rtl8xxxu.h`/`regs.h`.
- Type: mainline.
- Version: kernel 6.12.107, driver `rtl8xxxu.ko.xz`.
- Vendored in-repo: no — the port targets the capture's wire directly (no `driver-source/` payload in the bundle).

## Python Port Details
- VID/PID: `0bda:f179`; the default (and only) driver for it. Not to be confused with the vendor-DKMS `rtl8188fu` wire (planned `chips/rtl8188ftv_dkms`).
- Status: **port-complete through M7, cold-boot verified, TX and RX both hardware-verified on-air — MGMT *and* DATA — Linux A/B baseline at parity (beacon rate equal, RSSI +0.7 dB median).** The verify gate replays the whole capture byte-for-byte and gates the RX FIFO decode; live TX is confirmed via `tx_retries` (611 copies of unicast, 0 ACKs), a unique-src broadcast probe (53/60 on ch11), and the data-frames bench (60/60 accepted, 55/60 broadcast + 60/60 unicast on air); live host RX is confirmed via the RX triage (99 beacons/3 s + 27/30 injected deauths + 25/30 data frames through the normal Scanner reader path).
- Related port: `chips/rtl8188eus/` (same mainline `rtl8xxxu` family, 8188e vector). The 8188E and 8188F diverge in the CCK RSSI table, the FW RSV_CTRL reset dance, and `MAX_AGGR`.
- Non-obvious in the port (would cost a maintainer time):
  - Wire is a **USB vendor-control** interface (bRequest `0x05`, wValue=register, wIndex=0), not ep0-default. Reads/writes: 8/16/32-bit + `write_block` for FW chunks.
  - FW upload is `writeN(REG_FW_START_ADDRESS, page, 4096)` producing 32 × 128-byte writes laddered `0x1000/0x1080/…/0x1f80`; the 8188F needs the RSV_CTRL clear/enable dance (4 ops) before the 8051 reset, and `start_firmware` ends with `REG_HMTFR = 0x0f` (`init_reg_hmtfr=1`).
  - RX is **rxdesc24** (24-byte descriptor), inter-frame alignment `roundup(total, 8)` (core.c:6494). `rpt_sel = w2[28]` marks C2H / TX reports — skip those.
  - RSSI is rate-aware: CCK reads the packed LNA/VGA byte (`_rtl8188f_cck_rssi`, formula differs from 8188E), OFDM = `(pwdb >> 1) - 110`. Applying the OFDM formula to CCK frames reads -90+ dBm on strong APs.
  - Chromatic spur calibration fires on ch 5-8, 11, 13, 14 when the PSD report crosses threshold (`_SPUR_THRESHOLD 0x16`).
  - 1T1R: only path A exists, RF data masked to 20 bits (`0x000FFFFF`).
  - TX uses the 40-byte txdesc (v2 fill sets **no** antenna-select bits); MGMT rides the LOWEST bulk-OUT EP (HIGH lane), DATA rides the SECOND bulk-OUT EP (BE/LOW lane, `out_ep[1]` — this dongle exposes exactly 0x02 + 0x03); XOR-16 descriptor checksum. `HW_SEQ_ENABLE` stays cleared so the SW sequence counter is stamped into the frame. The DATA branch sets no `USE_DRIVER_RATE` and fills the `0x1f << 8` rate-fallback mask instead.
  - ACK RX-tap: `RXFLTMAP1` bit 13 admitted/dropped by `admit_ack_frames`/`drop_ack_frames` (kernel keeps PS-Poll only by default).
  - The monitor RCR is re-asserted on **both** cold and warm attach (`_finish_attach`) — a kernel-left warm chip has a non-monitor RCR that drops ToDS frames.
  - EFUSE parse enforces the `0x8129` rtl_id and memcpy's 5 ht40 bytes into a 6-slot array (slot 5 = 0 on our side, then sanitised).

- TX is XOR-16 checksummed over the **first 32 bytes only** (core.c:5128-5141); txdw8/txdw9 (where our SW seq lives at bytes 36-39) sit outside the window — folding them into the csum silently dropped descriptors.

## Known Problems
- The hardware does **not auto-ACK spoofed MACs** (`rx_autoack` — run on a verified-working RX path — 8/150 on a spoofed MAC, 0/150 controls and bogus) → `FAKE_MAC = UNIMPLEMENTED`; `enter_active_monitor` ports REG_MACID, but the bench verdict stays negative and flipping `FAKE_MAC` awaits a positive run.
- The ack-tap/RSSI/`record_ack` path is ported; ACK delivery works (control frames + RXFLTMAP1 bit 13) but the chip does not reliably answer — the auto-ACK verdict is negative.
- Single capture only: channel-hop scan covers 53 hops; no second capture for cross-validation.

## Driver Entry Points
- Bring-up: `driver.connect` → `_cold_bring_up` (EFUSE → FW → MAC/BB/RF → LC/IQ → RX path → tune ch1) or `_warm_reattach`; both funnel to `_finish_attach` (endpoint probe, pipe reset, monitor RCR, RX reader).
- EFUSE / chip params: `efuse.read_and_parse` — cck/ht40 TX-power indices, ofdm/ht20 diffs, MAC, crystal_cap.
- Firmware upload: `firmware.download_firmware` / `start_firmware` (128-B ladder + RSV_CTRL dance, MCU ready poll).
- MAC init: `mac.apply_mac_init_table`, `mac.init_device_post_phy`, `mac.enable_rx_path`, `mac.configure_filter`.
- BB / RF init: `phy.post_mac_init_phy`, `phy.lc_calibrate`, `phy.iq_calibrate`, `phy.set_tx_power`, `phy.enable_rf`.
- Channel tune: `chan.set_channel_2g_20mhz` (spur cal + 20 MHz BB + RF TRX_BW/filters).
- RX: `rx.iter_bulk_frames` (rxdesc24 walk + rssi, drops C2H/TX-report/corrupt); RSSI `parse_phystats_rssi`.
- TX / inject: `tx.build_deauth` + `tx.send_mgmt_frame` / `tx.send_data_frame`, `driver._inject_frame` (FC-type dispatch onto the MGMT or DATA lane), `_stamp_tx_seq`.
- ACK detection: `driver._enable_rx_acks` / `_disable_rx_acks` → `rx.admit_ack_frames` / `drop_ack_frames`.

## Scripts
- **Gate:** `scripts/chips/rtl8188ftv/verify_pcap.py` — byte-diffs the whole cold-boot capture (EFUSE · FW blob · MAC+PHY · LC/IQ · RX path · 53-hop scan) and gates the bulk-IN RX decode (3895 frames / 1927 beacons / RSSI [-90,-32]).

## Debug log

### 2026-09-16 — Linux A/B baseline: parity
`baseline_diff` on the fresh pair of JSONs: **beacon rate 7.0/s on the ref AP (78:8c:b5:7e:3b:90) — matches Linux**, **RSSI median +0.7 dB vs Linux over 22 common APs** (worst single-AP +20 dB at the low-RSSI edge), breadth 24 vs Linux's 29 (5 fewer, weak-AP bounded), channel tune 4/4 with 0 silent / 0 cross-channel. Note: the airmon-created `wlan0mon` wdev refuses `iw set channel` (EBUSY even with the iface down on a fresh driver); tuning works on the primary iface (`wlx…`) with the down/set/up dance once the monitor wdev isn't involved.

### 2026-09-16 — TX and RX hardware-verified on-air
The "0 on-air" and "0 host RX" reports were both probe-side artifacts, not chip faults: `.source` vs `.src` keying + bytes-vs-str counter keys (TX), and shadowing `_rx_read_once` after the reader thread had already captured the original bound method (RX). With the confounders cleared: `tx_retries` (ch11, cold chip) caught **611 copies** of FTV-injected unicast; a broadcast deauth with a unique src reached the AR9271 **53/60**; and the FTV's own Scanner-path reader delivered **99 beacons/3 s + 27/30 injected deauths + 25 data frames** (RCR/FLTMAP0/2 exactly the replay-verified config). Verdict: FTV does NOT auto-ACK (8/150, controls 0, bogus 0) → `FAKE_MAC` stays `UNIMPLEMENTED`. Committed `f2abe0a4`.

### 2026-09-16 — DATA-frame TX live-verified
The mgmt-only TX scope (tx.py) was the last cap on the attack suite (WEP/ARP-replay, WPS EAPOL, EvilTwin handshakes are data frames). Ported the `fill_txdesc_v2` DATA branch: BE queue (0x0), NO `USE_DRIVER_RATE`, `0x1f<<8` rate-fallback mask, same AGG_BREAK/SW-seq/csum; `driver._inject_frame` now dispatches on FC type to the MGMT lane (EP 0x02) or DATA lane (EP 0x03, kernel `out_ep[1]` case 2). Live bench (ch11, AR9271 sniff): broadcast DATA **60/60 accepted → 55 on air**, unicast DATA **60/60 accepted → 60 on air**, 0 in baseline. Tests: `pick_bulk_out_data`/`build_tx_desc_data`/`send_data_frame` + lane-routing unit tests (58 FTV tests, full suite 2869 passed, ruff clean). This unblocks WEP percentage, WPS M1-M7, and the EvilTwin handshake. Soak: 30-min ch6 dwell, 506,302 bursts, 0 dropped, `wedged=False`.

### 2026-09-16 — 20-min channel-hop soak: flat
14-channel loop (1-14), 2 s dwell, 20 min: **600 hops, 0 tune failures**, 66,728 RX bursts, 32,388 beacons, **0 dropped**, `wedged=False`. Second soak in the same session (ch6 dwell + hop) — no reader stall or channel-tune wear.