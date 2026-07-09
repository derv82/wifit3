# rt2800usb family

Ralink rt2800usb-family port from the kernel `rt2x00` source (`data_dumps/rt2x00-source-v6.18/`),
verified on hardware. One driver claims three VID:PIDs (`148f:5372/3572/5572`) and dispatches per
the `silicon_id` read from MAC_CSR0 at connect. The USB PID is named after the marketing SKU, but
the on-chip MAC_CSR0 reports the actual silicon ID — and the two diverge on all three rebrands:
RT5372→0x5392, RT3572→0x3572, RT5572→0x5592.

## Status

Cold bring-up, RX, and TX inject are working on hardware across all three silicons (M1-M5).
The RT5392 (PAU05) is the reference unit — its full attack chain works end-to-end (deauth → EAPOL
re-capture, WEP ARP replay + chop/frag). Warm reattach is detected but not yet taken — the driver
always cold-restarts. EEPROM-aware RSSI, per-channel TX power (RT5392), and a 93C66 EEPROM fallback
for pre-EFUSE RT2870 dongles are deferred polish.

The AWUS051NH v2 (RT3572) test unit has an **erased EFUSE** and can't be cleanly verified — see
Gotchas. A properly-burned RT3572 is needed to confirm that family's attack stack.

## Gotchas

**The vendor-request signature lies.** Kernel `rt2x00usb_vendor_request` is declared
`(u16 offset, u16 value)` but the wire goes `(value=wValue, offset=wIndex)`. Any other ordering
returns `0x00020208` (a stale word) for every address. Register access uses `bRequest` 6/7
(multi) or 2/3 (single), `wValue=0`, `wIndex=addr`.

**Multi-byte transfers must chunk to 64 bytes** (kernel `CSR_CACHE_SIZE`). A single 4-KB FW upload
silently fails or pipe-stalls; chunking to 64×64 (address advancing 64 bytes per chunk) fixes it.

**EFUSE `freq_offset` is the actual RX gate.** Without it the chip tunes a fraction of a MHz
off-channel, the BBP never locks onto preambles, and bulk-IN goes silent with every diagnostic
register reading correctly — this cost multiple hours. `EFUSE_CTRL.ADDRESS_IN` is a **u16-word**
index (`byte_offset // 2`), easy to get wrong as a byte address. The real EFUSE MAC is also needed
in MAC_ADDR_DW0/DW1 for the RX matching engine (a fake MAC + `UNICAST_TO_ME_MASK=0xFF` was an
earlier workaround).

**RX L2 padding must be removed before trimming to MPDU length.** The hw inserts 2 bytes between
header and payload when the header isn't 4-aligned (every QoS-Data frame, i.e. the EAPOL carrier;
beacons aren't padded). Trimming to `MPDU_TOTAL_BYTE_COUNT` first clips the last 2 body bytes — an
EAPOL M2's key_data tail — surfacing as "EAPOL clipped" + an uncrackable handshake.

**The TX crypto engine must be disarmed or WEP injects die silently.** `reg_init` clears
SHARED_KEY_MODE / WCID / WCID_ATTR / IVEIV so the zeroed TXWI W2/W3 (IV/EIV) are safe. If any
cipher table is left set, the engine encrypts a Protected inject and overwrites the frame's real IV
with zeros — the AP's ICV check drops every replay while TX_STA_FIFO still flags TX_SUCCESS.

**TX inject needs three things the RX path doesn't.** A mandatory 4-byte USB *end* pad after the
alignment pad (kernel `roundup(len,4)+4`; without it the chip never ACKs the bulk-OUT); `QSEL=2`
(EDCA), not MGMT, for every frame including management; and bulk-OUT EP `0x02` (AC_BE), not `0x06`
— the MGMT endpoint is chip→host TX status, not host→chip submission.

**RT3572 with an erased EFUSE behaves non-obviously, and it's hardware, not the driver.** Identity
is programmed but the RF/cal region reads 0xFF and `NIC_CONF0=0x0000`. The kernel runs the unit as
**1T1R** (RFCSR1=0xf1, chains 1+2 powered down), and the rx-filter calibration loop has no real
loopback response so it **rails** (kernel high to 0x6b, our driver low to 0x07) — both rails are
non-physical, neither is a calibration, and the ~7-count offset is noise on a degenerate filter,
not a portable bug. The weak TX (~20-40 deauths on-air, high variance) is the missing factory cal.
RFCSR12/13.TX_POWER is a **backoff code** (higher = more attenuation = weaker); the unburned
fallback is the low value (power1=11, power2=0), not a near-max guess. An unburned EFUSE on a retail
card is itself suspect (QC miss or counterfeit).

**Settle timing is a non-issue on userland USB.** Kernel `msleep(1)` delays port to a Windows
no-op (~15.6 ms tick), but every register access is already a ~1 ms+ USB round-trip, so inter-op
latency covers the kernel's millisecond settles. Real busy-waits changed cal readings by exactly
zero — don't chase settle timing as a cause of RF misbehaviour.

**RT5592 auto-manages some RFCSRs** — readback after init doesn't always match what we wrote; this
is expected (see `feedback_rt5592_chip_auto_managed_rfcsr`). It also picks one of two channel
tables (xtal20/xtal40) at runtime from `MAC_DEBUG_INDEX.XTAL` because the PCB can ship either
crystal, and runs a per-tune IQ calibration (BBP158/159 indirect pairs) on every channel set.

**Focus-entry channel tune sometimes doesn't take the first time** (0 beacons; re-entering Focus
fixes it). Reproduced on the MT7610U too, so it's a bug in the **shared Focus→set_channel path**,
not RT3572-specific. Tracked in `planning/FEATURES.md` § Bugs/QoL.

## Orientation

RT5392 is 1T1R 2.4 GHz; RT3572/RT5592 are 2.4 + 5 GHz, 2T2R-capable. Single 4096-byte FW blob
(`assets/rt5572.bin`) shared across the family — trailing 2 bytes are CRC-CCITT (LSB-first,
reversed poly 0x8408), matching the Linux `crc_ccitt` lib (not the MSB-first XModem variant).

Start at `driver.connect` for the cold flow: chip-id → FW upload → EFUSE read → MAC/BBP/RFCSR init
→ enable_radio → channel tune → RX loop. EFUSE is `eeprom.py`; per-silicon channel synth is
`chan.set_channel` (RF53xx/RF3052 use a 3-field synth, RF5592 a 5-field one); RX/TX descriptor
decode/build is `rx.parse_rx_urb` / `tx.py`. Names match the kernel C — grep the source bundle to
cross-reference.

The RxReaderThread + ToDS-promiscuous-filter (`RX_FILTER_CFG=0x11`, DROP_NOT_TO_ME clear) and the
ported RX-AGC link tuner (`link_tuner.py` + `driver._link_tuner_loop`) are monitor-mode deviations
from the kernel STA path — captured below.

## Scripts

- `test_hw_rt2800usb.py --phase {open,fw,usbinit,macinit,bbpinit,rfinit,rx}` — staged HW bring-up.
- `rt2800_ctrl_diff.py` — extracts the kernel control-transfer sequence from a pcap; isolated the missing EFUSE walk.
- `verify_pcap.py` — single-cursor whole-capture byte gate. Drives the port's real helpers
  in kernel wire order over one cursor; fail-closed, 0 waivers. Covers the full cold bring-up
  (1781 ops) + 128/128 RF55xx channel-tune blocks on the RT5572 capture; frontier = the
  operational phase (airmon + hops), still being converged.

## Debug log

### EFUSE: the RX blocker

M3 (RX) delivered no bulk-IN URBs until EFUSE bring-up landed — clean bring-up, every diagnostic
register correct, bulk-IN silent. `rt2800_ctrl_diff.py` over the rt5372 capture showed the first
~250 vendor requests are all EFUSE reads (32 iterations hitting `EFUSE_CTRL=0x0580`); wifit3 was
doing zero. The gate was `freq_offset` (crystal oscillator offset) — without it the BBP never locks
a preamble. `ADDRESS_IN` being a u16-word index (`byte_offset // 2`) was the subtle fix; a later
audit confirmed it byte-for-byte vs the kernel walk and showed no RX regression on real cards.

### TX inject: three follow-on bugs

Once RX worked, `iface.deauth` hit errno 10060 (timeout) on bulk-OUT. Reading
`rt2800usb_get_tx_data_len` + `rt2800usb_write_tx_desc` turned up the mandatory +4 USB end pad,
`QSEL=2` (not MGMT), and bulk-OUT EP 0x02 (not 0x06). With all three, deauth against a real AP
deauths a phone → reconnect → captures the new 4-way handshake.

### WEP IV zeroing

WEP ARP replay was dead on the RT5572 — a dual-NIC sniff showed on-air IV=00:00:00 and zero AP
rebroadcasts — until the SHARED_KEY_MODE clear in `reg_init` landed. The left-armed crypto engine
was overwriting the real IV with the zeroed TXWI W2/W3. With it, ~4000 AP rebroadcasts per 30 s and
chop/frag work.

### RX-AGC link tuner (weak/unstable RX)

On PAU05 the beacon rate wandered (1-3/s → 7-8/s → 4-5/s) with periodic ~zero gaps every ~10-15 s,
and a strong *near* AP came in worse than a weak *far* one — the signature of front-end overload,
not a detune. BBP66 (RX VGC/AGC gain) was seeded once per channel tune and never adapted, so we sat
permanently at the most-sensitive seed. Ported the kernel's ~1 Hz link tuner (`link_tuner.py`),
which *raises* VGC when averaged RSSI is strong. Monitor-mode deviation: kernel disables the tuner
for a pure-monitor interface and feeds it from associated-BSS beacons only; we keep the algorithm
verbatim but source RSSI from every good frame, and it can only de-sensitise on strong signals so
weak-signal sensitivity is never reduced. Resets on every channel change. HW (PAU09/RT5572,
2026-07-06): strong-AP beacon rate is stable at kernel parity (9.1 vs 9.6/s) — the tuner does its
stability job — but 2.4 breadth trails kernel (87 vs 111 APs); suspected the RSSI over-read
(EFUSE-aware RSSI, below) drives the de-sensitisation.

### RT3572 unburned-EFUSE attack pass (2026-05-31)

A full attack pass on the erased-EFUSE AWUS051NH v2 produced exactly the weak-TX/RX signature the
missing factory cal predicts: scan weak (~8 beacons/s from a few feet, ~10/s healthy), deauth too
weak to knock a phone beside the radio, partial handshake (M1+M4), passive PMKID only, WEP ARP
replay works but ChopChop stalls, WPS unreliable. All consistent with the missing RF cal — not new
bugs. We briefly tried forcing the kernel's railed-high 0x6b and sweeping mid-range cal values; the
on-air metric is dominated by RF environment so nothing beat the kernel's loop reliably (reverted).
These need a properly-burned RT3572 to verify.

The `rt2800_disable_unused_dac_adc` gate reads the **raw** NIC_CONF0 TXPATH/RXPATH fields, not a
validated chain count — on the erased EFUSE both read 0 so the kernel powers down neither DAC1 nor
ADC1; `driver.py` passes the raw fields through so DAC1 stays up without forcing a phantom chain.
An earlier "force 2T2R" override was a workaround for our own DAC-gate bug and diverged from the
wire — removed.

### EFUSE-aware RSSI (deferred)

RSSI is `base_val(-12) - eeprom_offset - lna_gain - raw_byte`, max across paths. We currently use
`eeprom_offset = lna_gain = 0`; `lna_gain` and `rssi_bg_offset0/1` are now read from EFUSE but rx.py
still uses the simplified form. Wire the EFUSE values in when EEPROM-aware RSSI lands (shares work
with the per-channel TX power tables).

HW-confirmed (2026-07-06, PAU09 vs kernel): the simplified form over-reads RSSI by **+8.1 dB (2.4)**
/ **+10.7 dB (5 GHz)** — band-split, as the missing per-band `lna_gain`/`rssi_bg_offset` predicts.
Not just cosmetic: the over-read feeds the RX-AGC link tuner (above), which de-sensitises on strong
averaged RSSI, so marginal 2.4 APs drop (breadth 87 vs kernel 111). Wiring the EFUSE values into
`rx.py` is the lead for closing that breadth gap.

### Byte-for-byte convergence to the kernel (2026-07-08)

Rebuilt `verify_pcap` from anchored blocks into a single-cursor whole-capture walk (RT5572/RF5592,
`usb_dumps_new2/captures_rt2800usb_rt5572`) and converged the port to emit the kernel's exact bytes.
Cold bring-up now walks **1781 ops byte-for-byte** + **128/128 channel-tune blocks** (80.3% of the
capture, 0 waivers). Operational phase (airmon monitor-enable + `iw`/airodump hops) still pending.
RX + PA hardware-verified after each behavioural change (PAU09, cold: RFCSR49/50 = 17/15, ~900
beacons/hop). What changed, per commit:

- **`9ed7e1a` TX power + cascade.** `chan.default_power` decodes RFCSR49/50 from EEPROM
  TXPOWER_BG1/BG2 (2.4 GHz) / A1/A2 (5 GHz, per-silicon index) with `txpower_to_dev` clamp; added
  `config_txpower` (TX_PWR_CFG_0..4 from TXPOWER_BYRATE) to the RF55xx tune. Fixes: `freq_cal_mode1_usb`
  RFCSR17-already-set short-circuit; NIC_CONF1 capability masks (BT/ext-LNA-BG/A were bits 13/8/9 →
  14/2/3); `iq_calibrate` 0xFF→0 only on the 2 global comp/imbal bytes (per-band TX0/TX1 raw);
  `AUTOWAKEUP_CFG` `0x1010`→`0x1208`; added `autorun_detect` (EFUSE + FW paths) + `probe_hw_gpio`.
  Unburned EFUSE keeps the wire-derived fallback.
- **`39809cb` verify_pcap walk.** Replaced anchored EFUSE/FW blocks with `verify_cold_walk` (one
  cursor, kernel order) + explicit coverage reporting.
- **`749e913` radio-on.** Added `set_radio_led` (MCU_LED from EEPROM_FREQ LED_MODE) + MCU_WAKEUP;
  MCU_LED / MCU_WAKEUP constants.
- **`169014c` USB DMA.** `USB_DMA_CFG.RX_BULK_AGG_LIMIT` was omitted (`0xc00080`→`0xc02d80`); split
  `usb_enable_radio_dma` out of the monolithic `enable_radio`.
- **`0cacfcd` init_registers.** Nest `usb_init_registers` (drv-hook reset) inside `init_registers`
  (dropped the duplicate `connect()` call); port `config_filter(FIF_ALLMULTI)` (`RX_FILTER_CFG`=0x1bf97);
  RTS threshold 2347→**2353** (Linux `IEEE80211_MAX_RTS_THRESHOLD`); WCID entry as one 8-byte
  multiwrite (was 2× `write32`); port the 8 beacon-slot clears.
- **`ddda352` init_bbp/rfcsr.** Port the BBP138 RX_ADC1/TX_DAC1 RMW in `normal_mode_setup_5xxx`
  (threaded txpath/rxpath through `init_rfcsr`). init_bbp needed no change.
- **`8d9f1dc` enable_radio tail.** Extracted `enable_radio_finish`: MAC_SYS_CTRL / WPDMA_GLO_CFG
  enables are now RMW (were direct writes) + LED_AG/ACT/POLARITY MCU configs (were skipped). The
  monitor `RX_FILTER_CFG`=0x11 stays in `connect()`'s `enable_radio` (kernel applies it in the
  operational phase, not here — to reconcile).
