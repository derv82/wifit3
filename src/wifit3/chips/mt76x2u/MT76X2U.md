# MT76x2U / MT7612U

A port of the mt76 kernel driver, generation `mt76x02` (the older sibling of `mt76_connac`;
kernel module `mt76x2u`, vs `mt7921u` for the WiFi-6 sibling). MT7612U silicon: 2T2R, 2.4 + 5 GHz
802.11ac, two-stage firmware (ROM patch + main FW). The dev-machine card is `0e8d:7612` (Alfa
AWUS036ACM), a USB 3.0 device; 15 VID:PIDs are claimed (`constants.py::USB_IDS_MT76X2U`).

## Status

Cold init, two-stage firmware boot, and dual-band monitor RX all work on hardware. A 30-min
dual-band soak (22 channels, 0.25 s hops) ran with no frame-rate sag — active BSSIDs 147→155, 2.4
GHz ~100+ and 5 GHz ~52 steady. ARP replay works first try and handshakes auto-save (after the
L2PAD fix, below). 5 GHz frame injection is HW-confirmed (2026-07-08), TSSI active (see Gotchas).

## EEPROM / strap variants (cross-card generalization)

The driver is written to run on any card in `USB_IDS_MT76X2U`, not just the Alfa `0e8d:7612`.
MediaTek stores per-chip calibration + config in EEPROM/eFuse; nearly all of it is **values**
consumed by computation (already card-agnostic): TX-power tables (`eeprom.read_rate_power` /
`read_power_info`), RX LNA/high-gain offsets (`read_rx_high_gain_2g/5g`), XTAL trim
(`mac._compute_xtal_trim` + `NIC_CONF_2.XTAL_OPTION` switch), and the RSSI/temp fields. The
EEPROM-derived **branches** are all already runtime-gated with every case ported:

- **chainmask** (`NIC_CONF_0` rx_path/tx_path) → `phy_set_rxpath` / `phy_set_txdac` / `mcu_set_channel`.
- **ext-PA** per band (`mt76x02_ext_pa_enabled` = `!NIC_CONF_0.pa_int`) → `driver._ext_pa_for`, both
  branches in `phy_set_txpower_regs` / `phy_configure_tx_delay` / `tssi_compensate` / TSSI-init flag.
- **ext-LNA** per band (`mt76x2_has_ext_lna` = `NIC_CONF_1.lna_ext`) → `phy_set_gain_val` /
  `update_channel_gain` (both `has_ext_lna` branches).
- **TSSI** (`NIC_CONF_1.tx_alc_en` gated by `temp_tx_alc`) → `driver._tssi_enabled`.
- **bt_rcal_valid** (`EE_BT_RCAL_RESULT` != 0xff) → gates `MCU_CAL_R`.

The one genuine reference-hardcoded discriminator was the **chip strap**
`is_mt7612 = (ASIC_VERSION >> 16) == 0x7612` — a WiFi-only die vs a WiFi+BT combo (0x7662/0x7632,
e.g. the claimed MT7662U `0e8d:7632`). Two USB-path branches key on it and are now gated on the
runtime ASIC read (`driver.is_mt7612`), defaulting to the reference so its recorded path is
byte-identical:

- `firmware.load_rom_patch` — `rom_protect = !is_mt7612`: a combo die shares `MT_MCU_SEMAPHORE_03`
  with its on-chip BT core, so the ROM-patch load must acquire it (poll, 600 ms) and release it
  (write 1). 0x7612 skips the dance. [SRC] `mt76x2/usb_mcu.c:59,65-70,138-139`
- `mac.mac_reset` — the `COEXCFG0` BT-coex clear runs only for 0x7612. [SRC] `mt76x2/usb_mac.c:84`

Connect logs the detected strap (`chip=0x7612, mt7612 (WiFi-only)` vs `combo (WiFi+BT, rom_protect)`).

**MediaTek EEPROM discriminator model (for sibling mt76x0u / mt7921au ports):** on mt76x2 the
per-card variance is overwhelmingly *values*; the only true init-gating branches are `NIC_CONF_0`
(rx/tx path, pa_int → ext-PA, board_type) + `NIC_CONF_1` (lna_ext, tx_alc_en, temp_tx_alc) + the
`is_mt7612` strap, all read live from EEPROM / the ASIC-version register. `mt76x02_eeprom_get`
grep-points are the map. Temp-compensation (`mt76x2_get_temp_comp`) is **PCI-only** — never called
on the USB `usb_phy.c` path, so it is not a gap for the USB drivers.

### Residual gaps (documented, not board-discriminators)

- **`mcu_init_gain` gain hardcoded to 0** (`chan.set_channel_20mhz` → `phy.mcu_init_gain`). The
  kernel passes `dev->cal.rx.mcu_gain` from `mt76x2_read_rx_gain` (`mt76x02_get_rx_gain`:
  LNA_GAIN + RSSI_OFFSET fields). wifit3 ports only the `high_gain` half of `read_rx_gain`; the
  `mcu_gain` / `rssi_offset` / `lna_gain` values are unported and default to 0. This is a value
  simplification affecting **all** cards identically (incl. the reference), not a hardcoded-to-
  reference branch, so it is out of scope for cross-card generalization — but re-porting it would
  improve RX sensitivity on every card.
- **`board_type` / band capability not enforced** (`mt76x02_eeprom_parse_hw_cap`). `SUPPORTED_CHANNELS`
  statically advertises both bands. Every claimed ID is a dual-band 2T2R MT7612U/MT7662U, so no
  single-band strap exists to gate against; a hypothetical 2GHz-only board would simply get empty RX
  on 5 GHz rather than a clean skip.

## Gotchas

**connect() always cold-boots; it never reuses running firmware.** `MT_MCU_COM_REG0` only reports
that *some* firmware is up, not *whose*: a prior wifit3 session, a stale MCU, or (on Linux) the kernel
`mt76x2u` driver's own firmware, which our command set can't drive. So if anything is running,
connect() `force_power_cycle`s the WLAN block (clears the ROM-patch-applied bit + FCE state) and always
re-uploads our firmware. There is no warm-skip path and no warm fallback. Reusing a warm/foreign MCU
aggravated the intermittent `mcu_load_cr` seq-mismatch and the ~4 s of `MCU_CAL_*` response timeouts;
always cold-booting makes both rarer (most cold boots are clean) but does not eliminate them. They still
surface intermittently on cold boots: the `MCU_CAL_*` (cmd=31) timeouts are non-fatal (the tune
continues), and a rare `mcu_load_cr` (cmd=2) timeout that `force_power_cycle` cannot clear needs a
physical replug (Win+WinUSB cannot fully cold-reset a wedged MCU). Costs ~1 s of FW upload per open.

**Remove the L2 alignment pad BEFORE trimming to MPDU_LEN.** mt76x02 sets `MT_RXINFO_L2PAD` and
inserts 2 bytes between the 802.11 header and the body whenever the header isn't 4-byte aligned —
i.e. every QoS-Data frame (26-byte header), which is what EAPOL and WEP-ARP ride on. The kernel
de-pads *then* trims, and MPDU_LEN counts the de-padded MPDU, so `rx.py::decode_urb` must match that
order. Windowing to MPDU_LEN first drops the last 2 body bytes — it clipped EAPOL M2 `key_data`
(uncrackable handshake) and shrank WEP ARP from 70→68 B (flaky replay). Beacons/mgmt are unaffected
(24-byte header, no L2PAD), which is why scanning always looked healthy.

**TSSI is ON by default** (`driver.py::_tssi_enabled` trusts the EEPROM flag, matching the kernel);
`WIFIT3_MT76X2U_TSSI=0` is a kill switch. It was gated OFF for a while on a suspicion that the periodic
`tssi_compensate` path zeroed TX power (the flagged `tssi_slope=127` read "near max"). That was a false
alarm: the EEPROM feeds sane, kernel-faithful slopes/offsets — the 2.4 GHz slope 127 sits right in the
5 GHz 122–132 cluster, offsets are in range, `target_power` is 34/29 (non-zero) — and the `phy.py` port
of `mt76x2_phy_tssi_compensate` matches the kernel line-for-line. 5 GHz TX with TSSI active is
HW-confirmed (2026-07-08: deauth on CH149 landed, compensate loop running `tssi=True`, no errors).

**No patch-semaphore wall.** `rom_protect = !is_mt7612(dev)` is false for this silicon, so the
`MT_MCU_SEMAPHORE_03` acquisition is skipped — this is the structural reason MT7612U doesn't hit the
wall that paused MT7921AU.

**The Alfa dev card enumerates directly as the wireless device** — `0e8d:7612`, a single
vendor-specific interface (255/255/255) with the 8-EP wireless set (`0x84`/`0x85` IN, `0x04`–`0x09`
OUT); no mass-storage stage. Verified on Linux 2026-07-08 (descriptor + dmesg: it comes up as 7612
with `mt76x2u` binding immediately on every replug). The USB-mass-storage ("DISK") enumeration is a
*different*, ZeroCD-shimmed knockoff variant that first appears as Realtek `0bda:1a2b` then mode-switches
to `0e8d:7612` — on Linux it auto-detaches to the MediaTek PID immediately; on Windows the WinUSB/Zadig
bind does the switch. `transport.assert_expected_endpoints()` is the guard for *that* variant: it fails
fast if the wireless EPs are missing (device caught mid-switch). On the Alfa the EPs are always present,
so the mode-switch-across-power-cycles question is a non-issue for it.

**Channel switches need no settle delay.** An earlier note suspected the vendor stack's ~2 s breathing
room might be a real firmware constraint; a 30-min soak hopping all 22 channels at 0.5 s dwell
(2026-07-08) held frame rate steady with no degradation, so the driver re-tunes with no extra wait.

**20 MHz primary only, by design.** `set_channel_20mhz` hardcodes `bw=0` / `ch_group_index=0`; we
deliberately skip the kernel's 40/80 MHz path. This is the project-wide posture, not a capture gap —
everything wifit3 acts on (beacons, auth/assoc, EAPOL, WEP IVs, all legacy-rate attacks) rides the
20 MHz primary; 40/80 MHz only carries HT data payloads wifit3 never needs.

## Orientation

Two-stage firmware lives in `firmware.py`: a ROM patch (`mt7662_rom_patch.bin` → `0x00090000`) then
main FW split into ILM (`0x00080000`) and DLM (`0x00110000`, or `0x00110800` on rev ≥ E3 — this card
is E4). Chunks upload over the bulk-OUT MCU path on EP 0x08, with the dst address split across two
no-payload control transfers per `mt76u_single_wr`. We ship header-stripped bodies (the
linux-firmware headers never appear on the wire) and skip the header-read step.

Register access is one vendor control transfer each, with two virtual-bus marker bits at the top of
the address selecting bRequest: none → MULTI_READ/WRITE (MAC/BB/RF), `BIT(30)` → CFG bus, `BIT(31)`
→ EEPROM read. Encoding: `wValue = addr >> 16`, `wIndex = addr & 0xFFFF`, 4-byte LE payload.

RX decode + monitor filtering: `mac.py::mac_start(monitor=True)` clears the `MT_RX_FILTR_CFG`
unicast/BSSID drop bits so ToDS capture works. Endpoints are assigned positionally in descriptor
order (`mt76u_set_endpoints`): in_ep `0x84`/`0x85`, out_ep `0x04`–`0x09`, with `0x08` the inband-cmd
EP used for FW upload + MCU. Names match the kernel; grep `data_dumps/mt76-source-v6.18/` to
cross-reference.

## Scripts

- `extract_mt7662_fw.py` — splits the bulk-OUT FW chunks out of the cold-boot pcap into `assets/`.
- `scripts/diag/sweep.py` — multi-channel RX soak / longrun stress (used for the 30-min dual-band run).
- `verify_pcap.py` — offline cold-boot byte gate against `captures_mt76x2u/capture-1.pcap`.

## Debug log

### 2026-07-20: cold boots are mostly-clean, not always-clean (correction)

Follow-up HW on the mt7612u refines the entry below. "A clean cold boot has zero cal/mcu timeouts" was
too strong: it held for the 40 logged coldboot cycles but is not universal. A channel-hop run showed 5
connect-time `MCU_CAL_*` (cmd=31) timeouts on a cold boot (non-fatal, the tune continues); its 22 runtime
re-tunes were clean (100-640 ms, zero timeouts, 137 BSSIDs heard), which refuted and removed the
"runtime re-tune cmd=31" BUGS item. Two later cold boots hit the `mcu_load_cr` (cmd=2) timeout and wedged
the MCU; `force_power_cycle` + re-upload could not clear it, so it needed a physical replug. Net: cold
booting makes the flake rarer, not gone; the cal timeouts are non-fatal, a wedged MCU is replug-only.

### 2026-07-20: connect() always cold-boots (warm-skip removed)

Dropped the warm-reattach path. connect() no longer trusts a running `MT_MCU_COM_REG0` latch: the latch
can't tell our firmware from a kernel-warmed card's, and reusing a warm/foreign MCU produced the
intermittent `mcu_load_cr` seq-mismatch (~1/10) plus ~4 s of `MCU_CAL_*` response timeouts on warm
attach. New policy: if any FW is running, `force_power_cycle` then always re-upload our firmware for a
deterministic cold state. Removed the warm-skip branch, `McuChannel.drain_response_queue`, and the warm
`mcu_load_cr`-timeout fallback ladder. HW (single owner, wifit3 closed): 50 rapid cold-open cycles,
48/50 booted with beacons every time (11-28 APs/cycle); the two misses were a USB transient in the first
run right after a physical replug and never recurred over the next 40 cycles. Two logged runs (40
cycles) had zero cal/mcu timeouts, confirming a clean cold boot calibrates fast (the timeouts were a
symptom of dirty warm/contended state, not of cold boot). `dev.reset()` was ruled out as a software
cold on Windows+WinUSB (no-op, no re-enumeration; latch unchanged). verify_pcap stays green (the cold
wire is unchanged: `force_power_cycle` only fires when warm, and a genuinely cold first plug skips it).
`LINUX_REPLUG_AFTER_MODPROBE` stays `False` but is setup-flow only (no runtime effect); the old
"self-cold, no replug" note is now literally what connect() does on every open.

### 2026-07-08 — 30-min hop soak clean; enumeration story corrected

Longrun soak (all 22 channels, 0.5 s hop, 30 min): active-BSSID trend 126→131 (flat/up, ratio 1.04),
frames steady-to-rising (~2.3k→~3.3k per 60 s bucket), 5 GHz active held ~44 throughout (no synth
loss), no death event. Kills the suspected ~2 s channel-switch settle — dropped from Gotchas. Two soak
WARNs are benign, not driver defects: 3.9% "garbage OUI" is broadcast `ff:ff:ff:ff:ff:ff` flagged by
the OUI-sanity heuristic, and 20.8% beacon-channel mismatch is adjacent-channel bleed at 0.5 s dwell.
Report: `scripts/diag/reports/mt76x2u_20260708-053217.md`.

Also corrected the enumeration Gotcha: the Alfa presents the wireless interface directly (`0e8d:7612`,
vendor-specific, 8 EPs, no MSD) — the mass-storage "DISK" front-end (Realtek `0bda:1a2b` → mode-switch)
is a separate ZeroCD knockoff, not the dev card. This retires the last mt76x2u line in `BUGS.md`; the
card is now in the clean list.

### 2026-07-08 — RX-poll (RxDrainer) re-verified dual-band

Confirmed the production `RxDrainer` bulk-IN path on hardware. `test_hw --phase rx` (ch6, 12 s): 835
URBs, 0 dropped, 559 beacons, 25 unique BSSIDs. `--phase hop` (all SUPPORTED_CHANNELS): 110 unique
BSSIDs (66× 2.4 GHz, 44× 5 GHz), frames on every populated channel, 5 GHz switches settling <300 ms.
Clears the "RX-poll unverified" line in `BUGS.md`.

### 2026-07-08 — TSSI enabled by default; the "zeroes TX power" suspicion was false

TSSI had been gated behind `WIFIT3_MT76X2U_TSSI=1` on a suspicion the periodic `tssi_compensate` loop
zeroed TX power (the read `tssi_slope=127` looked "near max"). Diagnosed on `0e8d:7612` by cold-booting
and dumping the EEPROM fields the loop feeds the MCU: they're sane and kernel-faithful — 2.4 GHz slope
127 sits inside the 5 GHz 122–132 cluster, offsets 17–30, `target_power` 34 (2G)/29 (5G). Both
target-power reads (2G `0xF6>>8`, 5G `0xF8 & 0xFF` = `MT_EE_RF_2G_RX_HIGH_GAIN`) and the whole
`mt76x2_phy_tssi_compensate` port match kernel v6.18. Flipped the default to trust the EEPROM (kernel
behavior) with `=0` as a kill switch. HW-confirmed: deauth burst on CH149 landed with the compensate
loop running `tssi=True`, no errors — TX not zeroed. Clears the last mt76x2u TSSI item in `BUGS.md`.

### 2026-07-08 — 5 GHz TX confirmed on HW

Live 5 GHz frame injection confirmed working on hardware (AWUS036ACM, `0e8d:7612`). Clears the last
"5 GHz inject unverified" item in `BUGS.md`. TX power is plainly not zeroed in the default TSSI-off
config; whether *re-enabling* TSSI zeroes it stays the open question (see Gotchas / the TSSI line).

### 2026-07-08 — replug default flipped; opt-out now explicit

`LINUX_REPLUG_AFTER_MODPROBE` now defaults **True** (replug-required is the safe default), so this
chip sets `= False` **explicitly** — the 06-27 note's "does not set it" is superseded; the self-cold
reasoning (`force_power_cycle`) stands. In this family only mt7921au is replug-required (`True`);
mt76x0u and mt76x2u self-cold and opt out with `False`.

### 2026-06-27 — Linux bring-up validated; no replug gate needed

First Linux (Kali VM) validation. Unlike its connac siblings (mt76x0u, mt7921au), mt76x2u does
**not** set `LINUX_REPLUG_AFTER_MODPROBE` and should not: those two can't cold-reset in userland
(replug-only), but `power.force_power_cycle` clears this chip's WLAN block to a cold-equivalent
state without a physical replug, so the device-setup no-replug path self-recovers a kernel-warmed
chip. Confirmed end-to-end: install-rules→boot, warm reboot, and unplug/replug all succeed; passive
`test_hw_mt76x2u --phase rx` (27 BSSIDs ch6) and `--phase hop` (107 BSSIDs, 65×2.4 GHz + 42×5 GHz)
both PASS. Note: `test_hw_mt76x2u`'s default/`all` phase fires live deauth — use `--phase rx|hop`
for passive validation.

### 2026-05-29 — L2PAD clipped EAPOL/WEP

RX was healthy for scanning but handshakes were uncrackable and WEP ARP replay flaky. Root cause was
the L2PAD ordering (now in Gotchas): windowing to MPDU_LEN before de-padding dropped the trailing 2
body bytes, but only on QoS-Data frames — beacons/mgmt have a 4-byte-aligned header and never set
L2PAD, so scanning masked it. Fixing the de-pad-then-trim order made ARP replay work first try and
handshakes auto-save.

### 2026-06-20 — firmware provenance

The pcap-extracted bodies are byte-identical to linux-firmware `mediatek/mt7662.bin` +
`mediatek/mt7662_rom_patch.bin` (not the `mt7662u*` variants): the ILM slice, trailing DLM, and
rom-patch body all match, and mainline `mt76x2u` requests exactly these. WHENCE files them under
driver `mt76x2e` → governed by `LICENCE.ralink_a_mediatek_company_firmware`, which ships alongside
the blobs in `assets/`. The DLM landing at `0x110800` plus an ASIC version of `0x76120044` pins the
silicon at rev E4.

### 2026-07-20 — verify_pcap brought to parity with v6.18 (CHECK A-C frontier)

Ran the strict single-cursor `verify_pcap` past the old op#1 wall and reconciled the cold-boot
path with the vendored `data_dumps/mt76-source-v6.18` source. CHECK D green; CHECK A-C reproduces
1091 ops byte-for-byte (reset_wlan → mac_start). Real port gaps fixed (see `planning/BUGS.md`):
ROM-patch read now gated behind `rom_protect`, WPDMA/mac wait order, US_CYC/TXOP moved to after
beacon config, cached RX_FILTR read, WCID/skey `write_copy`, full `mac_stop` port,
`mac_reset_counters`, and `mac_start` reordered ahead of the channel tune. All HW re-validated on
the real card: `test_hw_mt76x2u --phase rx` heard 16 BSSIDs; `ack_lab/rx_autoack --test-card 7612
--prober-card 8812` gave 96/100 spoofed (active monitor) + 100/100 silicon + 0 bogus.

The remaining CHECK A-C FRONTIER is **not a port bug**: `set_channel_20mhz` matches the v6.18
source order, but the pcap runs the *wrapped* `mt76x2u_set_channel` (initial `phy_set_txpower` +
config-time `mt76x2_mac_stop` before `phy_set_channel`) where wifit3 tunes bare. Follow-up: add
that wrapper for full parity. A few `MCU cmd=31 (SWITCH_CHANNEL) response timeout`s appear on
runtime re-tunes in ack_lab (not on the connect-time tune); the missing mac_stop-before-retune is
a candidate cause worth checking.
