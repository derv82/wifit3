# rt2800usb family — Ground-Truth Doc

## Potential Known Gaps

Cross-driver gap classes (project audit 2026-05-25).

- [~] **RX polling loop drops frames** — PORTED to the shared RxReaderThread
  (awaiting HW verify). Was the gap: `driver.py:_rx_loop`
  (~470) does `await loop.run_in_executor(read_rx_burst)` then parse on the
  event loop — no read posted while parsing. Same pattern as rtl8821au pre-fix.
  Fix: dedicated reader thread + queue hand-off (rtl8821au commit 2e3a7a7).
- [x] **RX filter / monitor mode (ToDS capture)** — ALREADY HANDLED (offline
  verdict). `mac.py:302-315` writes `RX_FILTER_CFG (0x1400) = 0x00000011` —
  only DROP_CRC_ERR | DROP_VER_ERR; **DROP_NOT_TO_ME (bit2) is CLEAR**, so the
  chip is promiscuous and accepts unicast not addressed to us, incl. client→AP
  (ToDS). Explicit monitor deviation from the kernel STA filter. [SRC
  rt2800_config_filter]
- [~] **RX-AGC link tuner missing → weak/unstable RX** (added 2026-06-01;
  PORTED, awaiting HW verify). Symptom on PAU05: beacon rate wanders
  (1–3/s → 7–8/s → 4–5/s) with periodic ~zero gaps every ~10–15 s, and a
  *strong near* AP (~15 ft) comes in worse (~3/s) than a *weak far* one
  (neighbour, steady 6–7/s) — the signature of front-end overload, not a
  detune. Root cause: BBP66 (the RX VGC / AGC gain) is seeded once per
  channel tune (`chan.py:1009`, 2.4 GHz = `0x1c + 2*lna_gain`) and never
  adapted, so we sit permanently at the most-sensitive seed. The kernel runs
  a ~1 Hz link tuner that *raises* VGC when the averaged RSSI is strong
  (default case: `rssi > -80 → vgc += 0x10`), preventing the overload.
  Fix: ported as `link_tuner.py` + a 1 s background task in `driver.py`
  (`_link_tuner_loop`/`_link_tuner_tick`), fed from received-frame RSSI.
  **Monitor-mode deviation** (the kernel disables the tuner for a pure-monitor
  interface and feeds it only from associated-BSS beacons): we keep the
  algorithm verbatim but source RSSI from every good frame, and it can only
  de-sensitise on strong signals — weak-signal sensitivity is never reduced.
  Resets on every channel change. [SRC] rt2800lib.c:5723 (get_default_vgc),
  :5759 (set_vgc), :5787 (link_tuner); rt2x00link.c:341 (1 Hz work),
  :228 (monitor skip), :314 (DEFAULT_RSSI fallback); rt2800lib.c:12085
  (CAPABILITY_LINK_TUNING always set). Ties to VERIFICATION.md PAU05 Scan ⚠️
  and the cross-card weak-2.4 GHz-RX item in `planning/PORTING.md`.
- [x] **EFUSE reader uses a u16-word ADDRESS_IN** — FIXED (2026-06-14). `eeprom.py`
  `_efuse_read_chunk` writes `EFUSE_CTRL_ADDRESS_IN = byte_offset // 2` (kernel
  `rt2800lib.c:10955` loops `i += 8` in word units). Reproduces the kernel EFUSE walk
  byte-for-byte (`verify_pcap`, 225 ops, rt5372 + rt5572 captures); post-fix RX A/B shows
  no regression — see "EFUSE addressing" below.

Covers Ralink rt2800usb-family chipsets supported by wifit3:

| Silicon | Marketing | Dongle               | USB ID    | Bands       | Chains | Status |
|---------|-----------|----------------------|-----------|-------------|--------|--------|
| 0x5392  | RT5372    | Panda PAU05          | 148f:5372 | 2.4 GHz     | 1T1R   | DONE   |
| 0x3572  | RT3572    | ALFA AWUS051NH v2    | 148f:3572 | 2.4 + 5 GHz | 2T2R   | DONE   |
| 0x5592  | RT5572    | Panda PAU09 N600     | 148f:5572 | 2.4 + 5 GHz | 2T2R   | DONE   |

Note: USB PID is named after the marketing SKU (e.g. RT5372), but the
on-chip MAC_CSR0 reports the silicon ID (e.g. RT5392) — the two diverge
on all three rebrands.

Every fact below is either:
- **[SRC]** a direct citation from `data_dumps/rt2x00-source-v6.18/`, or
- **[WIRE]** verified on real hardware via `scripts/rt2800usb/test_hw_rt2800usb.py`.

Anything *not* in this document is a hypothesis. Update as facts get
verified — do not let speculation accumulate here.

---

## Test hardware (per silicon)

- **PAU05** (silicon 0x5392 rev 0x0223): N150 single-band 2.4 GHz,
  1T1R. USB 2.0 high-speed. Reference chip for the family-wide MAC /
  FW / RX/TX desc / EFUSE bring-up.
- **AWUS051NH v2** (silicon 0x3572): RT3572 + RF3052. 2T2R-capable
  2.4 + 5 GHz silicon, USB 2.0. **This unit's EFUSE is erased** —
  identity is programmed (chip ID, MAC 00:c0:ca:88:10:2d) but the
  RF/cal region reads 0xFF and NIC_CONF0=0x0000. On an unburned EFUSE
  the kernel runs it as **1T1R** (RFCSR1=0xf1) and its rx-filter
  calibration is **degenerate** — see "RT3572 unburned-EFUSE behaviour"
  below. The cal loop (`init_rfcsr_3572` → `_rx_filter_calibration_3572`)
  still produces the per-bw RFCSR24/31 values replayed on every channel
  tune; we run it faithfully and accept the rail it returns.
- **PAU09 N600** (silicon 0x5592 rev 0x0222 = REV_RT5592C+): RT5572 +
  RF5592. 2T2R 2.4 + 5 GHz. USB 2.0. EFUSE NIC_CONF0=NIC_CONF1=0x0F0F
  (unburned, factory-test pattern — handled by
  `EepromValues._nic_conf0_looks_unburned()`; see
  [[feedback_pau09_efuse_0f0f]]). MAC_DEBUG_INDEX.XTAL surfaces 20-
  vs 40-MHz xtal selection (RF5592-specific). Per-tune IQ calibration
  via BBP158/159 indirect pair runs on every channel set.

The driver claims all three VID:PIDs and dispatches per `silicon_id`
read from MAC_CSR0 at connect time.

---

## Vendor control transfer protocol

[SRC] `rt2x00usb.h:42-58`, `rt2x00usb.c:45-80`

| Field           | Value                          |
|-----------------|--------------------------------|
| `bmRequestType` | `0xC0` (vendor IN) / `0x40` (vendor OUT) |
| `bRequest`      | 6/7 for `USB_MULTI_WRITE`/`READ` (register access) |
|                 | 2/3 for `USB_SINGLE_WRITE`/`READ` (byte access) |
|                 | 1 for `USB_DEVICE_MODE` (resets + FW boot signal) |
|                 | 9 for `USB_EEPROM_READ` |
| `wValue`        | **0** for register access (kernel passes `value=0`) |
| `wIndex`        | **register address** (kernel passes `offset=addr`) |

**Critical gotcha**: kernel `rt2x00usb_vendor_request` is declared
`(u16 offset, u16 value)` but the wire goes `(value=wValue,
offset=wIndex)` — see [[rt2x00usb_csr_cache_size]] memory. We verified
this empirically: any other ordering returns `0x00020208` for every
address (stale word, not the actual register).

**Multi-byte transfers MUST be chunked to 64 bytes** per control
transfer (kernel `CSR_CACHE_SIZE`). A single 4-KB FW upload silently
fails or pipe-stalls — chunking to 64×64 fixes it. The address (wIndex)
advances 64 bytes per chunk. This is implemented in
`transport.write_multi` / `read_multi`.

---

## Bring-up flow (cold)

```
connect()
  ├─ claim USB interface
  ├─ read_chip_id              MAC_CSR0 decode → silicon_id + revision  [M1]
  ├─ read_perm_mac             MAC_ADDR_DW0/DW1 read (cold = zeros)    [M1]
  ├─ is_chip_warm              PBF.READY + ~pre_init_bit13              [M1]
  ├─ load_firmware             rt2870.bin / rt5572.bin upload via 64B
  │                            chunks → USB_MODE_FIRMWARE → wait
  │                            PBF_SYS_CTRL.READY → MCU_BOOT_SIGNAL    [M2a]
  ├─ usb_init_registers        clear PBF pre-init bit 13 + USB_MODE_RESET
  │                            + MAC_SYS_CTRL reset                    [M2b-1]
  ├─ read EFUSE                512-byte EFUSE dump → MAC, freq_offset,
  │                            lna_gain_bg/a, NIC_CONF0/1, IQ cal      [M2b-2]
  ├─ init_registers            ~50 MAC config writes (basic rates,
  │                            BCN_TIME, TX/RX timing, retry, prot,
  │                            LED, USB DMA, MCS fallback)             [M2b-3]
  ├─ init_bbp(silicon_id)      per-silicon dispatcher:
  │                              0x5392 → init_bbp_53xx (~30 BBP writes)
  │                              0x3572 → init_bbp_3572 (~18 BBP writes)
  │                              0x5592 → init_bbp_5592 (+ GLRT 70-byte
  │                                       table via BBP195/196 indirect) [M2b-4]
  ├─ init_rfcsr(silicon_id)    per-silicon dispatcher:
  │                              0x5392 → init_rfcsr_5392 (56-entry table)
  │                              0x3572 → init_rfcsr_3572 (30-entry table
  │                                       + rx_filter_calibration loop
  │                                       → RfFilterCal for chan tune)
  │                              0x5592 → init_rfcsr_5592 (21-entry table
  │                                       + rev-gated extras)            [M2c]
  ├─ enable_radio              MAC TX/RX + WPDMA + USB DMA + RX filter
  │                            (RT3572 also fires MCU_CURRENT)          [M3]
  ├─ set_channel(silicon, ch)  per-silicon channel tune (see below)     [M4]
  └─ start RX loop             async bulk-IN poll                       [M3]
```

All milestones M1-M4 [WIRE]-verified on all three silicons. See the
git log for per-milestone hw outcomes (commits 2ec75b2, 32bffbb,
b19cad3, 8080a02, 217135a). M5 (TX inject) verified on RT5392 (deauth
→ EAPOL re-capture).

---

## Firmware

[SRC] `rt2800usb.c:205-265` (`rt2800usb_write_firmware`)

- **Blob**: `assets/rt5572.bin` — 4096 bytes. Single FW image shared
  across all rt2800usb-family chips (kernel `firmware/rt2870.bin` is
  the same blob doubled: PCI half + USB half; we ship the USB half).
- **CRC**: trailing 2 bytes = CRC-CCITT (LSB-first, reversed poly
  `0x8408`, init `0xFFFF`) over the first 4094 bytes, then `swab16`.
  Our `_crc_ccitt` matches the Linux `crc_ccitt` lib function (NOT
  the MSB-first "CCITT-FALSE/XModem" variant — easy to confuse).
- **Upload destination**: `FIRMWARE_IMAGE_BASE` = `0x3000` via
  `USB_MULTI_WRITE` (chunked 64B per control transfer per
  [[rt2x00usb_csr_cache_size]]).
- **Execution kick**: `USB_DEVICE_MODE` vendor request with
  `wValue=USB_MODE_FIRMWARE=8`, `wIndex=0`.
- **Boot signal**: `MCU_BOOT_SIGNAL=0x72` via `mcu_request` (writes
  H2M_MAILBOX_CSR doorbell + HOST_CMD_CSR command).

End-to-end FW upload time on hw: **~30 ms** (64 sub-chunks × 64 bytes
plus ACK polling).

---

## RX descriptor layout

[SRC] `rt2800usb.c:481-518`, `rt2800lib.c:900-942`

```
URB: [RXINFO (4B)] [RXWI (16B for RT539x, 24B for RT5592)]
     [802.11 frame] [L2 pad] [pad] [RXD (4B)] [USB pad]
                   |<--- rx_pkt_len = RXWI + frame + L2 pad + pad --->|
```

- **RXINFO_W0[15:0]** = `rx_pkt_len` (covers RXWI through pad, NOT RXD)
- **RXWI_W0[27:16]** = `MPDU_TOTAL_BYTE_COUNT` (actual 802.11 frame
  length, including FCS)
- **RXWI_W1[22:16]** = MCS, **W1[23]** = BW40 flag, **W1[31:30]** = PHYMODE
- **RXWI_W2[7:0/15:8/23:16]** = signed per-path RSSI bytes (paths 0/1/2)
- **RXD_W0[8]** = CRC_ERROR

**RSSI formula** [SRC `rt2800lib.c:856-898`]:
```
rssi = base_val - eeprom_offset - lna_gain - rssi_raw_byte
base_val = -12 (all chips except RT6352)
```
We currently use `eeprom_offset = lna_gain = 0` and pick the max
across the three paths. EEPROM-aware RSSI lands when EEPROM bring-up
ports (will share work with the per-channel TX power tables).

**L2 padding**: the hw inserts 2 bytes between the MAC header and the
payload when the header length isn't 4-aligned — every QoS-Data frame
(the EAPOL carrier; beacons have a 4-aligned 24-byte header and aren't
padded). `RXD_W0_L2PAD` flags it, and `parse_rx_urb` removes the pad
*before* trimming to `MPDU_TOTAL_BYTE_COUNT`. Trimming first clips the
last 2 body bytes (an EAPOL M2's key_data tail) — surfacing as "EAPOL
clipped" + an uncrackable handshake. [SRC] rt2800usb.c:565,
rt2x00queue.c rt2x00queue_remove_l2pad.

`MPDU_TOTAL_BYTE_COUNT` excludes the FCS for these frames (the chip
strips it), so `parse_rx_urb` trims to it directly and removes no
trailing bytes.

---

## TX descriptor layout

[SRC] `rt2800lib.c:795-853`, `rt2800usb.h:46-51`

```
URB: [TXINFO (4B)] [TXWI (16B for RT539x, 20B for RT5592)]
     [802.11 frame] [pad to 4-byte alignment]
     |<------ pkt_len = TXWI + frame + pad (NOT TXINFO) ------>|
```

- **TXINFO_W0[15:0]** = `USB_DMA_TX_PKT_LEN`
- **TXINFO_W0[24]** = `WIV` (set to 1)
- **TXINFO_W0[26:25]** = `QSEL` (we use 2 = EDCA)
- **TXWI_W0[22:16]** = MCS, **W0[31:30]** = PHYMODE
- **TXWI_W1[0]** = ACK bit (cleared for no-ack inject)
- **TXWI_W1[1]** = NSEQ (we set to 0 — use the frame's seqctl)
- **TXWI_W1[15:8]** = WIRELESS_CLI_ID (we use 0)
- **TXWI_W1[27:16]** = `MPDU_TOTAL_BYTE_COUNT`
- **TXWI_W2/W3** = zero (IV/EIV)

Zeroing W2/W3 is only safe because the TX crypto engine is disarmed at
init — `reg_init` step 23 clears SHARED_KEY_MODE, WCID/WCID_ATTR and
IVEIV. If any cipher table is left set, the engine encrypts a Protected
(WEP) inject and inserts a zeroed IV from W2/W3 over the frame's real
IV, so the AP's ICV check silently drops every replay (TX_STA_FIFO
still flags TX_SUCCESS). [HW] WEP ARP replay was dead on the RT5572 —
dual-NIC sniff showed on-air IV=00:00:00 and zero AP rebroadcasts —
until the SHARED_KEY_MODE clear landed; with it, ~4000 AP rebroadcasts
per 30 s and chop/frag work too. [SRC] `rt2800lib.c:6241-6257`

**Bulk-OUT EPs** by kernel queue mapping (rt2800usb.c):

| EP   | Queue   | Use |
|------|---------|-----|
| 0x01 | AC_BK   | Background |
| 0x02 | AC_BE   | Best-effort (normal data) |
| 0x03 | AC_VI   | Video |
| 0x04 | AC_VO   | Voice |
| 0x05 | HCCA    | HCCA controlled access |
| 0x06 | MGMT    | Management frames (where our injects go) |

We default `inject_frame` to EP 0x06 with `QSEL=MGMT`. EDCA-OUT EPs
(0x02) work for unicast data if needed.

---

## Channel tune

Per-silicon dispatcher in `chan.py:set_channel(silicon_id, channel)`:

| Silicon | RF chip | Function           | Bands       | Channel table              |
|---------|---------|--------------------|-------------|----------------------------|
| 0x5392  | RF53xx  | `_set_channel_5392`| 2.4 GHz     | `_RF_VALS_3X` (1-14)       |
| 0x3572  | RF3052  | `_set_channel_3572`| 2.4 + 5 GHz | `_RF_VALS_3X` (1-14, 36-173) |
| 0x5592  | RF5592  | `_set_channel_5592_{2g,5g}` | 2.4 + 5 GHz | `_RF_VALS_5592_XTAL{20,40}` |

**RF53xx + RF3052** share the 3-field `(rf1, rf2, rf3)` synth encoding
that goes into RFCSR8/9/11 (RF53xx) or RFCSR2/6/3 (RF3052).

**RF5592** uses a different 5-field `(channel, N, K, mod, R)` synth
encoding packed into RFCSR8/9/11 via bitfield masks (RFCSR9.K + .N +
.MOD, RFCSR11.R + .MOD). Two channel tables (xtal20 / xtal40) — picked
at runtime from `MAC_DEBUG_INDEX.XTAL` because the RF5592 PCB can be
fitted with either crystal.

[SRC] `rt2800lib.c:3387-3483` (config_channel_rf53xx),
      `rt2800lib.c:2547-2795` (config_channel_rf3052),
      `rt2800lib.c:3485-3758` (config_channel_rf55xx),
      `rt2800lib.c:11435-11707` (channel tables).

**Per-tune IQ calibration** (RF5592-only): kernel writes 6 BBP158/159
indirect pairs on every channel set (TX0/TX1 × gain/phase × per-band
selection + 2 global RF IQ comp/imbal bytes). EFUSE bytes at
0x130-0x14F. We do this on both 2.4 GHz and 5 GHz tunes — even though
the bytes are typically 0xFF (unburned) on retail dongles, the
`0xFF → 0` kernel fallback keeps the chip happy. See
[[feedback_rt5592_chip_auto_managed_rfcsr]] for why RFCSR readback
post-init doesn't always match what we wrote.

**Channel-edge tweaks** (RF5592, 5 GHz): kernel has 5 per-channel
breakpoints at ch 50/52, 116/118, 124/126, 128/130, 138/140, 153/155
for RFCSR23/24/51/55/56/58/59/62. All ported verbatim in
`_set_channel_5592_5g` — see [[feedback_port_all_cases]].

---

## Warm reattach

Currently always cold-restarts. The `is_chip_warm` heuristic
correctly distinguishes:
- **Fresh plug**: `PBF_SYS_CTRL = 0x00002F80` — READY+pre_init both set → COLD
- **Post-init_registers**: pre_init bit 13 cleared → WARM

But we don't yet take the warm short-circuit path. Driving on top of
a warm chip would skip FW upload + init and just resume bulk-IN
polling — same pattern as `[[feedback_warm_reattach]]` from RTL8821AU.

---

## TX inject: the three follow-on bugs

Once RX worked, TX inject (`iface.deauth`) hit `errno 10060`
(Operation timed out) on bulk-OUT. Three additional bugs from reading
the kernel's `rt2800usb_get_tx_data_len` (rt2800usb.c:440-451) and
`rt2800usb_write_tx_desc` (rt2800usb.c:411-427):

1. **Mandatory 4-byte USB end pad.** Kernel returns
   `roundup(skb_len, 4) + 4`. The `+ 4` is a trailing pad after the
   frame-alignment pad. Without it the chip silently drops the bulk-
   OUT write (host controller forwards, chip never ACKs). Layout:

       [TXINFO 4B] [TXWI 16/20B] [802.11 frame] [4-byte align pad]
       [4-byte USB end pad]

2. **`QSEL = 2` (EDCA), not 0 (MGMT).** Kernel hardcodes QSEL=2 for
   every frame including management — no separate MGMT QSEL on the
   data path.
3. **Bulk-OUT EP = `0x02` (AC_BE), not `0x06` (MGMT).** Kernel routes
   all TX through AC queues; the MGMT endpoint is for chip→host TX
   status, not host→chip submission.

**Verified live:** with all three fixes in, `iface.deauth(ap, sta)`
against NETGEAR2G successfully deauths an iPhone → triggers reconnect →
captures EAPOL M2+M3 of the new 4-way handshake. Full attack chain
working end-to-end.

---

## EFUSE: the multi-hour blocker (and the actual fix)

M3 (RX) wouldn't deliver a single bulk-IN URB until EFUSE bring-up
landed. Symptom: full bring-up completed cleanly, every diagnostic
register read showed the expected value, but bulk-IN was silent.

Real cause: chip per-unit calibration values that gate the BBP→RF
chain. Specifically (most→least critical for our PAU05):

- **`freq_offset = 30`** (EEPROM word 0x1D low byte) — chip crystal
  oscillator offset. Without it the chip tunes a fraction of a MHz
  off-channel and the BBP never locks onto preambles → no frames
  decoded → bulk-IN silent. **The actual RX gate.** Fed via
  `MCU_FREQ_OFFSET` MCU command inside `chan.freq_cal_mode1_usb`.
- **`lna_gain_bg = 0xFF`** (EEPROM word 0x22 low byte) — LNA gain
  compensation; subtracted from `0x37` for BBP62/63/64 noise-floor
  writes (`bbp_write(t, 62, 0x37 - lna_gain)`). PAU05 has 0xFF here
  ("not calibrated") which underflow-wraps to 0x38 vs the chip default
  0x37. Probably not gating on its own.
- **`mac_address = 9c:ef:d5:fd:78:b8`** (Foxconn OUI, EEPROM words
  0x02-0x04) — chip RX matching engine needs identity in
  `MAC_ADDR_DW0/DW1`. Worked around earlier with a fake MAC +
  `UNICAST_TO_ME_MASK=0xFF`, but using the real EFUSE MAC is correct.

Discovery method: extracted the kernel's control-transfer sequence
from `usb_dumps/captures_rt2800usb_rt5372/capture-1.pcap` using
`scripts/rt2800usb/rt2800_ctrl_diff.py`. The first ~250 vendor requests are
all EFUSE reads (32 iterations × 7 ctrl xfers each, hitting
`EFUSE_CTRL=0x0580`). Wifit3 was doing 0 of them.

**EFUSE_CTRL protocol** (see `eeprom.py`):

    for byte_offset in range(0, 512, 16):
        # 1) Request a 16-byte read (ADDRESS_IN is a u16-word index)
        reg.ADDRESS_IN = byte_offset // 2; reg.MODE = 0; reg.KICK = 1
        write(EFUSE_CTRL, reg)
        # 2) Poll KICK clear (~5µs)
        # 3) Read 4 dwords HIGH→LOW: EFUSE_DATA3, _2, _1, _0
        #    Each LE into eeprom[byte_offset .. +16]

**EEPROM word layout** (rt2800lib.c:308-347 `rt2800_eeprom_map`):

| word | byte | content |
|------|------|---------|
| 0x02 | 0x04 | MAC bytes 0-1 |
| 0x03 | 0x06 | MAC bytes 2-3 |
| 0x04 | 0x08 | MAC bytes 4-5 |
| 0x1A | 0x34 | NIC_CONF0 (TX/RX path counts) |
| 0x1B | 0x36 | NIC_CONF1 (BT coex, ant div) |
| 0x1D | 0x3A | FREQ (freq_offset in low byte) |
| 0x22 | 0x44 | LNA (lna_gain_bg in low byte) |
| 0x23 | 0x46 | RSSI_BG (per-path RSSI offsets) |

---

## EFUSE addressing

`EFUSE_CTRL.ADDRESS_IN` is a u16-word index: `_efuse_read_chunk` writes `byte_offset // 2`
(kernel `rt2800lib.c:10955` loops `i += 8` in word units; the 16-byte result is stored back
at `byte_offset`). The reader reproduces the kernel EFUSE walk byte-for-byte — `verify_pcap`
matches 225 ctrl ops on both the rt5372 and rt5572 captures.

Post-fix RX (2026-06-14, CH1 nearby AP, 3×20 s, fresh replug each): PAU09/RT5572 ~8.7 median 9,
AWUS051NHv2/RT3572 ~8.0 median 8 — no regression; 30-min dual-band hop soak on the PAU09 flat
(active BSSIDs 121→124, no wedge). No-op on the erased-EFUSE RT3572 (all-0xFF reads identically
by byte or word).

## RT3572 unburned-EFUSE behaviour

The AWUS051NH v2 test unit shipped with an **erased EFUSE**: identity is
programmed (chip ID, MAC), but the RF/calibration region reads 0xFF and
`NIC_CONF0 = 0x0000`. The kernel still runs its normal init + cal paths
over the uncalibrated silicon, which produces several non-obvious
behaviours we match. (An unburned EFUSE on a retail card is itself
suspect — a QC miss or counterfeit; "unburned RT3572" may be a whole
class of units.)

### Chains: 1T1R, not 2T2R

The silicon is 2T2R-capable, but on the erased EFUSE the kernel runs a
single chain. [WIRE] `aireplay.pcap` writes `RFCSR1 = 0xf1`
(`RX1_PD|TX1_PD|RX2_PD|TX2_PD` set — chains 1+2 powered down, only
chain 0 live). So `EepromValues.{tx,rx}path` default the unburned case
to **1 TX / 1 RX**, and `config_channel` lights a single PA
(`PA_PE_G0`). An earlier "force 2T2R" override was a workaround for our
own DAC-gate bug (next) — not faithful, removed.

### DAC1/ADC1 gate reads the RAW NIC_CONF0 field

`rt2800_disable_unused_dac_adc` ([SRC] rt2800lib.c:6434-6446) powers
DAC1 down only when the NIC_CONF0 **TXPATH field == 1** (ADC1 when
**RXPATH == 1**) — gated on the raw EEPROM field, not a validated chain
count. On the erased EFUSE both fields read 0, so the kernel powers down
**neither**. `driver.py` passes the raw fields (`(nic_conf0&0xF0)>>4`,
`nic_conf0&0xF`) into `init_bbp`, so DAC1 stays up without forcing a
phantom second chain.

### RFCSR12/13.TX_POWER is a backoff code

Higher = more attenuation = **weaker** output. [WIRE] `aireplay.pcap`
writes `RFCSR12 = 0x6b` (chain-0 TX_POWER = 11) and `RFCSR13 = 0x60`
(chain-1 = 0) for 2.4 GHz, so the unburned fallback is the LOW value the
kernel uses (`default_power1=11`, `default_power2=0`). An earlier guess
of 24 ("near the 5-bit max for RSSI") was backwards and made us quieter.
[SRC] rt2800lib.c:2660-2683.

### The rx-filter calibration is degenerate (hardware ceiling)

`rt2800_init_rx_filter` ([SRC] rt2800lib.c:7320-7383) tunes RFCSR24 with
a loopback sweep — fire a passband then stopband tone, read BBP55, walk
RFCSR24 while `(passband − stopband) ≤ filter_target`. It runs on
**every** RT3572: there is no EFUSE-provides-the-value path (only
RT5390/5392-class chips hardcode `calibration_bw20 = 0x1f`, [SRC]
rt2800lib.c:8073). On a **burned** card the front-end is calibrated, the
loopback has a real response, and the loop converges mid-range. On this
**erased** card there's no real response, so it **rails**:

- Kernel rails **high**: [WIRE] `aireplay.pcap` marches RFCSR24 one step
  per iteration `0x07 → 0x6b` to the 100-step cap, never breaks, and
  ships `calibration_bw20 = 0x6b` (config_channel writes both RFCSR24
  and RFCSR31 from it).
- Our driver rails **low**: the loop breaks on step 0 → `0x07`.

Both rails are non-physical; neither is a calibration. The ~7-count
reading offset between the drivers is noise-level on a degenerate filter
and is **not** a portable bug — the RFCSR + BBP init tables, init order,
chain/DAC state, and the cal code were all verified byte-for-byte
faithful. We run the loop unmodified and accept its result. **The TX
ceiling on this unit (~20/40 deauths on-air, high run-to-run variance)
is the missing factory calibration, not the driver**; a properly-burned
RT3572 converges to a real value and sees none of this. (We briefly
tested forcing the kernel's `0x6b` and sweeping mid-range values; the
on-air metric is dominated by RF environment, so nothing beat the
faithful loop reliably — reverted.)

### Settle timing is a non-issue on userland USB

Kernel `msleep(1)` delays (the LDO_CFG0 dance in `init_rfcsr_3572`, the
per-tone settle in `init_rx_filter`) port to `time.sleep(0.001)`, a
Windows no-op (~15.6 ms scheduler tick). It doesn't matter: every
register access is a USB control transfer (~1 ms+ round-trip), so
inter-op latency already covers the kernel's millisecond settles.
Confirmed empirically — real busy-wait settles changed the cal readings
by exactly zero. Don't chase settle timing as a cause of RF misbehaviour
here.

### Attack-stack hardware results (2026-05-31) — match the EFUSE prediction

A full attack pass produced exactly the weak-TX/RX signature the erased EFUSE
predicts (matrix cells in `VERIFICATION.md`):

- **Scan** ✅ but weak — ~8 beacons/s from an AP a few feet away (~10/s healthy).
- **Deauth** — knocked *something* off, too weak to deauth a phone beside the radio.
- **Handshake** — partial (M1+M4) capture, weak.
- **PMKID** — passive capture works; the active "PMKID" button does not (can't
  elicit M1 — weak TX).
- **WEP** — ARP replay ✅; ChopChop ✗ (stalled at 22/32 bytes). FakeAuth bounced
  Associated↔Idle with errors.
- **WPS** — PBC timed out; PIN got 2 NACKs + 1 no-response (talking, unreliable).

All consistent with the missing factory RF cal — not new bugs. The
railing-low-vs-high rx-filter cal is already investigated and is not the lever
(above). These attacks can't be cleanly verified on this unit; that needs a
properly-burned RT3572.

**One observation NOT explained by the EFUSE — the Focus-entry tune bug:**
entering Focus on a CH1 AP once showed 0 beacons; exiting to Scanner and
re-entering Focus on the same AP then showed ~8 beacons/s. The channel set on
Focus entry didn't take the first time (a re-tune fixed it) — independent of RF
calibration. **Confirmed 2026-05-31 on the MT7610U (a healthy card, different
family)** with the identical symptom, so it's a real bug in the **shared
Focus→set_channel path**, not RT3572-specific. Tracked in
`planning/FEATURES.md` § Bugs/QoL.

---

## Deferred (post-feature-complete polish)

1. **Real warm reattach** — skip FW + init when the chip's already
   post-init, just resume bulk-IN polling.
2. **EEPROM-aware RSSI** — `base_val - eeprom_offset - lna_gain - raw`.
   We now have `lna_gain` and `rssi_bg_offset0/1` but rx.py still
   uses simplified `base_val - max(raw)`. Wire EFUSE values in.
3. **TX power per channel** — `RFCSR49/50.TX` writes (only on RT5392).
   Currently skipped (RX works fine without; RT3572/RT5572 already
   write these from `default_power1/2` defaults).
4. **93C66 EEPROM fallback** — older RT2870 dongles don't have
   EFUSE; `efuse_detect` would return False. Would need to port the
   USB_EEPROM_READ one-shot 512-byte streaming path.
5. **AWUS036NH (RT3070)** — fresh chip not yet supported. Captures
   not yet collected; pcap drop-in extension once captures land.
   See `VERIFICATION.md` § Hardware queue.

---

## Hardware test cheat sheet

```
# Each phase strictly supersedes the previous (don't chain — they all
# run cold_bring_up at the start).
uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase open     # M1
uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase fw       # M2a
uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase usbinit  # M2b-1
uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase macinit  # M2b-2
uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase bbpinit  # M2b-3
uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase rfinit   # M2c
uv run python scripts/rt2800usb/test_hw_rt2800usb.py --phase rx       # M3 (+M4 via default ch=1)
```

Replug between runs if the chip ends up in a weird state — Windows
+ Zadig + WinUSB sometimes loses its binding when the kernel rt2870sta
driver tries to grab the device. After replug, verify with:

```
uv run python -c "import libusb_package, usb.core; backend=libusb_package.get_libusb1_backend(); print([(d.idVendor, d.idProduct) for d in usb.core.find(find_all=True, idVendor=0x148f, backend=backend)])"
```

Should print `[(5263, 21362)]` (or 0x148f/0x3572/0x5572 for the other dongles).
