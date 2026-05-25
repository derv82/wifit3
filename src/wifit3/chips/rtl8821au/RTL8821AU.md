# RTL8821AU — Ground-Truth Doc

## Potential Known Gaps

Cross-driver gap classes found the hard way. Audit each per driver; `[x]`
once verified fixed or confirmed N/A, with the commit/evidence.

- [x] **RX polling loop drops frames** — read+parse on the event loop, starved
  by the TUI → ~30% beacon loss, flaky handshakes. Fix: dedicated reader
  thread + hand-off to loop. HW-confirmed 2026-05-25 (commit 2e3a7a7).
- [x] **RX filter drops client→AP (ToDS) traffic** — STA-mode RCR default isn't
  promiscuous, so only M1/M3 (FromDS) seen, never M2/M4 → no 4-way. Fix: write
  the exact airmon monitor RCR `0xf410400f` (AAP + CBSSID cleared) in
  `apply_monitor_rx_filter`, called from `_finish_attach` so it runs on BOTH
  cold + warm attach. HW-confirmed 2026-05-25: full M1–M4 captured (commits
  24bc17d, b6e7cb9). Note: net-type is NOT the gate — [WIRE] frame 5265 shows
  the kernel keeps net-type at MGD_LINKED(2) in monitor.

Realtek 802.11ac single-stream USB chipset (rtw88 family). Driver is shared
with RTL8811AU; both map to `rtw8821a_hw_spec` in the kernel.

Every fact below is either:
- **[SRC]** a direct citation from `data_dumps/rtw88-source-v6.18/`, or
- **[WIRE]** observed in `usb_dumps/captures_rtw88_8821au/capture-1.pcap`,
  produced by `src/wifit3/scripts/capture.py` on Kali.

Anything *not* in this document is a hypothesis. Update as facts get
verified — do not let speculation accumulate here.

---

## Test hardware

- **Card**: ALFA AWUS036ACS
- **VID:PID**: `0bda:0811` [SRC `rtw8821au.c:12`, WIRE bus=3 dev=21 in capture-1]
- **USB**: 2.0 high-speed only (no USB3 alt path)

## Capture-1 timeline (frames)

Mapped via `scripts/pcap_slicer.py`:

| Phase                                | Frames        | Notes |
|--------------------------------------|---------------|-------|
| Hardware plug-in + driver bring-up   | 1 – 3122      | FW upload, EFUSE, MAC init |
| `airmon-ng start wlan1`              | 3123 – 10094  | Monitor-mode flip; FW re-init at f~4339 |
| `iw dev wlan1 set channel 1`         | 10095 – 10580 | 2.4 GHz tune |
| `iw dev wlan1 set channel N` (2.4G)  | 10095 – 14920 | ch 1 → 12 |
| `iw dev wlan1 set channel N` (5G)    | 14921 – 25730 | ch 36 → 165 |
| `aireplay-ng --test`                 | 26221 – 30396 | broadcast TX probe |
| `aireplay-ng -0 1`                   | 30397 – 33614 | deauth TX |

## Bring-up phase (frames 1–3122)

| What                                         | Frames        | Notes |
|----------------------------------------------|---------------|-------|
| Standard USB enumeration (descriptors)       | 1 – ~118      | host hub + device |
| First chip-ident read `REG_SYS_CFG1`         | 119 – 120     | `0x00F0` → returned `0x04412135` [WIRE]. `REG_SYS_CFG2` returns `0x00000005`. |
| `rtw_mac_pre_system_cfg` (USB+8051 path)     | 121 – 126     | write8(0x001C=0); read32(0x00F0); write8(0x007C=0x83) — picked SPS_SEL because BIT_LDO(=1<<24) is clear in 0x04412135 |
| `rtw_mac_power_switch(true)` — REG_CR probe  | 127 – 128     | read8(0x0100) — kernel checks for 0xEA "off" marker before running pwr_seq |
| Power-on sequence (`card_enable_flow_8821a`) | 129 – ~250    | carddis_to_cardemu + cardemu_to_act, USB-applicable entries only |
| `__rtw_mac_init_system_cfg_legacy`           | ~250 – ~270   | REG_CR=0xFF, REG_HWSEQ_CTRL=0x7F, WAKEPAD_EN, ~EN_SIC, REG_CR=0x02FF |
| Firmware upload (legacy)                     | 271 – 935     | 8 pages × ~40 chunks each; see protocol below |
| FW validate / wlan-CPU reset / init tables   | 936 – 3122    | (not yet decoded) |

## Firmware upload protocol (legacy, 8051 wlan CPU)

All control transfers — no bulk-out for FW. Mirrors
`rtw_usb_write_firmware_page` in `usb.c:168` [SRC] and confirmed against
`capture-1` frames 271..935 [WIRE].

Per-chunk wire format:

```
bmRequestType = 0x40           # Vendor, Host->Device
bRequest      = 0x05           # RTW_USB_CMD_REQ
wValue        = FW_START_ADDR_LEGACY + offset_in_page  # starts at 0x1000
wIndex        = 0x00           # RTW_USB_VENQT_CMD_IDX
wLength       = chunk_size     # 196 → 8 → 1 (whichever fits remaining)
data          = chunk_size bytes of raw FW
```

Per-page driver behavior:

1. Write next page index into `BIT_ROM_PGE` of `REG_MCUFW_CTRL` (0x0080).
2. Stream the page's 4096 bytes in 196-byte chunks (then 8, then 1 for
   the tail), addresses advancing from 0x1000.
3. Repeat for `total_page = size >> 12`.
4. If `size & 0xFFF`, do one more partial page.

**Capture-1 numbers** [WIRE]:
- First FW chunk: frame **271**, `wValue=0x1000`, 196 bytes
- Pages streamed: **8** (7 full + 1 partial of 3194 bytes)
- Total bytes uploaded: **31866** = `rtw8821a_fw.bin[32:]`
- Last FW chunk: frame **935**
- Frame 4339 starts a **second** FW upload pass (driven by `airmon-ng`'s
  re-init); same protocol, ignore for the bring-up milestone.

### Firmware blob

- Canonical asset: `chips/rtl8821au/assets/rtw8821a_fw.bin` (31898 bytes,
  with 32-byte `rtw_fw_hdr_legacy` prefix)
- Body (bytes 32..) is **byte-for-byte identical** to what is sent on the
  wire — verified by `scripts/rtl8821au/extract_rtl8821au_fw.py --verify`.
- Header is metadata only (signature, version, build date, size, etc.) —
  stripped before upload [SRC `mac.c:899`].

### Upload-complete ACK

After all FW pages are written, the driver polls `REG_MCUFW_CTRL`
(0x0080) for `BIT_FWDL_CHK_RPT = BIT(2)` to be set [SRC `mac.c:916`].
That is the **device-side ACK** that all FW bytes arrived and the
checksum matched. It's the success criterion for the FW-upload milestone.

A stronger validity check (`FW_READY_LEGACY`) is run after a CPU reset:
`MCUFWDL_RDY | FWDL_CHK_RPT | WINTINI_RDY | RAM_DL_SEL`. That's a
follow-on milestone, not the first.

## Vendor control-transfer register I/O

Mirrors `rtw_usb_read` / `rtw_usb_write` (`usb.c:72,120`) — every
register access is one control transfer.

```
Read:  bmRequestType=0xC0, bRequest=0x05, wValue=addr, wIndex=0x00, len=1|2|4
Write: bmRequestType=0x40, bRequest=0x05, wValue=addr, wIndex=0x00, data
```

## Chip parameters (`rtw8821a_hw_spec`) [SRC `rtw8821a.c:1143`]

| Field                | Value |
|----------------------|-------|
| `id`                 | `RTW_CHIP_TYPE_8821A` |
| `fw_name`            | `rtw88/rtw8821a_fw.bin` |
| `wlan_cpu`           | `RTW_WCPU_8051` |
| `tx_pkt_desc_sz`     | 40 |
| `rx_pkt_desc_sz`     | 24 |
| `page_size`          | 256 |
| `band`               | 2 GHz + 5 GHz |
| `ht_supported`       | true |
| `vht_supported`      | true |
| `rx_ldpc`            | false |
| `usb_tx_agg_desc_num`| 6 |
| `rf chains`          | 1 (single stream, `rf_a_tbl` only) |

## Bring-up milestones

- [x] **0** — FW byte-stream extracted from pcap; byte-matches linux-firmware.
- [x] **1** — USB open + control-transfer read of `REG_SYS_CFG1` succeeds (returned `0x04412135` on first run, matching capture-1 ground truth byte-for-byte).
- [x] **2** — FW upload + `BIT_FWDL_CHK_RPT` ACK confirmed on hardware (2026-05-17).
  - Whole upload took ~31 ms (~320 control transfers; the WinUSB stack handles them faster than the conservative 1 ms/transfer estimate we'd budgeted).
  - Post-upload `REG_MCUFW_CTRL = 0x00070305`: `FWDL_CHK_RPT|MCUFWDL_EN` set, last `ROM_PGE=7` persisted, plus chip-status bits 8,9 (`0x300`) that aren't named in upstream `reg.h`.
- [x] **3** — FW *running* confirmed on hardware. `download_firmware_validate_legacy` flips `MCUFWDL_RDY` + clears `WINTINI_RDY`, toggles wlan CPU, polls FW_READY_LEGACY. Final `REG_MCUFW_CTRL = 0x000706c6` (all four FW_READY_LEGACY bits set).
- [x] **4a** — Init tables extracted to Python + phy_cond walker ported (2026-05-17). Extractor `scripts/rtl8821au/extract_init_tables.py` emits flat-u32 lists under `chips/rtl8821au/assets/{mac,agc,bb,rf_a}_tbl.py`. Walker `chips/rtl8821au/phy_cond.py` mirrors `rtw_parse_tbl_phy_cond` (phy.c:1193) + `check_positive` (phy.c:1150) line-for-line. 16/16 tests pass.
- [x] **4b** — MAC-only init from `rtw88xxa_power_on` (rtw88xxa.c:1055..1175). PASSED on hardware 2026-05-17 first try. Pre-FW: `set_trx_fifo_info` + `llt_init` (256 LLT writes, no poll timeouts) + `REG_TXDMA_OFFSET_CHK BIT_DROP_DATA_EN`. Post-FW: queue/page setup, wmac/edca/beacon, USB burst-cfg, ARFR tables, `REG_CR set BIT_MACTXEN|BIT_MACRXEN`. **Final state**: `REG_CR = 0x000206ff` (byte 0 = 0xFF includes both MACTXEN bit 6 + MACRXEN bit 7; byte 2 = 0x02 from `write32_mask REG_CR 0x30000=0x2`). Whole post-FW init took **4.5 ms**. `mac_tbl` load + BB/RF tables deferred to M4c.
- [x] **4c** — `chips/rtl8821au/phy.py`. PASSED on hardware 2026-05-17 first try (360.9 ms for the full PHY init). Loads `mac_tbl` (98 write8s) + bb_tbl (172) + agc_tbl (130) + rf_a_tbl (277 SIPI writes + 2× 50ms 0xFFE delays) at rfe=0 ELSE branches, runs `switch_band(2G, 20MHz)`, then the line-1185..1217 inline pokes. **Skipped** `rtw_phy_init` (DIG) and `pwrtrack_init` — empirically not required for beacon RX.
- [x] **5** — `chips/rtl8821au/rx.py`. PASSED on hardware 2026-05-17 first try. Bulk-IN endpoint probe (returned `0x84`), synchronous `dev.read()` polling loop, 24-byte rx_pkt_desc decoder, 8-byte-aligned frame iterator. Hands MPDU to existing `WlanFrameParser`. 27 BSSIDs in 8s; **every burst returned exactly 1 frame** and **every frame parsed successfully** (803/803).
  - [x] **5a — dedicated RX reader thread** (`driver.py`, 2026-05-25). [HW] The original on-loop read+parse dropped ~30% of beacons and only caught ~1-in-5 4-way handshakes in Focus, because the 10 Hz UI render starved the read loop (no `dev.read` posted → dongle FIFO overflow). Moved reads to a dedicated thread (keeps a URB posted always) that hands buffers to the loop via `call_soon_threadsafe`; parse + callback stay on the loop thread. **Confirmed on hardware 2026-05-25**: passive handshake/PMKID capture in Focus is now reliable. Pattern is rtl8821au-only for now; the other 9 drivers share the same on-loop starvation bug and should adopt a shared version of this. See `[[project_rx_loop_ui_starvation]]`.
- [x] **6** — `chips/rtl8821au/chan.py`. PASSED on hardware 2026-05-17 first try (5.5 ms for `set_channel_2g_20mhz(1)`). Ports `switch_channel` + `post_set_bw_mode` + `set_channel_rf` for the channel 1..14 / 20 MHz path. RF read-modify-write via SIPI read (path A, REG_HSSI_READ → REG_SI_READ_A/REG_PI_READ_A) implemented.

### Survey of post-FW init activity (frames 936–10094) — informs M4

| Frame range | Writes | Reads | Bulk-IN | Notes |
|---|---|---|---|---|
| 936 – 3122 | 541 | 548 | 0 | **EFUSE read**: 512× write32 to `REG_EFUSE_CTRL` (0x0030) — *not* LLT (corrected from earlier note). The kernel reads the 512-byte EFUSE here to derive `cut`, `rfe_option`, `btcoex`, ext_lna/pa flags. Drives M4c's `DeviceCond`. |
| 3123 – 10094 | 1600 | 734 | 833 | `airmon-ng start` triggers full chip init: BB/AGC writes in 0x08xx-0x0cxx ranges, plus a **second FW re-upload** at ~frame 4339. RX bulk-IN bytes start flowing here. |

Init tables (tile counts from extractor; tile = 2 u32):

| Table | Tiles | cfg ops (USB, rfe=0) | Dispatch | Notes |
|---|---|---|---|---|
| `rtw8821a_mac`  |  98 |  98 | `write8(addr, data)`                        | no markers |
| `rtw8821a_agc`  | 252 | 130 | `write32(addr, data)`                       | 1 IF/1 ELIF/1 ELSE block on (intf, rfe) |
| `rtw8821a_bb`   | 172 | 172 | `write32`, with delays at addr 0xFA..0xFE   | no markers |
| `rtw8821a_rf_a` | 867 | 279 | `write_rf(path=A, addr, mask=0xFFFFF, data)`, delays at 0xFE/0xFFE | 18 IF blocks branching on (intf, rfe∈{0x00,0x0C,0x10,0x11}) |

The IF/ELIF/ELSE chains in `agc` and `rf_a` branch on **(intf, rfe)** only — no `cut` dependency. For our USB device, `rfe` comes from EFUSE (`ext_lna_2g | ext_pa_2g<<1 | ext_lna_5g<<2 | ext_pa_5g<<3 | btcoex<<4`); until M4b reads it, we use rfe=0 and fall through to the ELSE branches. M4b will cross-check the resolved op stream against capture-1 frames 3123–10094 to pin the actual rfe value.
