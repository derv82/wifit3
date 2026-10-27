# RTL8192CU

## Captured Wireless Card
- 2.4 GHz only. Captured card is an RTL8188CUS, 1T1R (`REG_SYS_CFG` 0x04400735 → `VERSION_NORMAL_TSMC_CHIP_88C`, 0x10).
- `0bda:8176`, USB 2.0 high-speed. Endpoints: bulk IN 0x81, bulk OUT 0x02 + 0x03, interrupt IN 0x84.
- Capture: `driver_captures/captures_rtl8192cu_caponly/capture-2.pcap` (cold boot, 6298 vendor register ops, all EP0 control; no bulk-OUT). No timeline or driver log in the bundle.
- Firmware uploaded in the capture is byte-identical to `rtl8192cufw_TMSC.bin` (16094-byte payload after the 32-byte header); copies live in `driver_captures/captures_rtl8xxxu_2/firmware/`.

## Linux Driver Source
- Repo: linux-stable, `drivers/net/wireless/realtek/rtlwifi/` (`rtl8192cu/`, plus the shared `rtl8192c/`, `rtl8192ce/*.h`, `usb.c`, `efuse.c`, `wifi.h`).
- Type: mainline rtlwifi (module `rtl8192cu`), not rtl8xxxu and not the Realtek vendor `8192cu` DKMS.
- Pinned: tag v6.12.107, commit `f717995cb7dcd8998ab15516b8006aea09cfde0d`.
- Vendored (sparse checkout): `driver_sources/rtlwifi-source-v6.12.107/`.
- Driver identity evidence from the capture:
  - op 2 writes 0x69 to register 0x0000: `efuse_power_switch` writes `maps[EFUSE_ACCESS]`, which `rtl92cu_hal_cfg` never sets (efuse.c:1109, sw.c:166-185). rtl8xxxu writes 0x69 to `REG_EFUSE_ACCESS` 0x00CF and reads `REG_9346CR` as 16-bit (`rtl8xxxu-source-v6.18` core.c:1788-1803); the capture reads it as 8-bit.
  - EFUSE byte read is `read_efuse_byte` exactly: W 0x31, R/W 0x32, R/W 0x33, R32 0x30 poll, R32 0x30 after the 50 us delay.
  - Firmware goes up in 64-byte control writes, then 8-byte, then 1-byte (`_rtl_fw_block_write_usb`, efuse.c:1299), one page select per 4 KiB page via `REG_MCUFWDL+2`.
  - Power-on (ops 1492-1536) follows `_rtl92cu_init_power_on` read-for-read (by inspection).
- Kernel version: not pinned by the capture (no driver log). The probe window replays byte-for-byte against v6.12.107; the power-on and FW-download sequences match it by inspection.

## Python Port Details
- VID:PID: the full `rtl8192c_usb_ids[]` table (72 IDs) in `__init__.py`; no other package claims them.
- Status: probe ported (chip version, EFUSE shadow map + parse, USB endpoint config, firmware name selection). `verify_pcap` replays the probe window 1492/1492 ops byte-for-byte. `connect()` stops with `BringUpError` after probe: `rtl92cu_hw_init` onward is not ported.
- Related ports: none share code (rtl8xxxu also drives this silicon upstream; no rtl8xxxu 8192C port exists here).
- Non-obvious in the port:
  - `maps[EFUSE_ANA8M]` holds the `rtl_var_map` enum index 18, not `ANA8M` (sw.c:182); the SYS_CLKR check in `efuse_power_switch` tests mask 0x12.
  - `rtlhal.hw_type` is `HARDWARE_TYPE_RTL8192E` (0) during probe; `rtl92cu_hw_init` sets `HARDWARE_TYPE_RTL8192CU` (hw.c:884). `efuse_power_switch` branches on it.
  - `IS_92C_SERIAL` is false for RF-type bits 0b11, so a 92C 1T2R chip (version 0x13) gets `rf_type = RF_1T1R` (mac.c:97, fw_common.h:31-35).
  - `_out_ep_mapping` passes `ischipn` as `bwificfg` to `threeoutepmapping` (trx.c:173), so a 3-OUT normal chip always takes the WMM mapping.
  - `rtl_get_hwinfo` returning 1 (autoload fail, EEPROM boot, bad EEPROM ID) skips the TX-power and board-type parse entirely (hw.c:334).

## Known Problems
- `connect()` cannot bring the card up: `rtl92cu_hw_init`, firmware download, monitor setup, channel tune, RX and TX are not ported.
- `tests/chips/test_inject_frame.py` skips this package (`TX_NOT_PORTED`) until the TX path exists.

## Driver Entry Points
- Probe (`rtl_usb_probe`, usb.c:951) → `usb.rtl_usb_probe`
- Chip version (`rtl92c_read_chip_version`, mac.c:27) → `mac.rtl92c_read_chip_version`
- EFUSE read (`rtl_efuse_shadow_map_update`, `read_efuse`, efuse.c) → `efuse.py`
- EEPROM parse (`rtl92cu_read_eeprom_info`, hw.c:393) → `hw.rtl92cu_read_eeprom_info`
- Endpoint config (`rtl8192cu_endpoint_mapping`, trx.c:188; `_rtl_usb_init`, usb.c:236) → `trx.py`, `usb._rtl_usb_init`
- Firmware name (`rtl92cu_init_sw_vars`, sw.c:31) → `usb.rtl92cu_init_sw_vars`
- Register I/O (`_usbctrl_vendorreq_sync`, usb.c:28) → `transport.RTL8192CUTransport`

## Capture phase map (capture-2, vendor ops)
- Frames before 54: USB-core enumeration (descriptors, SET_CONFIGURATION); no vendor requests. `verify_pcap` starts at the first vendor op.
- 0: `REG_SYS_CFG` read (chip version; 88C so no `REG_HPON_FSM` read).
- 1: `REG_9346CR` read.
- 2-1489: `efuse_read_all_map` (power switch on, `read_efuse`, power switch off).
- 1490-1491: `configvernoutep` reads `REG_NORMAL_SIE_EP+1`/`+2`.
- 1492: `rtl92cu_hw_init` starts (t=6.3 s; the 5.9 s gap is mac80211 registration until interface up, no register traffic).
- 1492-1536: `_rtl92cu_init_power_on`. 1537-2048: `rtl92c_init_llt_table`. 2110-2386: `rtl92c_download_fw`.
- 3211: first read of `REG_RCR` (0x0608); 22 such reads, ~2 s apart from 3330 on.

## Scripts
- `scripts/chips/rtl8192cu/verify_pcap.py`: replay-diff of the probe window (also `scripts/porting/verify_pcap.py rtl8192cu`).

## Debug log
