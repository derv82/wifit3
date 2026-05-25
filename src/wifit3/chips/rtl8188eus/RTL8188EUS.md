# RTL8188EUS Reverse Engineering Context

## Potential Known Gaps

Cross-driver gap classes (project audit 2026-05-25). **Offline analysis only —
no hardware available; verify before `[x]`.**

- [ ] **RX polling loop drops frames** — LIKELY AFFECTED. `driver.py:_rx_loop`
  (363) does `await loop.run_in_executor(_read_once)` (381) then parse on the
  event loop — no read posted while parsing. Same pattern as rtl8821au pre-fix.
  Fix: dedicated reader thread + queue hand-off (rtl8821au commit 2e3a7a7).
- [ ] **RX filter / monitor mode (ToDS capture)** — COLD OK, **WARM PATH BROKEN**
  (the exact trap fixed in rtl8821au b6e7cb9). `RCR_MONITOR` is correct — it
  includes `RCR_ACCEPT_AP` (all unicast/promiscuous) — and `enable_rx_data_path`
  (mac.py:327) writes it, BUT that's only called from `_cold_bring_up`
  (driver.py:222). `_warm_reattach → _finish_attach` (238/250) does NOT reapply
  it, so a chip left warm by the kernel STA driver / a prior session keeps a
  non-promiscuous RCR and drops client→AP (ToDS) frames. Fix: move the RCR
  write into `_finish_attach` so it runs on both paths (mirror rtl8821au's
  `apply_monitor_rx_filter`). Cold-boot (replug) works today; warm doesn't.

Verified facts only. Anything not in this doc is a hypothesis.

Citations: `[SRC]` = kernel source path, `[WIRE]` = `usb_dumps/captures_rtl8xxxu/capture-N.pcap` frame numbers.

## 1. Project Objective

Userspace Python driver for Realtek RTL8188EUS (e.g. TP-Link TL-WN722N v2/v3, several other low-cost dongles) — monitor mode + injection via PyUSB. Cleanroom port of the kernel `rtl8xxxu` driver's 8188e fileops vector.

## 2. Hardware & Environment

- **Target Chipset:** Realtek RTL8188EUS (WiFi 4, 2.4 GHz only, 1T1R).
- **VID/PID:** `0x2357:0x010C` (TP-Link TL-WN722N v2/v3). Other vendor PIDs share the chip but are not registered yet.
- **USB Speed:** HS (`bcdUSB = 0x0200`). No WinUSB SuperSpeed RAW_IO concerns.
- **Firmware:** `rtl8188eufw.bin`, 15262 B total = 32 B header (`struct rtl8xxxu_firmware_header`) + 15230 B payload. Signature `0x88E1`, version `16.0`. sha256 = `2ff74315287529dec2e50eb57d6e0c97d2116f28ae166773ccdf93b6360000c4`.
- **Platform:** Windows (WinUSB via Zadig) and Linux (libusb after `rmmod r8188eu` / `rmmod rtl8xxxu`).

## 3. Kernel reference scope

- Chip-specific: `data_dumps/rtl8xxxu-source-v6.18/8188e.c`
- Family-shared: `core.c`, `rtl8xxxu.h`, `regs.h`
- **NOT** `rtw88-source-v6.18/` (different family — rtw88 / iDDMA path does NOT apply to 8188e).
- **NOT** `data_dumps/rtl818x-source-v6.8/` (that's rtl8180/rtl8187 — pre-N hardware, unrelated despite the similar name).

The 8188e fileops vector `[SRC] 8188e.c:1835-1885` is the single source of truth for which kernel functions we port.

## 4. Wire protocol

All MAC register reads/writes are vendor control transfers `[SRC] rtl8xxxu.h:34-36`:

| Field | Value |
|---|---|
| `bRequest` | `0x05` (`REALTEK_USB_CMD_REQ`) |
| `bmRequestType` | `0x40` write, `0xC0` read |
| `wValue` | register address (16-bit) |
| `wIndex` | `0x00` |
| `wLength` | 1, 2, or 4 (for byte/word/dword reads); up to 196 for FW page chunks |

The 196-byte `FW_WRITE_BLOCK_SIZE` comes from `[SRC] 8188e.c:1863 (.writeN_block_size = 196)` — verified on the wire: every FW page is exactly `20 × 196 + 1 × 176 = 4096` bytes, plus a final remainder of `15 × 196 + 1 × 2 = 2942` bytes `[WIRE] capture-1 frames 2513..2681`.

## 5. Bring-up phases (capture-1)

| Phase | Frame range | Notes |
|---|---|---|
| USB enumeration | 1 – 110 | Hub setup, descriptors |
| Chip identify + EFUSE + power_on | 111 – 2493 | First vendor-ctrl read: `0xC0 wValue=0x00F0 wLength=4` = `REG_SYS_CFG` (chip ID gate) |
| FW DL prologue | 2495 – 2511 | Set `MCU_FW_DL_ENABLE`, `MCU_FW_DL_CSUM_REPORT`, page-index byte 0 |
| FW page upload | 2513 – 2681 | 4 page-windows × ≤21 × 196-B chunks → `REG_FW_START_ADDRESS = 0x1000` |
| FW ready ack | 2683 – 2727 | Poll `REG_MCU_FW_DL` for `MCU_FW_DL_CSUM_REPORT \| MCU_WINT_INIT_READY` |
| MAC init table | 2729 → … | Writes to `0x0026, 0x0027, 0x0040, 0x0421+` (`rtl8188e_mac_init_table`) |
| PHY/BB/RF init + IQK | … – 5212 | end of `<hardware_plugin_and_initialization>` slicer slice |
| airmon-ng start | 5213 – 6937 | Monitor mode bring-up |
| Channel hops 1..12, 1 | 6938 – 10544 | `iw dev wlan1 set channel <n>` |
| TX inject | 10545 – 18132 | aireplay-ng `--test` + deauth |

End-to-end M1 wall time = ~1.23 ms (frame 2495 → 2727, ~ 200 µs polling).

## 6. M1 — FW upload + 8051 ready ack

### Status: DONE 2026-05-18 (verified on TL-WN722N v2/v3)

Hardware test: `scripts/rtl8188eus/test_hw_rtl8188eus.py --phase all --debug` passed first-shot. `MCU_WINT_INIT_READY` set 4.1 ms after `start_firmware` invocation (kernel ground truth from capture-1 is ~1.23 ms; the ~3× overhead is WinUSB control-transfer latency on Windows vs Linux usbmon).

## 7. M2 — Post-FW MAC init + LLT + MAC TX/RX enable

### Status: DONE 2026-05-18 (verified on TL-WN722N v2/v3)

Hardware test: `--phase all --debug` first-shot pass. `post_fw_mac_init` completed in **26.8 ms** (176 polled LLT writes × 2 control transfers each + the rest). Readbacks confirmed:

- `REG_CR = 0x06FF` (`CR_INIT_POWER_ON | MAC_TX_ENABLE | MAC_RX_ENABLE`)
- `REG_TRXFF_BNDY = 0x25FF00AA` (byte 0 = 0xAA from `set_tx_buffer_boundary`, byte +2 = 0x25FF from `set_trxff_rx_page_boundary`)
- `REG_RQPN = 0x008C0000` — chip auto-cleared `RQPN_LOAD` (BIT 31) after consuming the partition. Load-and-clear semantics.

Implemented in `mac.py`. Order in `post_fw_mac_init`:

| Step | Kernel source | What it does |
|---|---|---|
| `apply_mac_init_table` | `core.c:2187-2226` + `8188e.c:19-44` | 92 byte writes (mactable) + REG_MAX_AGGR_NUM=0x0707 |
| `init_queue_reserved_page` | `core.c:3815-3845` | TX FIFO partition (defaults to normal-only) |
| `set_trxff_rx_page_boundary` | `core.c:3962` | write16 REG_TRXFF_BNDY+2 = 0x25FF (RX page bndy) |
| `set_tx_buffer_boundary` | `core.c:4023-4031` | 5 byte writes (TXPKTBUF_* + REG_TRXFF_BNDY + REG_TDECTRL+1) all = 0xAA |
| `set_pbp` | `core.c:4038-4041` | REG_PBP = 0x11 (page size 128, RX + TX) |
| `init_llt_table` | `core.c:2519-2556` | 176 polled LLT writes: pages 0..168 chained, 169=0xFF, 170..175 ring buffer |
| `enable_mac_tx_rx` | `8188e.c:1299-1301` | REG_CR \|= CR_MAC_TX_ENABLE \| CR_MAC_RX_ENABLE |

### Kernel order vs port order

The kernel runs `init_queue_reserved_page` + `init_queue_priority` + `REG_TRXFF_BNDY+2` PRE-FW (`core.c:3951-3962`). M1 finished FW upload without those writes (and passed), so we hoist them post-FW into M2 instead. The 88E HW-bug comment (`8188e.c:1180-1183`) only mandates `REG_TRXFF_BNDY` is set before `MAC_TX/MAC_RX_ENABLE` — both writes happen earlier in `post_fw_mac_init` than the MAC enable flip.

### Skipped in M2

- `init_queue_priority` (`core.c:2583-2714`) — TX-DMA routing. Doesn't affect monitor-mode RX; defer to TX-inject milestone.
- `init_burst` (`core.c:3847-3885`) — REG_RXDMA_PRO_8723B + AMPDU + ACK timeout etc. Not needed for basic RX.
- `usb_quirks` rest of body (`8188e.c:1302-1306`) — `rtl8xxxu_gen2_usb_quirks` + REG_EARLY_MODE_CONTROL_8188E. Defer.
- Interrupt mask config (`core.c:4102-4125`).
- RCR / RXFLTMAP* (`core.c:4128-4154`). PHY/RF needed before these matter for actual frames.
- EDCA / SIFS / DARFRC (`core.c:4172-4194`).

### Success criterion

`scripts/rtl8188eus/test_hw_rtl8188eus.py --phase mac --debug` reports:
- `REG_CR` has bits `CR_MAC_TX_ENABLE` (0x40) and `CR_MAC_RX_ENABLE` (0x80) set
- `post_fw_mac_init` completes without raising
- `init_llt_table`'s 176 polled writes all return to `LLT_OP_INACTIVE` within `LLT_WRITE_POLL_MAX=20` polls per write


Implemented in `firmware.py` + `driver.py._power_on`. Three kernel functions ported verbatim:

| File | Function | Kernel source |
|---|---|---|
| `firmware.py` | `download_firmware` | `[SRC] core.c:2004-2103` |
| `firmware.py` | `start_firmware` | `[SRC] core.c:1944-2002` |
| `firmware.py` | `firmware_self_reset` | `[SRC] core.c:2159-2184` |
| `firmware.py` | `reset_8051` | `[SRC] 8188e.c:558-568` |
| `driver.py._power_on` | `rtl8188eu_power_on` | `[SRC] 8188e.c:1165-1193` |
| `driver.py._disabled_to_emu` | `rtl8188e_disabled_to_emu` | `[SRC] 8188e.c:993-1000` |
| `driver.py._emu_to_active` | `rtl8188e_emu_to_active` | `[SRC] 8188e.c:1002-1069` |

### Out of scope for M1

- EFUSE parse — defaults are fine for now (per `[[rfe-defaults-first]]`)
- MAC init table (`rtl8188e_mac_init_table`)
- PHY/BB/RF init, IQ calibration
- LLT init, queue init, aggregation init
- Channel switching (RF18 SIPI, BB writes)
- RX path (descriptor decode, bulk-IN polling)
- TX inject (`fill_txdesc_v3`)

### Success criterion

`scripts/rtl8188eus/test_hw_rtl8188eus.py` reports:
- `REG_SYS_CFG = 0x<some-nonzero>` (chip ID readout works)
- `download_firmware` completes without raising
- `MCU_WINT_INIT_READY` set within `RTL8XXXU_FIRMWARE_POLL_MAX × 100 µs = 100 ms`

## 8. M3 — PHY init (BB + AGC + RF path A)

### Status: DONE 2026-05-18 (verified on TL-WN722N v2/v3)

Hardware test: `--phase all --debug` first-shot pass. PHY init completed in **413 ms** total:
- BB table (192 × write32): 101 ms
- AGC table (130 × write32): 65 ms
- RF-A table (91 SIPI writes + 4 × `msleep(50)` opcodes = 200 ms mandatory sleep): 247 ms

`REG_CR = 0x06FF` unchanged pre/post PHY init — chip responsive through all 413 register writes, no wedging.

Implemented in `phy.py` + `phy_tables.py`. The init tables are mechanically extracted from the kernel source by `scripts/rtl8188eus/extract_phy_tables.py`; re-run that if the kernel source updates.

| Function | Kernel source | What it does |
|---|---|---|
| `init_phy_regs` | `core.c:2228-2252` | Iterates a (u16, u32) BB/AGC table, write32 + 1 µs/entry |
| `write_rfreg` | `core.c:912-947` | Encodes `(reg << 20) \| (data & 0xFFFFF)` → `REG_FPGA0_XA_LSSI_PARM`; 8188e is 1T1R so path-B is rejected |
| `init_rf_regs` | `core.c:2385-2431` | Iterates RF table; honours delay opcodes 0xF9..0xFE (kernel `case` block) |
| `init_phy_rf` | `core.c:2433-2495` | SW_CTRL/INT_OE/HSSI_PARM2 pre-writes for SIPI mode → `init_rf_regs` → RFENV restore |
| `init_phy_bb` | `8188e.c:582-603` | SYS_FUNC + RF_CTRL prep pokes → load `phy_init_table` + `agc_table` |
| `init_phy_rf_8188e` | `8188e.c:605-608` | 1-line wrapper: `init_phy_rf(RADIO_A_INIT_TABLE, RF_A)` |
| `post_mac_init_phy` | (driver glue) | `init_phy_bb` → `init_phy_rf_8188e` |

### Table sizes (extracted; assert-checked in the extractor)

| Table | Entries | Source |
|---|---|---|
| `PHY_INIT_TABLE_8188E` | 192 × u32 → BB regs 0x800-0xFAC | `8188e.c:46-145` |
| `AGC_TABLE_8188E` | 130 × u32 → AGC regs (mostly 0xC78) | `8188e.c:146-214` |
| `RADIO_A_INIT_TABLE_8188E` | 95 × {u8 addr, u32 val} (4 of which are 0xFE = msleep(50) opcodes) | `8188e.c:215-313` |

Total wire writes: 192 + 130 + 91 = 413, plus 4 × `time.sleep(0.05)` for the RF delay opcodes (≈ 200 ms of sleep).

### Skipped in M3 (calibration; chip will RX without these but with degraded sensitivity)

- `rtl8188eu_phy_iq_calibrate` (`8188e.c:906-991`) — IQ calibration
- `rtl8723a_phy_lc_calibrate` — LC calibration (path-A only on 8188e)
- `usb_quirks` rest of body (`8188e.c:1302-1306`)

### Watch-outs

- RF delay opcodes 0xF9..0xFE must NOT be SIPI-written — kernel `case` block handles them as sleeps. The port preserves this in `_RF_DELAY_OPCODES`. Forgetting this would write to non-existent RF regs and silently corrupt PHY state.
- `REG_FPGA0_XA_LSSI_PARM` is the only LSSI path for 8188e (1T1R). The kernel's `rtl8xxxu_rfregs[path]` lookup table collapses to that one register for path A; `write_rfreg` raises on path B.
- `FPGA0_LSSI_PARM_DATA_MASK = 0x000FFFFF` — RF data is 20-bit, not 32-bit. Upper 12 bits are silently masked.

### Success criterion

`scripts/rtl8188eus/test_hw_rtl8188eus.py --phase phy --debug` reports:
- 413 register writes complete without `USBTimeoutError`
- `REG_CR` readback post-PHY-init is non-bogus (not 0x0000 or 0xFFFF) — chip not wedged

## 9. M4 — Channel tune (2.4 GHz, 20 MHz)

### Status: DONE 2026-05-18 (verified on TL-WN722N v2/v3)

Hardware test: `--phase channel --debug` first-shot pass. Wall time **10.6 ms**. `RF MODE_AG = 0x07C01` confirmed:
- channel bits[9:0] = 1 (expected 1)
- BW bits[11:10] = 0xC00 (expected 0xC00)

First **round-trip SIPI read+write proof** in the bring-up — both halves of the RF transport now confirmed on hardware.

Implemented in `chan.py`. Restricted to the 20 MHz / 2.4 GHz subset:

| Function | Kernel source | What it does |
|---|---|---|
| `read_rfreg(t, RF_A, reg)` | `core.c:867-905` | SIPI READ on path A. 4 writes to `REG_FPGA0_XA_HSSI_PARM2` with EDGE_READ toggling + 10/100/10 µs sleeps. Reads back via HSPI or LSSI based on `FPGA0_HSSI_PARM1_PI`. Returns lower 20 bits. |
| `set_channel_2g_20mhz(t, ch)` | `8188e.c:431-447, 507-521` | (1) `REG_BW_OPMODE \|= BW_OPMODE_20MHZ`, (2) clear `FPGA_RF_MODE` in `REG_FPGA0_RF_MODE` + `REG_FPGA1_RF_MODE`, (3) SIPI read+write `RF6052_REG_MODE_AG` to set channel bits[9:0], (4) SIPI read+write same to set BW bits[11:10] to `MODE_AG_BW_20MHZ_8723B`. |

### Skipped in M4

- 40 MHz width path (`8188e.c:449-501`) — not needed for monitor mode at 20 MHz.
- 5 GHz — chip has no 5 GHz radio.
- Path B writes — 1T1R chip.
- IQK + LC calibration (still deferred from M3).

### Success criterion

`scripts/rtl8188eus/test_hw_rtl8188eus.py --phase channel --debug` reports:
- `set_channel_2g_20mhz` completes without raising
- Round-trip `read_rfreg(RF_A, RF6052_REG_MODE_AG)` returns a value where bits[9:0] == channel number and bits[11:10] == `MODE_AG_BW_20MHZ_8723B (0xC00)`
- This is the **first round-trip SIPI read+write proof** in the bring-up — M3 was write-only.

### Driver Protocol surface

`RTL8188EUSDriver.set_channel(channel)` is now implemented (was previously `NotImplementedError`). Returns `True` on success, `False` on exception. `connect()` ends with `set_channel_2g_20mhz(1)` so the chip is tuned and ready before returning.

## 10. M5 — RX path (beacons)

### Status: DONE 2026-05-18 (verified on TL-WN722N v2/v3)

Hardware test: `--phase beacon --debug` first-shot pass after **three fixes** found in three iterations:

1. **Missing `enable_rf`** (`8188e.c:1262`) — `REG_OFDM0_TRX_PATH_ENABLE = OFDM_RF_PATH_RX_A | OFDM_RF_PATH_TX_A`. Without this, RCR is open but the OFDM block isn't routing samples to RX. Lives in `start` callback, not `init_device`. Saved as memory [[init-then-start]].
2. **Missing `enable_cck_ofdm_block`** (`core.c:4230-4232`) — `REG_FPGA0_RF_MODE |= FPGA_RF_MODE_CCK | FPGA_RF_MODE_OFDM` (bits 24+25). Without this both baseband blocks stay off — gives RCR a firehose with nothing flowing through. Lives in `init_device` past where I'd been reading.
3. **`sizeof(rtl8xxxu_rxdesc16)` is 24, not 16** — the struct has 5 u32 bitfield words in the LE/BE block (20 B) PLUS `u32 tsfl;` declared OUTSIDE the endian block at `rtl8xxxu.h:267` (4 B). Symptom: MPDU bytes offset-shifted by 8 (`tsfl` 4 B + a wrong shift of 4 my eyes added in counting), beacon FC at mpdu[8] instead of mpdu[0]. Saved as memory [[struct-size-post-endif]].

Result: **21 distinct beacon BSSIDs** captured on channel 1 in 5 seconds. 308 of 326 yielded frames were beacons (type=0, subtype=0x8); 16 probe responses, 2 actions, 3 parse failures. SSIDs decode cleanly.

### M5-polish — RSSI offset fix (DONE 2026-05-18)

Initial M5 ship had all RSSIs = -110 dBm. Root cause: `cck_sig_qual_ofdm_pwdb_all` is at byte **4** of `rtl8723au_phy_stats`, not byte 6. The bug was assuming `struct phy_rx_agc_info` was 2 bytes — it's actually a **single u8** with `gain:7, trsw:1` bitfields (`rtl8xxxu.h:593-599`). So `path_agc[2]` is 2 bytes (not 4), pushing all subsequent fields up by 2.

Verified on two URB samples from the M5 hw-test: pwdb=0x2e (46) at byte 4 → RSSI = (46 >> 1) - 110 = -87 dBm. Realistic for typical indoor beacons.

Implemented across two new modules + an addition to `mac.py`:

| Layer | Kernel source | What it does |
|---|---|---|
| `mac.enable_rx_data_path` | `core.c:4100-4154` (8188E branch + universal RCR) | `REG_RX_DRVINFO_SZ = 4`, clear `REG_HISR0`, set `REG_HIMR0`/`HIMR1` masks, `REG_USB_SPECIAL_OPTION \|= USB_SPEC_INT_BULK_SELECT`, **`REG_RCR = RCR_MONITOR`** (the firehose-open write) |
| `rx.probe_endpoints` | (wifit3-side) | Walks the active configuration descriptor, classifies bulk-IN / bulk-OUT / interrupt endpoints |
| `rx.parse_rxdesc16` | `core.c:6261-6320` + `rtl8xxxu.h:135-200` | Decodes the 16-byte (4 × u32) header — extracts `pktlen`, `drv_info_sz × 8`, `shift`, `phy_stats`, `pkt_cnt`, `rxmcs`, `rpt_sel` |
| `rx.parse_phystats_rssi` | `core.c:5658` (OFDM branch only) | Reads byte 6 of the 32-byte `rtl8723au_phy_stats` (= `cck_sig_qual_ofdm_pwdb_all`), returns `(pwdb >> 1) - 110` dBm |
| `rx.iter_bulk_frames` | `core.c:6281-6320` | Walks a bulk-IN URB, yielding `(desc, mpdu, rssi)` for each data/mgmt frame. Skips TX-report frames (`rpt_sel != 0`). **Inter-frame alignment is `roundup(total, 128)`** — 8188e-specific, vs rtw88's 8-byte. |
| `rx.read_rx_burst` | (wifit3-side) | Synchronous `dev.read` wrapper that returns None on USB timeout |
| `phy.enable_rf` | `8188e.c:1262-1274` | **Required after RCR + channel** — `REG_RF_CTRL` re-assert + `REG_OFDM0_TRX_PATH_ENABLE = OFDM_RF_PATH_RX_A \| OFDM_RF_PATH_TX_A` + `REG_TXPAUSE = 0`. Kernel calls this from `rtl8xxxu_start` (`core.c:7364`), AFTER `init_device`. **Discovered 2026-05-18 first M5 hw-test**: without `OFDM0_TRX_PATH_ENABLE` write the chip is deaf — RCR open but OFDM block not connected to RX/TX, so zero bulk-IN URBs delivered. |

### Skipped in M5

- **CCK RSSI accuracy** — `rtl8188e_cck_rssi` (8188e.c:1309-1331) uses an LNA/VGA lookup table. Defer to a polish milestone. M5 uses the OFDM formula `(pwdb >> 1) - 110` for all rates; CCK reads a few dB low but BSSIDs still parse correctly.
- **REG_RXFLTMAP* writes** — 8188e fileops doesn't set `init_reg_rxfltmap`, so kernel takes the ELSE branch (`core.c:4148-4154`) which writes `REG_MAR` (multicast filter). RCR alone is sufficient for monitor-mode reception.
- **Tuning writes** (RESPONSE_RATE_SET, EDCA, SIFS, DARFRC, ACK timeout) — affect TX quality, not RX gating. Defer.

### Success criterion

`scripts/rtl8188eus/test_hw_rtl8188eus.py --phase beacon --debug --beacon-secs 5` reports ≥ 1 distinct beacon BSSID on channel 1 (or the channel passed via `--channel`). The phase prints per-BSSID counts + SSID + RSSI.

### Driver Protocol surface

`RTL8188EUSDriver.connect()` now ends with `enable_rx_data_path` + `set_channel_2g_20mhz(1)`. After `connect()` returns `True`, callers can pull frames via `rx.read_rx_burst` on `rx.probe_endpoints(dev).primary_bulk_in`. The driver does not (yet) spawn a background polling thread — that's the integration step when wiring into `WlanInterface`.

## 11. M6 — TX inject (MGMT/deauth)

### Status: DONE 2026-05-19 (verified on TL-WN722N v2/v3)

Hardware test: `--phase tx --debug` first-shot pass. **0.4 ms** for the full descriptor build + checksum + bulk-OUT write. Chip accepted the 58-byte URB (32 B desc + 26 B deauth) on EP 0x02 without `USBError` or pipe stall. Endpoint convention confirmed: lowest-numbered bulk-OUT (`0x02`) is the HIGH/MGMT lane.

Whether the frame actually radiated on air not yet verified — needs an external sniffer/witness. But the wire-level USB TX path works.

Implemented in `tx.py` + `mac.init_queue_priority_2ep`. The 8188e fileops:

```
.fill_txdesc = rtl8xxxu_fill_txdesc_v3  (core.c:5321)
.tx_desc_size = sizeof(rtl8xxxu_txdesc32)  = 32 bytes  (rtl8xxxu.h:400-412)
```

### Components

| Layer | Kernel source | What it does |
|---|---|---|
| `tx.build_deauth` | 802.11 frame builder (no kernel equivalent) | 26-byte frame: 24-byte 802.11 hdr (fc0=0xC0 = subtype 0xC + type 0) + 2-byte reason code |
| `tx.build_tx_desc_mgmt` | `core.c:5449-5466` + `5357-5362, 5395-5397` | 32-byte descriptor: `pkt_size`, `pkt_offset=32`, `txdw0 = OWN \| FIRST_SEG \| LAST_SEG [\| BROADMULTI]`, `txdw1 = QUEUE_MGNT (0x12) << 8`, `txdw2 = AGG_BREAK \| ANT_A \| ANT_B`, `txdw4 = USE_DRIVER_RATE`, `txdw5 = (retry=6 << 18) \| RETRY_LIMIT_EN`, `txdw7 = ANT_C >> 16` |
| `tx.calc_tx_desc_csum` | `core.c:5025-5041` | Clear `csum` (bytes 28-29), XOR all u16s of the 32-byte descriptor, store XOR back in csum |
| `tx.pick_bulk_out_mgmt` | `core.c:2705` | Picks the lowest-numbered bulk-OUT (= first in PyUSB's `bulk_out` list = HIGH-lane = MGMT) |
| `tx.send_mgmt_frame` | `core.c:5538-5542` | Build desc + csum + bulk-OUT write of `[desc \|\| mpdu]` |
| `mac.init_queue_priority_2ep` | `core.c:2618-2693` (ep_count=2 case) | `REG_TRXDMA_CTRL` routing: VO/VI/MGMT/HI → HIGH (EP 0x02), BE/BK → NORMAL (EP 0x03) |

### Endpoint choice

8188EUS exposes bulk-OUT EP 0x02 + EP 0x03. Kernel auto-detects HIGH/NORMAL assignment from USB descriptor vendor-specific bytes; we hard-pick **EP 0x02 = HIGH (= MGMT)** and **EP 0x03 = NORMAL (= BE/BK)** based on the kernel's "lower endpoint = higher priority" convention. If TX stalls in hw-test we know to swap.

### Skipped in M6

- Data-frame TX (`fill_txdesc_v3` data branch, `txdw5` rate field, A-MPDU enable, security cipher selection)
- TX-report consumption (8188e fileops has `has_tx_report = 1`; we just send and forget)
- Channel-bandwidth bits in `txdw4` (only relevant at 40 MHz, we're 20 MHz only)
- RTS/CTS protection (`use_rts`, `use_cts_prot` paths)

### Success criterion

`scripts/rtl8188eus/test_hw_rtl8188eus.py --phase tx --bssid <BSSID> --count 20` reports:
- 20/20 sends complete without `USBError` / pipe stall
- All `send_mgmt_frame` returns equal `len(desc) + len(mpdu) = 58 bytes` written
- Unit-test (already passing): `XOR over full 32 B = 0x0000` confirms checksum encode round-trips

## 12. M7 — Warm reattach + WlanInterface integration

### Status: PORTED, awaiting hw-test

Two pieces in one milestone:

**M7a — Warm reattach** (`mac.is_chip_warm` + `driver._warm_reattach`):

Detect prior-wifit3-session state via two register signals:
- `MCU_WINT_INIT_READY` (bit 6 of `REG_MCU_FW_DL`) — set by `start_firmware`
- `CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE` (bits 6+7 of `REG_CR`) — set by `post_fw_mac_init`

Both set ⇒ skip FW upload, MAC/PHY init, channel set, enable_rf. Just reattach to USB + `_reset_bulk_pipes` + `_rx_smoke_test`. Saves ~500 ms vs cold boot.

**M7b — WlanInterface integration** (`driver._rx_loop` + proper `close()`):

After `_finish_attach`, the driver spawns `_rx_loop` as an asyncio task. The loop:
1. Calls `dev.read(bulk_in_ep, 16384, 100ms)` in an executor
2. Parses URBs via `iter_bulk_frames`
3. Hands each parsed frame to `_rx_callback` (set by `WlanInterface` via `register_rx_callback`)
4. Continues until `_rx_running` becomes False

`close()` is now proper: sets `_rx_running = False`, awaits the task with a 1s timeout (cancels if it hangs), then releases USB. Idempotent.

### Components (driver.py additions)

| Method | Lives in | What |
|---|---|---|
| `is_chip_warm(t)` | `mac.py` | Reads `REG_MCU_FW_DL` + `REG_CR`, returns True if both warm-signals set |
| `_cold_bring_up` | `driver.py` | Original M1-M6 chain extracted into a method |
| `_warm_reattach` | `driver.py` | Just calls `_finish_attach(from_warm=True)` |
| `_finish_attach` | `driver.py` | Probe endpoints, reset bulk pipes, on-warm smoke test, spawn `_rx_loop` |
| `_reset_bulk_pipes` | `driver.py` | `clear_halt` on all bulk endpoints + drain stale bulk-IN bytes |
| `_rx_smoke_test` | `driver.py` | 1.5s × 100ms-attempt bulk-IN read; "please replug" message if wedged |
| `_rx_loop` | `driver.py` | Async RX pump → `WlanFrameParser` → registered callback |
| `close` | `driver.py` | Stops `_rx_loop`, releases USB. Idempotent |

### Success criteria

1. **Cold boot first run**: `--phase warm` after a fresh replug shows `is_chip_warm() = False`.
2. **Warm reattach second run**: after `--phase all` completes (chip is now FW-running + MAC-enabled), re-running `--phase warm` shows `is_chip_warm() = True`. driver.connect() in that state should skip the FW upload phases (look for "WARM — reattaching to running session" log).
3. **WlanInterface integration**: `uv run python -m wifit3` discovers the 8188EUS, the Scanner view fills with BSSIDs from the background RX pump.

## 13. M8 — EFUSE read + per-channel TX power + RCR monitor-mode fix

### Status: DONE 2026-05-19 (verified end-to-end on TL-WN722N v2/v3)

EFUSE parse landed clean on first hw-test:
- MAC `d4:6e:0e:0d:ad:bf` (real, TP-Link OUI)
- CCK power per group: `[0x30, 0x30, 0x2f, 0x2e, 0x2e, 0x2e]` (~24 dBm)
- HT40-1s power per group: `[0x33, 0x33, 0x33, 0x32, 0x31, 0x01]` (~25 dBm, ch 14 dropped to 0x01 by regulatory burn)
- OFDM diff +1, HT20/HT40 diff 0

But the *real* missing piece for end-to-end injection was monitor-mode RCR. Our initial `RCR_MONITOR` was the kernel's STATION-MODE value (`core.c:4130-4133`) — missing `ACCEPT_DATA_FRAME` and `ACCEPT_CTRL_FRAME`. The kernel toggles those in via the mac80211 `configure_filter` callback only when monitor mode is requested. Without them:

- Beacons + probe responses (MGMT) → visible ✓
- Deauth frames flew correctly but the resulting EAPOL re-handshake was invisible → looked like deauth "didn't work"
- PMKID attack worked but the AP's M1 response was filtered out → no PMKID harvested

Fix: `RCR_MONITOR` now `0x7000_7B0F` — all 9 accept-bits set (`AP | PHYS_MATCH | MCAST | BCAST | CRC32 | ICV | DATA_FRAME | CTRL_FRAME | MGMT_FRAME` + `HTC_LOC_CTRL` + 3 APPEND bits).

### Live test result (2026-05-19)

Within seconds of restarting the UI with the RCR fix:

- **Passive 4-way handshake capture from a real client reconnecting to `NETGEAR2G` (M1 + M3 + M4 + PMKID embedded in M1)**
- **Active PMKID harvest** via AUTH+ASSOC injection — instant, matching the user's experience on other cards

8188EUS is now feature-complete for the wifit3 attack stack.

The chip's per-channel TX power values + factory-burned MAC address live in EFUSE (on-die OTP memory). Without parsing them, the TX AGC registers hold reset defaults (typically zero) — frames build correctly but radiate at near-zero power. M6's hw-test confirmed this: chip accepted the URB, deauths didn't reach a phone 3 m away.

### Components

| Layer | Kernel source | What |
|---|---|---|
| `efuse.read_efuse_byte` | `core.c:1746-1778` | Polled byte read via `REG_EFUSE_CTRL` (0x0030) |
| `efuse.read_efuse_map` | `core.c:1780-1890` | Walks the variable-length-word EFUSE encoding, unpacks into 512-byte buffer; pre-fills 0xFF for unwritten bytes |
| `efuse.parse_efuse_8188eu` | `8188e.c:537-557` | Extracts `cck_tx_power_index_A[6]`, `ht40_1s_tx_power_index_A[6]`, `ofdm/ht20/ht40_tx_power_diff` (signed 4-bit, path A), MAC address |
| `phy.channel_to_group_8188e` | `8188f.c:338-355` | Maps 2.4 GHz channel 1-14 → (group, cck_group). 5 groups for the 13 normal channels + a special cck_group=5 for ch 14 |
| `phy.set_tx_power` | `8188f.c:357-397` | 6 register writes (`REG_TX_AGC_A_CCK1_MCS32`, `REG_TX_AGC_B_CCK11_A_CCK2_11`, `REG_TX_AGC_A_RATE18_06`, `REG_TX_AGC_A_RATE54_24`, `REG_TX_AGC_A_MCS03_MCS00` × 4 MCS groups) per-channel-group power |

### Wiring

`driver._cold_bring_up`: EFUSE read happens between `start_firmware` and `post_fw_mac_init` (matches kernel order — EFUSE access needs FW running). Parsed `EfuseDefaults` cached on the driver. `set_tx_power` is called immediately after each `set_channel_2g_20mhz` — both in the cold path's initial channel set AND in the runtime `set_channel()` Protocol method (each of the 5 channel groups uses its own power index, so power must be re-applied on group transitions).

### Fallback path

If `read_efuse_map` raises (rare — usually means we didn't claim USB cleanly or the chip was reset mid-read), the driver logs a warning and uses `EfuseDefaults()` — a hardcoded `0x22` (~17 dBm) per group. Same `[[rfe-defaults-first]]` pattern as the rtw88 chips: prefer real values, never block bring-up on their absence.

### Skipped in M8

- Path-B power tables (8188e is 1T1R — no path B)
- TX power tracking via thermal meter (kernel `pwrtrack_init` + thermal-throttle ISR — adaptive but not required for first-fire injection)
- BT coex power adjustment (no Bluetooth on 8188EUS)
- Custom TX power override flag — see NEXT-STEPS.md "Distant Future: Configurable TX power override"

### Success criterion

`scripts/rtl8188eus/test_hw_rtl8188eus.py --phase efuse` reports a non-fallback CCK / HT40-1s power index (e.g. `0x2E` rather than `0x22`) and a real MAC address (not `00:00:00:00:00:00` or `FF:FF:FF:FF:FF:FF`). Then re-run `--phase tx --bssid <your-AP> --client <your-phone> --count 20` and confirm the phone disconnects.

## 14. Critical kernel quirks the port preserves

1. **`SYS_FUNC + 1 |= 4` pre-flight** (`[SRC] core.c:2024-2026`). This is a byte write to register `0x0003` setting bit 2 — the kernel doesn't name the bit. Required before enabling `SYS_FUNC_CPU_ENABLE`; skipping it leaves the 8051 wedged.
2. **`MCU_FW_RAM_SEL` warm check** (`[SRC] core.c:2034-2040`). If a previous session left FW running, write `0x00` to `REG_MCU_FW_DL` and call `reset_8051` before reuploading.
3. **REG_CR excludes MAC_TX_ENABLE / MAC_RX_ENABLE on 8188e** (`[SRC] 8188e.c:1176-1183`). The 88E silicon has a TRXFF_BNDY HW bug requiring TX/RX MAC enable AFTER `REG_TRXFF_BNDY` is set. M1 leaves both off; they flip on in the post-FW MAC init milestone.
4. **`reset_8051` MUST be called between `MCU_FW_DL_READY` set and the `MCU_WINT_INIT_READY` poll** (`[SRC] core.c:1978`). Without it the 8051 starts the upload but never runs the loaded image — comment says "otherwise it won't come up on the 8192eu", same applies on 8188e since both share `start_firmware`.
5. **`writeN_block_size = 196`** (not 64, not 256, not 512). Hard-coded chunk size. Wire-verified: 20 full 196-B chunks per 4 KB page + a 176-B tail.

## 15. Firmware blob provenance

`assets/rtl8188eufw.bin` was extracted from `usb_dumps/captures_rtl8xxxu/capture-1.pcap` frames 2513–2681 (every vendor-write to `wValue ∈ [0x1000, 0x1FFF]`), in order. Byte-for-byte identical to `data_dumps/rtl8xxxu-source-v6.18/linux-firmware_rtlwifi_rtl8188eufw.bin` (sha256 `2ff74315287529dec2e50eb57d6e0c97d2116f28ae166773ccdf93b6360000c4`). Shipping the pcap-derived copy per `[[firmware-extraction-preference]]`.
