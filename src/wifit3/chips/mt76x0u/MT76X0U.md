# MT76x0U / MT7610U

A port of the kernel `mt76x0u` driver (mt76 family, `mt76x02` generation — older sibling of
mt76_connac, shares `mt76x02_*.c` helpers with mt76x2u). 1T1R, 2.4 + 5 GHz, firmware-based, in-band
MCU command channel. Dev card is `0e8d:7610` (Sabrent / MediaTek MT7610U).

## Status

- Cold boot through PHY init working on hardware: FW upload, post-FW DMA/MAC reset, MCU channel, EFUSE, MAC/BBP/RF init.
- 2.4 GHz monitor RX: working.
- 5 GHz monitor RX: at kernel parity (per-channel LNA-gain fix; see Gotchas). 2026-06-28 wifit3-vs-`mt76x0u` A/B: 5 GHz breadth 33=33, per-channel ~9.09/s incl. CH157 (~93% ceiling).
- Full attack suite HW-confirmed (2026-06-28): deauth / handshake / PMKID / WPS-PBC (36 EAPOL, auto-ACK) live-fired on 5 GHz; WEP chopchop + ARP-replay ~300 IVs/s on the 2.4 GHz router.
- `verify_pcap`: CHECK A-C + CHECK D green (byte-exact cold boot + TX descriptor, capture-1).
- Not ported: the periodic RSSI-driven AGC tracker (`mt76x0_phy_update_channel_gain` / `calibration_work`) — dynamic gain isn't re-seeded, lower-priority gap. RSSI-display (`rssi_offset[]`) half of `read_rx_gain` also unported (display-only).

## EEPROM / strap variants (cross-card generalization)

The driver runs on any card in `USB_IDS_MT76X0U`, not just the captured `0e8d:7610` (a 0x7650
dual-band die). MediaTek keeps per-chip calibration + config in EFUSE; nearly all of it is
**values** consumed by computation and already card-agnostic: TX-power (`eeprom.get_tx_power_*`,
unported RX-only), RX LNA gain (`eeprom.lna_gain_for_channel`, threaded per-tune), XTAL/`freq_offset`
+ `temp_offset`, and the ext-PA branch (`phy_set_chan_rf_params` reads `nic_conf_0` PA_INT bits live).
The 1T1R chainmask is a whole-family constant (mt76x0 has no 2-stream part), not a reference hardcode.

The real init-gating discriminators the `SUPPORTED_IDS` admit, now runtime-gated (default = the
captured 0x7650 reference, so its recorded path is byte-identical):

- **`is_mt7630`** — the 2.4G-WiFi+BT combo strap, `mt76_chip = ASIC_VERSION >> 16 == 0x7630`
  ([SRC] mt76x0/usb.c:266 + mt76.h:1231), read live in `driver._connect_init_mac`. Gates three
  USB-live branches: `eeprom.decode_chip_cap` masks off `has_5ghz` ([SRC] eeprom.c:62-65);
  `phy._apply_rf_patch_override` writes `RF(5,2)=0x1d` vs `0x0c` ([SRC] phy.c:1143-1146);
  `phy.phy_calibrate` returns before any op ([SRC] phy.c:866-867). The reference reads 0x7650 →
  `is_mt7630=False` → all three take the else/run path.
- **`no_2ghz`** — the TP-Link Archer T1U (`2357:0105`) USB `driver_info=1` quirk ([SRC] usb.c:35,245),
  keyed on the matched USB id (not EFUSE). `eeprom.decode_chip_cap` masks off `has_2ghz`
  ([SRC] eeprom.c:57-60).

`is_mt7610e` (RF(0,3)/RF(0,21)/RF(5,2) arms) is **mmio-only** ([SRC] mt76x0.h:31-36 requires
`mt76_is_mmio`) so it is dead on USB for every strap — `RF(0,3)=0x73` / `RF(0,21)=0x12` stay constant.
Unit tests: `tests/chips/mt76x0u/test_chip_variant.py`.

### Residual gaps (documented, not gated)

- **`phy_ant_select` writes only WLAN_FUN_CTRL + COEXCFG3.** The kernel also writes `MT_CMB_CTRL`
  (`ee_ant`, incl. the `is_mt7630 |= BIT14|BIT11` arm [SRC] phy.c:462), `MT_CSR_EE_CFG1`, and clears
  `COEXCFG0` BIT(2) ([SRC] phy.c:465-468). These three are omitted for **all** cards (the port
  predates the audit and the recorded wire agrees at that cursor). Because `ee_ant` is never written,
  the `is_mt7630` antenna arm has nowhere to land; porting it would require adding the base
  `MT_CMB_CTRL` write, which would change the reference path — so it is left as a whole-card residual,
  not a 0x7630 gate.
- **`SUPPORTED_CHANNELS` is static.** `no_2ghz` / `is_mt7630` mask the advertised band capability
  (`has_2ghz`/`has_5ghz`) but the class-level channel list still lists both bands; a masked band
  yields empty RX rather than a pruned hop list (same shape as the mt76x2u board_type residual).
- **`mt76x0_phy_update_channel_gain` AGC tracker + `phy_set_gain_val` DFS arm** (the `!is_mt7630`
  branch [SRC] phy.c:1063) are unported for every card — a whole-subsystem gap, not a reference
  hardcode (see the 2026-06-21 log).

## Gotchas

**Silicon identifies as MT7650.** `MAC_CSR0` reads `0x76502000` — an MT7650-family chip behind a
7610 USB descriptor, same kernel driver per id_table. `BOARD_TYPE=3` on the dev card → dual-band.

**The uploaded firmware is `mt7610e.bin`, not `mt7610u.bin`.** The kernel tries `mt7610e` first and
that's what Kali actually loaded; the pcap-extracted body matches `mt7610e.bin[32:]` byte-for-byte
and does NOT match `mt7610u.bin[32:]`. Single-stage upload (unlike mt76x2u's two-stage ROM-patch + main).

**`MT_FCE_PSE_CTRL = 1` is written twice back-to-back during FW upload.** Kernel source emits one
write; the cold-boot pcap shows two identical writes in succession, so both are ported verbatim.
Whether the duplicate is load-bearing is untested.

**`Q_SELECT` must be the first MCU command.** The kernel issues `mt76x02_mcu_function_select(Q_SELECT, 1)`
before any other MCU op; confirmed on hardware that `CMD_RANDOM_READ` times out if sent before it.

**Post-FW init is mandatory before the MCU bulk-IN works.** `init_usb_dma` (RX/TX_BULK_EN, RX_DROP_OR_PAD
toggle) + `reset_csr_bbp` (MAC reset cycle) must run after FW_READY, or MCU bulk-IN reads on EP 0x85 time
out — the RX-DMA path isn't armed. Skipping them made the M2 smoke test fail with 5×300 ms timeouts.

**EFUSE, not external EEPROM.** The on-die EFUSE is read via the `MT_EFUSE_CTRL` KICK protocol, NOT via
`MT_VEND_READ_EEPROM` — that bRequest is for an external EEPROM this silicon doesn't have. An unburned
16-byte block reads `AOUT==0x3F` and returns `0xff × 16`.

**MCU register access is base-relative.** MAC regs go on the wire as `MT_MCU_MEMMAP_WLAN (0x410000) + reg`;
RF regs as `MT_MCU_MEMMAP_RF (0x80000000) + MT_RF(bank,reg)`. So `MT_MAC_CSR0` (0x1000) is `0x00411000`.

**5 GHz RX needs the per-channel LNA-gain correction (see Debug log).** It's band- and 5 GHz-subband-specific;
feeding `lna_gain=0` leaves `MT_BBP(AGC,8)` desensitized. Fixed via `eeprom.lna_gain_for_channel`.

**A warm chip needs a WLAN_RESET wake before the cold-boot upload.** connect() always cold-boots, but
re-uploading firmware over a still-running FW inits clean yet never arms RX (an RF/DMA power state the
upload alone doesn't reset). `firmware.reset_dirty_chip`, called from connect() before `load_firmware`,
does the forced WLAN_RESET cycle first when the chip is warm (WLAN_EN set or FW resident). It lives in
connect(), not `load_firmware`, so the `verify_pcap` cold path stays byte-exact against the clean-cold
capture. On Linux device setup's `modprobe -r` cold-re-enumerates the card anyway, so no replug is needed.

## Orientation

Cold boot is orchestrated in `driver.connect()`: M1 FW upload (`firmware.py`) → post-FW init
(`firmware.py`) → MCU bring-up + `Q_SELECT` + EFUSE (`mcu.py`, `eeprom.py`) → M3 MAC/BBP/RF init
(`mac.py`, `phy.py`). The MCU command channel (send_msg / wait_resp / random_read|write /
function_select) lives in `mcu.py`. Vendor control xfers + retry loop + bulk I/O are in `transport.py`.
RF primitives (`rf_wr/rr/rmw`) and `phy_init` (ant_select + rf_init + rxpath + txdac) are in `phy.py`;
channel tune is `phy.set_channel_20mhz`. Init tables ported 1:1 from kernel C are in `initvals_*.py`.

Endpoints after Zadig: bulk-IN 0x84 (RX) / 0x85 (MCU resp), bulk-OUT 0x08 (FW + MCU cmd), 0x04-0x07/0x09 (AC queues).

Monitor-mode RX is already handled (this driver is the source of the monitor-deviation playbook):
`driver.py` clears `MT_RX_FILTR_CFG` PROMISC + OTHER_BSS and overrides the address-match registers with the
bare MAC so unicast DATA (incl. EAPOL, ToDS) isn't dropped.

Names match the kernel C (sources under `data_dumps/mt76-source-v6.18/`), so grep there to cross-reference.

## Scripts

- `find_fw_window.py` — counts FW-reset / IVB-trigger frames to tell a cold pcap from a warm one.
- `extract_mt7610u_fw.py` — byte-verified FW extraction from capture-2.
- `probe_hw.py` — USB descriptor + endpoint + silicon-id dump.
- `beacon_watch.py` — per-channel beacon-rate soak; this is what measured the 5 GHz desense.

## Debug log

### 2026-07-20: always-cold connect() + verify_pcap CHECK A-C green

connect() always cold-boots. `load_firmware` dropped the warm-skip (the early `firmware_running` probe
and the Step 0z pre-reset) and now reproduces the kernel cold-boot prologue byte-for-byte; the warm-chip
WLAN_RESET wake moved to connect()'s `reset_dirty_chip` (before `load_firmware`) so the verify cold path
stays exact. `verify_pcap` CHECK A-C now reproduces all 1134 cold-boot + post-FW ops byte-for-byte (was
red at op#1). Fixes: `transport.write_copy` for the SKEY/WCID clears (one MULTI_WRITE, not N x write32);
the EFUSE physical-size check before the full dump; `phy_ant_select`'s three RMW ops
(CMB_CTRL/CSR_EE_CFG1/COEXCFG0) plus the ee_ant/ee_cfg1 mutations the kernel does. HW: full bring-up
(FW through phy_calibrate) completes with correct register values; 1754 tests pass. Open: monitor RX
over the bulk-IN reader fails on this Windows+WinUSB setup with 'No such device' on the first EP-0x84
read (pre-existing, reproduces on the pre-change code; RX was validated on Linux). A Windows RX-reader
look is the follow-up.

### 2026-06-21 — weak 5 GHz RX: skipped per-channel LNA gain

On 5 GHz the card received but was desensitized — a 60 s CH157 soak on a reference AP held mean
3.5 beacons/s (~36% of a good card's ~9.77/s ceiling), card-wide rather than AP-specific; 2.4 GHz was fine.

Root cause: `set_channel_20mhz` passed `lna_gain=0` and skipped `mt76x0_read_rx_gain` under a "display
only" comment — a skip-rationale that mispredicted the axis. The kernel calls `read_rx_gain` *before*
`set_chan_bbp_params`; it sets a band- and 5 GHz-subband-specific `lna_gain` (2.4 GHz → lna_2g; 5 GHz
ch≤64 → lna_5g[0], ≤128 → lna_5g[1], else → lna_5g[2], and CH157 lands in the last bucket). The BBP
then does `AGC,8 gain -= lna_gain*2`; with 0, the correction never applies. Only the `rssi_offset[]`
half of `read_rx_gain` is display-only — the `lna_gain` half is functional, and the skip conflated them.

Fix: ported the `lna_gain` half as `eeprom.lna_gain_for_channel` (reads `MT_EE_LNA_GAIN` + the two
RSSI-offset words, band/sub-band select, `!=0 && !=0xff` fallback to lna_5g[0], 0xff→0, s8 sign-extend)
and threaded the real value into `set_channel_20mhz`. CH157 soak jumped to mean 9.5/s (~97% of ceiling);
the whole band lifted. Unit test `tests/chips/mt76x0u/test_eeprom_lna_gain.py`. The periodic
`mt76x0_phy_update_channel_gain` AGC tracker is a separate, lower-priority unported gap.
