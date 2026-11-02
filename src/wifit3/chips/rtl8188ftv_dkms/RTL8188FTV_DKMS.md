# RTL8188FTV — DKMS (vendor) port

## Captured Wireless Card
- No-name `0bda:f179` 802.11b/g/n dongle: 1T1R, 2.4 GHz only, USB 2.
- Captures: `driver_captures/captures_8188fu/`, each `capture-N.pcap` with its `capture-N_logs/`.
  - capture-1, capture-2: cold boot (airmon start, airodump hop, fixed ch1, `iw` sweep ch1-13), no TX.
  - capture-6: station-mode TX reference (associate, DHCP, ping, disconnect).

## Linux Driver Source
- Link: `https://github.com/kelebek333/rtl8188fu` (fork of `ulli-kroll/rtl8188fu`).
- Type: vendor/DKMS, SoftMAC: MLME runs in the host driver (`core/rtw_mlme_ext.c`).
- Version: `v4.3.23.6_20964.20170110`, commit `799fa0beacadce84bef6514d5948b454a8037c96` (2026-09-07).
- Captured on `6.12.107+deb13-amd64`, module `rtl8188fu`.
- Vendored in-repo: `driver_captures/captures_8188fu/driver-source/`.
- Build: stock `Makefile` has `CONFIG_WIFI_MONITOR=n`; captures need `WIFI_MONITOR=y` + `POWER_SAVING=n`.
  Blacklist `rtl8xxxu` / `r8188eu`.

## Python Port Details
- VID/PID: `0bda:f179`; selected by the `rtl8188ftv` family row in `device/manager.py`.
- Status: monitor RX + RSSI, MGMT + DATA TX, spoofable auto-ACK; deauth, handshake, PMKID, WPS PIN live.
  WEP untested.
- Gates: `verify_pcap` byte-matches capture-1 and capture-2 to the last op; `verify_tx` all 87 capture-6 URBs.
- Non-obvious in the port:
  - Registers and FW download are vendor control `bRequest 0x05`, never bulk. RX bulk-IN `0x81`.
  - TX: bulk-OUT `0x02`. Monitor inject sends MGMT and DATA on the MGMT template (mac_id 1, MGNT queue).
  - TX descriptor: 40 B, XOR checksum over the first 32 B. When `(size + 40) % bulk_out_size == 0`,
    the URB is `[desc | 8 B pad | frame]` with PKT_OFFSET=1.
  - Monitor inject retries up to `DATA_RETRY_LIMIT` 12: `rtw_monitor_xmit_entry` sets `retry_ctrl` FALSE
    (`rtw_xmit.c:3841`, `rtl8188f_hal_init.c:4676-4693`).
  - Auto-ACK answers the MAC in REG_MACID (0x610), also in monitor (MSR NOLINK).
    `enter_active_monitor` writes it via `hw_var_set_macaddr` (`rtl8188f_hal_init.c:5117`): `FAKE_MAC = SPOOFABLE`.
  - Monitor RCR appends FCS (`mode._RCR_MONITOR` BIT31): an ACK arrives as 14 B, not 10.
  - ACK tap: `rx.admit_ack_frames` / `drop_ack_frames` toggle RXFLTMAP1 bit 13 (monitor entry leaves 0x0400).
  - Cold plug downloads FW twice: `hal_read_mac_hidden_rpt` powers off (`CardDisable`) after the probe FW.
  - Warm chip (`is_chip_warm`: MCUFWDL != 0x05 or CR != 0): warns, skips the probe FW, runs the rest cold.
  - `FirmwareDownload` always ends with `InitializeFirmwareVars` (`HMETFR=0x0f`), even on failure.
  - RX stays deaf without `misc.hal_init_tail` (MACTXEN|MACRXEN) before the `init_hw_mlme_ext` ch1 tune.
  - `odm_TXPowerTrackingInit` runs twice (`ODM_DMInit` after `phydm_rf_init`): two back-to-back `R32 0xC80`.
  - RF `0x55` gain offset write (`rtw_rf_set_tx_gain_offset`, `core/rtw_rf.c`) only when EFUSE kfree is
    `KFREE_FLAG_ON`.
  - TX power limit is compiled off (`CONFIG_CALIBRATE_TX_POWER_TO_MAX=y`): CCK lane = base + `Remnant_CCKSwingIdx`.
  - Thermal tracking uses the MP USB `2GCCKA_P/N` deltas: `TxRate` stays MGN_1M in monitor (`HwRateToMRate(0)`).
  - `phydm_ra_dynamic_retry_count` writes `0x430=0` / `0x434=0x04030201` on the first watchdog tick only.
  - `SUPPORTED_CHANNELS` = `chplan.init_channel_set(params.chplan)`: EFUSE plan 0x20 → ch1-13 (ch14 off).
  - Poll counts vary run to run (power flow 34 vs 37 ops): gates assert byte-exact replay, never op counts.
  - capture-2 has a 127-op watchdog-vs-ioctl USB race at a ch9 switch; `verify_pcap` merge-walks it.

## Known Problems
- capture-1 starts at the power flow (tshark opened late): chip version + EFUSE verify against capture-2 only.
- capture-1/2 have no bulk-OUT: the vendor's monitor TX never reaches USB. `rtw_monitor_xmit_entry` drops
  any radiotap that isn't 12 B, and carrier stays off in monitor. The port injects over PyUSB, unaffected.
- The C2H hidden report never posts live (reads back 0xFD). Descriptive caps only.

## Driver Entry Points
- Bring-up: `driver.connect` → probe + EFUSE → power → FW (`assets/rtl8188fufw.bin`) → MAC/BB/RF →
  DM init + LC + IQK → station opmode → monitor entry → `RxReaderThread`.
- DM watchdog: `dm.watchdog_tick` every 2 s (`WIFIT3_RTL8188FTV_WATCHDOG=off` disables).
- EFUSE: `ReadAdapterInfo8188FU` → `Efuse_PgPacketRead` + `HalEfuseMask8188F_USB` + `Hal_EfuseParse*`.
- Power off: `CardDisableRTL8188FU`.
- Firmware: `firmware.download_firmware` + `init_firmware_vars`.
- Monitor entry: `cfg80211_rtw_change_iface` → `hw_var_set_monitor`.
- Channel tune: `cfg80211_rtw_set_monitor_channel` → `set_channel_bwmode` → `rtw_hal_set_chnl_bw`.
- RX: `rtl8188fu_inirp_init` + `recvbuf2recvframe` + `rtl8188f_query_rx_desc_status`.
- TX: `tx.inject_frame` (`rtl8188fu_hal_xmit` / `mgnt_xmit` + `rtl8188f_update_txdesc`).
- Auto-ACK MAC: `enter_active_monitor` / `exit_active_monitor` → `hw_var_set_macaddr`.
- ACK detection: `rx.admit_ack_frames` + `_rx_dispatch` → `record_ack`.

## Scripts
- `scripts/chips/rtl8188ftv_dkms/capture_vendor_8188fu.sh`: pinned monitor-enabled DKMS build + `capture.py`.
- `scripts/chips/rtl8188ftv_dkms/verify_pcap.py`: cold-boot byte gate, capture-1 and capture-2.
- `scripts/chips/rtl8188ftv_dkms/verify_tx.py`: bulk-OUT byte gate against capture-6.
- `scripts/chips/rtl8188ftv_dkms/verify_warm.py`: power-on + single FW against a warm reference capture.
- `scripts/chips/rtl8188ftv_dkms/verify_rx.py`: decodes every bulk-IN completion of a capture.

## Debug log
- 2026-11-02: DoIQK and LCK have not fired on hardware. 20-min soak, warm chip: max delta_iqk 7 (< 8).
