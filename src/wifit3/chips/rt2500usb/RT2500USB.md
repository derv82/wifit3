# RT2500USB (Ralink RT2570) — ground truth

## Potential Known Gaps

Cross-driver gap classes (project audit 2026-05-25).

- [x] **RX polling loop drops frames** — fixed via the shared RxReaderThread
  (`driver.py` `_rx_read_once`/`_rx_dispatch`), off the event loop.
- [x] **RX filter / monitor mode (ToDS capture)** — HANDLED. `mac.py:config_filter`
  clears `TXRX_CSR2` DROP_TODS (0x20) + DROP_NOT_TO_ME (0x10) → client→AP frames
  accepted; final 0x0046 (drop CRC/PHYSICAL/VERSION). This value **matches the
  kernel's airmon wire**, not a deviation — see "Monitor RX filter" below.
- [x] **AGC never seeded (the weak/inconsistent-RX root cause)** — FIXED 2026-06-11.
  The port skipped `reset_tuner`, leaving BBP R17 (the VGC/variable gain) at the
  init value 0x30 where the kernel runs 0x3b, never re-seeding per hop. See
  "Faithful re-port" below.

## Faithful re-port — RX AGC fix + full-walk gate (2026-06-11)

The port reproduced only `init_registers` + `init_bbp` (123 of 3215 control ops);
everything operational — antenna, channel tune, the AGC seed, the LED — was
unverified, and the AGC seed was **missing entirely**.

- **Root cause of weak/inconsistent RX: missing `reset_tuner`.** rt2x00 calls
  `rt2500usb_reset_tuner` on every `CONF_CHANGE_CHANNEL` (`rt2x00lib_config`) and
  on antenna config — unconditionally, monitor mode included (`rt2x00link.c`:
  the `intf_sta_count` gate is only on the *periodic* tuner, which rt2500usb
  doesn't have). It seeds BBP R24/R25/R61/**R17** from the per-card EEPROM
  BBP-tune words (0x31-0x34). Our port never ran it → BBP R17 stuck at 0x30 (vs
  the kernel's 0x3b on this unit), never re-seeded per hop. `[SRC]` rt2500usb.c:689-712.
  Ported to `bbp.reset_tuner`; wired into `monitor.tune_hop` so it re-seeds every hop.
- **Full single-cursor gate.** `scripts/rt2500usb/verify_pcap.py` is now ONE
  monotonic walk over the whole capture (init → airmon monitor entry → every hop),
  the rt3070 shape. Reproduces **100%** of the control conversation with ZERO
  waivers: 3205/3205 (new capture-2), 3201/3201 (new capture-1), 1687/1687 (old
  capture-2/3). Old capture-1 lacks the probe reads (not anchorable).
- **Operational sequence** (`monitor.py`, shared by driver + gate), the rt2x00
  call order confirmed against the wire:
  - `enable_monitor` = `led_enable` (MAC_CSR20 radio+activity) + `start_queue_rx`
    (rt2x00lib_enable_radio tail) → `config_filter(¬mon)` → `initial_config`
    (stop_rx, `config_txpower` rf3=0, `config_ps`, `config_ant`, `reset_tuner`,
    start_rx) → `config_filter(mon)`.
  - `tune_hop` = one `rt2x00mac_config(CHANGE_CHANNEL)`: stop_rx → `config_channel`
    + `reset_tuner` → `config_ant` + `reset_tuner` → start_rx.
  - `config_intf` is **absent**: a monitor vif programs no MAC/BSSID/beacon-sync
    (zero MAC_CSR2/CSR5/TXRX_CSR18-20 writes on the wire).
- **Stability hardening** (the ~1-min RF-death wedge): `transport._ctrl` adds a
  bounded transient-error retry (3 attempts, backoff) — a full-speed control op
  racing the bulk-IN reader can fast-fail with a transient pipe error; `driver`
  gained the rt3070 `_io_lock` (asyncio) + `_hw_lock` (threading) so a cancelled
  tune's draining thread can't collide with a new tune/inject on the control endpoint.
- **[HW] 2026-06-11** — cold bring-up + RX healthy: best AP **9.2 beacons/s** (median
  9, max 10, **0 dead seconds**), 10+ APs received on CH1 (184/168/167/138/129…
  beacons/20s each). Transformed from the grade-D "one AP 0/s" inconsistency.
  - **Soak:** 5-min 14-channel hop (0.25s) — no wedge, no RF death; breadth flat-
    to-rising (48→56 active BSSIDs), ~830-950 frames/window, no degradation. The
    old grade-D died after ~1 min. Connected **WARM** (re-arm + tune, no replug)
    and sustained the whole run — warm reattach validated.

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

## Monitor RX filter (matches the kernel wire)

`mac.py config_filter(monitoring)` ports `rt2500usb_config_filter` (399-427)
with the FIF_* flags airmon sets: FIF_FCSFAIL / FIF_PLCPFAIL **off** (so the
chip drops CRC + PLCP errors — the RX loop discards them anyway, and on this
full-speed bus surfacing them floods it), FIF_CONTROL + FIF_ALLMULTI on,
VERSION_ERROR always dropped, BROADCAST always accepted. `monitoring=True`
clears DROP_NOT_TO_ME + DROP_TODS so client→AP (ToDS) frames from every BSS
arrive. Final value **TXRX_CSR2 = 0x0046** — and the cold-boot capture's airmon
path writes the **same** value (decoded at ops 167-168), so this is **faithful
to the wire, not a deviation** (the earlier "monitor-mode deviation" framing was
wrong: the kernel's airmon filter drops PLCP/CRC too). DISABLE_RX is owned by
`start_queue_rx` / `stop_queue_rx`, which bracket every config. `[WIRE]/[HW]`

**[HW] 2026-05-31 — ToDS RX confirmed working; instability is the real problem.**
A full attack pass showed the monitor filter is correct: the session log carried
`[RXFRAME] data to_ds=True from_ds=False <client> -> <bssid>` lines, so client→AP
(ToDS) frames ARE delivered — *not* the filter gap one might suspect from the
symptom. But the run was unstable and ended badly:
- **Handshake captured only M1+M3 (FromDS), never M2+M4 (ToDS)** → no crackable
  pair. Since ToDS reception works, the likely cause is weak/unstable RX + the RF
  death below cutting the window short, not a filter problem.
- **RF died after ~1 min** under sustained load: no beacons from any AP, with
  `rt2500usb-rx read failed: [Errno 32] Pipe error` and
  `rt2500usb set_channel(N) failed: [Errno 32] Pipe error` repeating. The bulk-IN
  pipe wedges. This is the headline RT2500USB bug to chase (USB pipe stall /
  channel-set against a wedged pipe). Stress soak can't pass until it's fixed.
- **Weak/odd RX**: one CH1 AP gave ~10 beacons/s while another CH1 AP gave 0/s
  consistently; ARP replay only ~1–3 IVs/s; ChopChop stalled (~32 B left). All
  point at RX sensitivity/stability on this older hard-MAC part, not the filter.
- WPS PIN exchange works (valid first-half-wrong NACKs); PBC timed out (weak RX).
  PMKID + deauth fine.

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

- **M6** [DONE 2026-06-11, gate + hw-verified]: faithful operational re-port.
  `reset_tuner` (AGC seed), `config_txpower`/`config_ps`/`config_filter`/LED/
  queue-toggle in `mac.py`/`chan.py`, the `monitor.py` orchestration
  (enable_monitor + tune_hop), and the full single-cursor `verify_pcap` (100%,
  zero waivers). Driver runs the faithful path; stability hardened
  (`_io_lock`/`_hw_lock` + transport bounded-retry). See "Faithful re-port" above.

**Full passive + active stack complete.** The Buffalo "Nintendo Wi-Fi USB
Connector" (RT2570) scans, monitors, channel-hops, and deauths from the
TUI — a no-firmware, register-only userland port, now byte-faithful to the
kernel's whole control conversation.

## Test harness

`scripts/rt2500usb/test_hw_rt2500usb.py --phase {open,macinit,bbpinit,
chaninit,rx} [--channel N] [--debug]` — incremental hardware bring-up.
`tests/chips/rt2500usb/test_driver.py` — mocked unit tests (no hardware;
synthetic MAC/BSSID fixtures, never the captured device's identifiers).
