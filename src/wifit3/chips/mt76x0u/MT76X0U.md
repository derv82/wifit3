# MT76x0U / MT7610U

A port of the kernel `mt76x0u` driver (mt76 family, `mt76x02` generation — older sibling of
mt76_connac, shares `mt76x02_*.c` helpers with mt76x2u). 1T1R, 2.4 + 5 GHz, firmware-based, in-band
MCU command channel. Dev card is `0e8d:7610` (Sabrent / MediaTek MT7610U).

## Status

- Cold boot through PHY init working on hardware: FW upload, post-FW DMA/MAC reset, MCU channel, EFUSE, MAC/BBP/RF init.
- 2.4 GHz monitor RX: working.
- 5 GHz monitor RX: working after the per-channel LNA-gain fix (see Gotchas); CH157 soak ~97% of a good card's beacon rate.
- `verify_pcap`: clean against the cold-boot pcap (capture-2).
- Not ported: the periodic RSSI-driven AGC tracker (`mt76x0_phy_update_channel_gain` / `calibration_work`) — dynamic gain isn't re-seeded, lower-priority gap. RSSI-display (`rssi_offset[]`) half of `read_rx_gain` also unported (display-only).

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

**Warm bring-up doesn't restore RX — a power-cycle is required.** Coming up from a still-running FW
(force-reset + re-upload) inits clean with no error, but RX never flows; only a real cold boot does.
The Linux take-control flow unloads the kernel driver and leaves the card warm, so the driver sets
`LINUX_REPLUG_AFTER_TAKEOVER = True` and the splash asks for a replug instead of auto-connecting. A
normal cold plug is unaffected. (Why warm fails to arm RX is unconfirmed — likely an RF/DMA power
state the re-upload doesn't reset.)

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
`mt76x0_phy_update_channel_gain` AGC tracker is a separate, lower-priority unported gap. User to HW-test
the full 5 GHz attack suite.
