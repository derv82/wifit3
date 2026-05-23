# RT2500USB (Ralink RT2570) — ground truth

Userland PyUSB port of the Linux `rt2500usb` kernel module. This doc
accumulates **verified** facts. Citations:
`[SRC]` = kernel source (`data_dumps/rt2x00-source-v6.18/`),
`[WIRE]` = decoded from a pcap (`usb_dumps/captures_rt2500usb/`),
`[HW]` = confirmed on live hardware. Anything not here is a hypothesis.

## Device

- Target: Buffalo "Nintendo Wi-Fi USB Connector".
- VID:PID **0x0411:0x008b** (Melco/Buffalo block of the device table).
  `[SRC]` rt2500usb.c:1932 · `[WIRE]` capture USB descriptors.
- `bcdUSB = 0x0200`. RT2570 is a full-speed (USB 1.1-class) radio. `[WIRE]`
- Silicon: **RT2570**. `[SRC]` rt2500usb.h:11.
- RF chip on this unit: **RF2525E** (EEPROM word 0x0b RF_TYPE field = 5).
  `[WIRE]` capture-2 EEPROM word 0x0b = 0x2815 → bits 15:11 = 0b00101.

## Firmware

**None required.** RT2570 has no firmware blob — all bring-up is register
pokes. `rt2500usb.c` has zero `request_firmware`/`.fw`/`load_firmware`
references, and `rt2500usb_rt2x00_ops` carries no FW hooks (compare
rt73usb/rt2800usb which load `rt73.bin`/`rt2870.bin`). `[SRC]` rt2500usb.c:1824

## USB transport (the whole wire protocol)

Everything is USB **control transfers** via `rt2x00usb_vendor_request`.
`[SRC]` rt2500usb.c:47-94, rt2x00usb.h:42-56 · `[WIRE]` capture control xfers.

| Op | bmRequestType | bRequest | wValue | wIndex | wLength | data |
|---|---|---|---|---|---|---|
| CSR read  (`USB_MULTI_READ`)  | `0xC0` IN  | `7` | `0` | reg addr | `2` | u16 LE |
| CSR write (`USB_MULTI_WRITE`) | `0x40` OUT | `6` | `0` | reg addr | `2` | u16 LE |
| EEPROM read (`USB_EEPROM_READ`) | `0xC0` IN | `9` | `0` | `0` | len | bytes |

- **CSRs are 16-bit** (cf. rt2800's 32-bit). Address goes in **wIndex**,
  `wValue=0`.
- **Multi-byte** writes (MAC addr, keys) use the same req=6 to a base
  offset; chunk to `CSR_CACHE_SIZE` (64 B). `[SRC]` rt2x00usb.c vendor_request_buff
- **EEPROM** is one-shot: `wValue=wIndex=0`, the chip streams the whole
  EEPROM from byte 0. Length = `EEPROM_SIZE` = 0x6e = 110 bytes.
  `[WIRE]` capture-2 frame 149 (wLength=110) → frame 150 (110 data bytes).
- **BBP / RF are indirect** (not directly addressable):
  - BBP: write/read via `PHY_CSR7` (data+reg_id+read_ctrl), busy-poll
    `PHY_CSR8.BUSY`. `[SRC]` rt2500usb.c:122-177
  - RF: low 16 bits → `PHY_CSR9`, high bits + `RF_NUMBER_OF_BITS=20` +
    `RF_BUSY` → `PHY_CSR10`, busy-poll `PHY_CSR10.RF_BUSY`.
    `[SRC]` rt2500usb.c:179-206
  - Busy-poll budget: `REGISTER_USB_BUSY_COUNT` tries, `REGISTER_BUSY_DELAY`
    us apart. (Indirect helpers land in M2 where they get pcap-diffed.)

## EEPROM layout (verified offsets)

Word offsets (16-bit); byte offset in a one-shot read = word × 2.
`[SRC]` rt2500usb.h:635-743 · `[WIRE]` capture-2 frame 150.

- MAC address: word `0x0002` → **byte offset 4**, 6 bytes. capture-2 shows
  the Buffalo OUI **00:0D:0B** there (confirms length + offset math).
- `EEPROM_ANTENNA` word `0x000b`: `RF_TYPE` = bits 15:11.
- `EEPROM_SIZE` = 110 bytes.

> The device's full MAC is deliberately **not** recorded here (user's
> hardware identifier). The M1 test reads/prints it at runtime and only
> asserts it's a valid unicast address.

## Bulk endpoints

- TX data: bulk **EP 0x01 OUT**. RX data: bulk **EP 0x81 IN**.
  `[WIRE]` capture-1 bulk traffic (502 OUT / 11k+ IN).
- Descriptor sizes: TX desc = 5×u32 (20 B), RX desc = 4×u32 (16 B).
  `[SRC]` rt2500usb.h:748-749.
- RX RSSI conversion offset: `DEFAULT_RSSI_OFFSET` = 120. `[SRC]` rt2500usb.h:38

## Capture inventory (usb_dumps/captures_rt2500usb)

All three captures share one timeline (`capture.py`):
insert → `airmon-ng start` (cold init) → channel hop 1–12 →
`aireplay-ng --test` → one deauth (`-0 1`). Frame ranges via
`scripts/pcap_slicer.py` (capture-1): init 57–950, channels 951–4384,
aireplay test 4385–9842, deauth 9843–15248.

- **capture-1**: does NOT contain the probe-time EEPROM/MAC_CSR0 reads
  (capture window opened at the radio-enable phase; lowest read offset is
  0x402 = MAC_CSR1).
- **capture-2 / capture-3**: DO contain the cold-boot probe — 3× EEPROM
  reads (req=9) and the MAC_CSR0 read (wIndex=0x400). Use these for the
  init-sequence pcap-diff in M2.

## Cold-boot init sequence (verified)

`enable_radio` = `init_registers` + `init_bbp`. `[SRC]` rt2500usb.c:956-966

- The `init_registers` op sequence (rt2500usb.c:766-879) matches
  **capture-2 frames 203–299** one-for-one. `[WIRE]` Decoded wIndex map:
  203 USB_DEVICE_MODE → 205 SINGLE_WRITE(0x308←0xf0) → TXRX_CSR2(disable)
  → MAC_CSR13/14 → MAC_CSR1 reset pulse ×2 → TXRX_CSR5-8 → TXRX_CSR19 →
  TXRX_CSR21 → MAC_CSR9 → **MAC_CSR17 ×5 (set_state AWAKE handshake)** →
  MAC_CSR1(HOST_READY) → PHY_CSR2 → MAC_CSR11/22/15/16 → MAC_CSR8 →
  TXRX_CSR0 → MAC_CSR18 → PHY_CSR4 → TXRX_CSR1(end). `init_bbp` starts at
  frame 303 (PHY_CSR7 writes).
- **Prologue arg ordering** (`vendor_request_sw(req, offset, value)`):
  offset→wIndex, value→wValue, no data phase. `[WIRE]` frame 203 = req1
  wValue=4(USB_MODE_TEST) wIndex=1; frame 205 = req2 wValue=0xf0
  wIndex=0x308. `[SRC]` rt2x00usb.h:149-158.
- `MAC_CSR8` max-frame = `DATA_FRAME_SIZE` = 2432 (0x0980). `[WIRE]` frame 283.
- `set_state(AWAKE)` = MAC_CSR17 desire-state write + SET_STATE trigger +
  busy-poll on BBP/RF current-state (the ×5 writes on the wire). `[SRC]` 981-1017
- Version branch: rev = MAC_CSR0 low nibble (this unit `0x5` ≥ VERSION_C 3
  → PHY_CSR2 LNA=0). `[SRC]` rt2500usb.c:1440-1446, 841-849.

## BBP (baseband) — indirect via PHY_CSR7/8

`[SRC]` rt2500usb.c:122-177, 882-951 · `[WIRE]` capture-2 frames 303+.
- Write: busy-poll PHY_CSR8.BUSY, then PHY_CSR7 = DATA(value) |
  REG_ID(word)<<8 | READ_CONTROL=0. Read: same + READ_CONTROL=1, second
  busy-poll, then read PHY_CSR7; failed poll → 0xff.
- 31 fixed writes (frames 311+) match the kernel list 1:1; frame 303 is
  the `bbp_read(0)` from `wait_bbp_ready`. `[WIRE]`
- EEPROM BBP overrides (words 0x0e-0x1d, REG_ID hi byte / VALUE lo byte):
  this unit = BBP[17]=0x30, [21]=0x18, [22]=0x18, [62]=0x00. `[HW]` all
  read back correctly (BBP[0]=0x13). BBP[17/21/22] are readable.

## RF (synthesizer) — write-only via PHY_CSR9/10

`[SRC]` rt2500usb.c:179-206, 582-611 · `[WIRE]` capture-2 frames 1237-1271.
- `rf_write(value)`: busy-poll PHY_CSR10.RF_BUSY, write PHY_CSR9 =
  value & 0xffff, write PHY_CSR10 = (value>>16 & 0xff) |
  NUMBER_OF_BITS(20)<<8 | RF_BUSY. RF has **no read path**.
- This unit is RF2525E → `config_channel` writes RF[2] half-a-band-higher
  first, then RF[1],RF[2],RF[3],RF[4] from `rf_vals_bg_2525e`. `[SRC]` 593-610
- TXpower rides in RF[3] field RF3_TXPOWER (0x3e00, bits 9-13);
  `TXPOWER_TO_DEV` = clamp(0,31). capture used power_level 20.
- Wire-verified encodings: ch1 half-band RF[2]=0x000008aa →
  PHY_CSR9=0x08aa / PHY_CSR10=0x9400; ch1 RF[3]@txp20=0x00062911 →
  PHY_CSR9=0x2911 / PHY_CSR10=0x9406. `[WIRE]`

## RX path — descriptor TRAILS the frame

`[SRC]` rt2500usb.c:1216-1287 · `[WIRE]` capture-2 frame 1453.
- Bulk-IN URB on EP **0x81** = `[802.11 frame (size B)] [align pad] [RXD 16B]`.
  RXD is at `buf[actual_length - 16]`, NOT the front. `[SRC]` 1222-1225
- Decode: `RXD = buf[-16:]`; `size = RXD_W0_DATABYTE_COUNT (bits 16-27)`;
  `frame = buf[0:size]`; `rssi = RXD_W1_RSSI - rssi_offset` (offset 120,
  or EEPROM_CALIBRATE_OFFSET); flags CRC/PHYSICAL/OFDM/MY_BSS in W0.
- Verified: 232-B URB → 215-B CCK beacon, RSSI -46 dBm, 1 pad byte, parses
  via WlanFrameParser as a beacon. `[WIRE]/[HW-pending]`
- `config_ant` (rt2500usb.c:500-580) sets antenna + **RF2525E TX I/Q flip**
  (RX I/Q unflipped); folded into RX bring-up. This unit: antenna word
  0x2815 → tx=A, rx=A. `start_queue(RX)` is just `DISABLE_RX=0`, already
  done by `apply_monitor_filter`.

## TX path — descriptor LEADS the frame

`[SRC]` rt2500usb.c:1056-1211 · `[WIRE]` capture-1 frame 9895 (aireplay deauth).
- Bulk-OUT on EP **0x01** = `[TXD 5×u32 (20B)] [802.11 frame, no FCS]`.
  The chip appends the 4-byte FCS; sequence assigned by HW (TXD NEW_SEQ +
  TXRX_CSR1.AUTO_SEQUENCE).
- 1 Mbps CCK injection TXD (verified byte-for-byte vs the deauth wire):
  `word0 = retry15|NEW_SEQ|count`, `word1 = 0x0000a580` (AIFS2/CWMIN5/
  CWMAX10), `word2 = signal0x00|service0x04|length`, word3=word4=0.
- PLCP length = `(frame_len + 4) * 8` µs (1 Mbps). Deauth(26B) → 240=0x0f0.
- Bulk-OUT length (`get_tx_data_len`): round up to even, and if it's an
  exact multiple of usb_maxpacket (64, full-speed) add 2 — avoids a ZLP.
- TX only behind explicit user action [[passive_by_default]].

## Monitor-mode deviation

The kernel `config_filter` (399-427) sets TXRX_CSR2 DROP_* bits per the
mac80211 filter flags (STA mode). wifit3 is always-monitor, so `mac.py
apply_monitor_filter` opens the filter for real frames from all BSSes
(clear DISABLE_RX + DROP_CONTROL/NOT_TO_ME/TODS/MCAST/BCAST) **but drops
the error classes the RX loop discards anyway**: DROP_CRC=1 +
DROP_PHYSICAL=1 + DROP_VERSION_ERROR=1. Final value **TXRX_CSR2 = 0x0046**.
`[HW]`

Two hardware findings (via `--phase rx` drains) drove this:
- Clearing DROP_PHYSICAL flooded the full-speed bus with PLCP-failure
  garbage — 93% of URBs (6584/7068 in 10s) were multi-KB noise.
- With DROP_PHYSICAL fixed, ~45% of URBs were still large FCS-fail noise
  (bogus length, invalid frame types — a one-off back-walk diagnostic
  confirmed these are single corrupt frames, **not** coalesced multi-frame
  transfers). Since the RX loop drops every FCS-fail frame in software,
  DROP_CRC=0 was pure
  bus cost; DROP_CRC=1 reclaims it with zero output change.
See [[feedback_monitor_mode_deviation]].

## Milestones

- **M1** [DONE, hw-verified]: `transport.py` + `constants.py` + `--phase
  open`. Live: MAC_CSR0=0x0005, MAC OUI 00:0D:0B, RF2525E.
- **M2a** [DONE, hw-verified]: `mac.py` init_registers + set_state(AWAKE)
  + warm probe + monitor filter. Live: COLD→AWAKE, TXRX_CSR2=0x0000,
  HOST_READY latched, WARM detected on re-run.
- **M2b** [DONE, hw-verified]: `bbp.py` indirect helper + init_bbp.
  Live: BBP[0]=0x13, all EEPROM override readbacks (17/21/22) matched.
- **M2c** [DONE, hw-verified]: `chan.py` rf_write + config_channel +
  RF2525E/RF2525 tables. Live: 14/14 channels tuned, PLL lock on all
  (PHY_CSR10=0x5400).
- **M3** [DONE, hw-verified]: `rx.py` (RXD-at-end decode + EP 0x81 bulk
  loop) + config_ant. Live ch1: 309 frames / 15 BSSIDs, RSSI -89..-29.
  Monitor filter tuned to TXRX_CSR2=0x0044 (drop PLCP noise).
- **M4** [DONE, TUI-verified]: `driver.py` (WlanDriver Protocol, async RX
  loop, warm-reattach smoke test) + manager registration. Appears + scans
  in the TUI.
- **M5** [DONE, hw-verified]: `tx.py` inject_frame (1 Mbps CCK TXD, EP
  0x01). TXD matches capture deauth byte-for-byte. Live: deauth burst via
  `iface.deauth()` kicked the client and recaptured EAPOL M1+M3 **and a
  PMKID** on the same radio. 21 mocked unit tests (incl. the
  inject-frame run_in_executor path). `--phase deauth --bssid X --client Y`.

**Full passive + active stack complete.** The Buffalo "Nintendo Wi-Fi USB
Connector" (RT2570) scans, monitors, channel-hops, and deauths from the
TUI — a no-firmware, register-only userland port.

## Test harness

`scripts/rt2500usb/test_hw_rt2500usb.py --phase {open,macinit,bbpinit,
chaninit,rx} [--channel N] [--debug]` — incremental hardware bring-up.
`tests/chips/rt2500usb/test_driver.py` — mocked unit tests (no hardware;
synthetic MAC/BSSID fixtures, never the captured device's identifiers).
