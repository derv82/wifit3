# RTL8192EU

2.4 GHz, Wi-Fi 4, 2T2R (8192EU) or 1T2R (8191EU), USB 2.0. Mainline `rtl8xxxu`, `rtl8192eu_fops`.

**Status: fully ported and hardware-validated (RX + TX).** The whole bring-up is ported, the chip is
registered (`SUPPORTED_IDS`), and every register sequence runs end-to-end both in an offline
mock-transport smoke test (`tests/.../test_bringup.py`) and **on a real card**.

Hardware run (card `0bda:818b` via usbipd → WSL2 Kali, kernel 6.6.123):
- **Bring-up + RX** (`scripts/.../test_hw.py`): cold-boot (power-on, firmware download, MAC/PHY/RF
  table init, IQ + LC calibration, monitor start) completes and decodes live traffic — ~2159 frames
  in 15 s on ch 1 (27 SSIDs); `set_channel` retunes (ch 6: 24 SSIDs, ch 11: 33 SSIDs, distinct AP
  sets). RSSI reads a plausible −96..−60 dBm.
- **TX** (`scripts/.../test_tx_probe.py`): injecting 30 broadcast Probe Requests drew 51 Probe
  Responses addressed to our MAC (baseline 0) — the frames left the radio and APs answered.

Still open: the rigorous `verify_pcap` replay against a mainline `usbmon` capture, an A/B RX baseline
vs the Linux driver, and active monitor (`FAKE_MAC` is `UNIMPLEMENTED`). Note: our teardown
`power_off` re-enumerates the USB device, so under usbipd the card must be re-`attach`ed between runs.

## Linux Driver Source

- Upstream: mainline `rtl8xxxu` (not DKMS), `drivers/net/wireless/realtek/rtl8xxxu/`.
- Pinned: **v6.6** (`rtl8xxxu_8192e.c`, `rtl8xxxu_core.c`, `rtl8xxxu.h`, `rtl8xxxu_regs.h`). The register
  tables in `tables.py` were extracted from a WSL 6.6 `rtl8xxxu_8192e.c` and verified **byte-identical**
  to torvalds `v6.6`.
- `C` line references in the port cite v6.6.

## VID:PIDs (`rtl8192eu_fops`, core.c device table)

Tested (outside `CONFIG_RTL8XXXU_UNTESTED`): `0bda:818b`, `2357:0108` (TP-Link TL-WN822N v4),
`2001:3319` (D-Link DWA-131 rev E1), `2357:0109`. Untested-guarded: `2357:0107`, `2019:ab33`, `0bda:818c`.

## fops map (8192e.c:1737)

| fops member | Function | Where | Ported |
| --- | --- | --- | --- |
| parse_rx_desc | `rtl8xxxu_parse_rxdesc24` | core.c | ✅ `rx.py` |
| parse_phystats | `rtl8723au_rx_parse_phystats` | core.c | ✅ `rx.py` |
| cck_rssi | `rtl8192e_cck_rssi` | 8192e.c | ✅ `rx.py` |
| fill_txdesc | `rtl8xxxu_fill_txdesc_v2` | core.c | ✅ `tx.py` |
| config_channel | `rtl8xxxu_gen2_config_channel` | core.c | ✅ `core.py` (20 MHz) |
| identify_chip | `rtl8192eu_identify_chip` | 8192e.c | ✅ `rtl8192e.py` |
| parse_efuse | `rtl8192eu_parse_efuse` | 8192e.c | ✅ `rtl8192e.py` |
| read_efuse | `rtl8xxxu_read_efuse` | core.c | ✅ `core.py` |
| load_firmware | `rtl8192eu_load_firmware` | 8192e.c | ✅ `rtl8192e.py` + core.py download |
| power_on | `rtl8192eu_power_on` | 8192e.c | ✅ `rtl8192e.py` |
| power_off | `rtl8192eu_power_off` | 8192e.c | ✅ `rtl8192e.py` |
| reset_8051 | `rtl8xxxu_reset_8051` | core.c | ✅ `core.py` |
| llt_init | `rtl8xxxu_auto_llt_table` | core.c | ✅ `core.py` |
| init_phy_bb | `rtl8192eu_init_phy_bb` | 8192e.c | ✅ `rtl8192e.py` |
| init_phy_rf | `rtl8192eu_init_phy_rf` | 8192e.c | ✅ `rtl8192e.py` |
| phy_iq_calibrate | `rtl8192eu_phy_iq_calibrate` | 8192e.c | ✅ `iqk.py` |
| phy_lc_calibrate | `rtl8723a_phy_lc_calibrate` | core.c | ✅ `core.py` |
| enable_rf | `rtl8192e_enable_rf` | 8192e.c | ✅ `rtl8192e.py` |
| disable_rf | `rtl8xxxu_gen2_disable_rf` | core.c | ✅ `core.py` |
| usb_quirks | `rtl8xxxu_gen2_usb_quirks` | core.c | ✅ `core.py` |
| set_tx_power | `rtl8192e_set_tx_power` | 8192e.c | ✅ `rtl8192e.py` |
| set_crystal_cap | `rtl8723a_set_crystal_cap` | core.c | ✅ `core.py` |
| led_classdev_brightness_set | `rtl8192eu_led_brightness_set` | 8192e.c | ✅ `rtl8192e.py` |

Data constants in the fops are recorded on `Rtl8xxxuFileops`: `writeN_block_size=128`,
`tx_desc_size=40`, `rx_desc_size=24`, `trxff_boundary=0x3cff`, `total_page_num=0xf3`, ADDA path words
`0x0fc01616`, `max_macid_num=128`, `gen2_thermal_meter`, `needs_full_init`, `supports_ap`.

## Port layout

- `transport.py` — USB control/bulk register I/O (core.c `rtl8xxxu_read8`..`writeN`).
- `core.py` — shared core.c structs, register/RF-serial/H2C helpers, `gen2_config_channel`, probe helpers.
- `rtl8192e.py` — 8192e.c chip-specific fops (identity, EFUSE, power, PHY/RF init, enable_rf, set_tx_power, LED) + the `rtl8192eu_fops` table.
- `iqk.py` — the 8192eu IQ calibration and the core.c IQK helpers it reaches.
- `driver.py` — the Driver ABC subclass (connect/set_channel/inject_frame/close).
- `tx.py` / `rx.py` — core.c TX/RX data paths.
- `tables.py` — source-derived register tables; `constants.py` — reference-driven, both generated.

### Non-obvious in the port

- **txdw0 (TX)** is a `u8` (rtl8xxxu.h); RTL8192E assigns `OWN | FIRST | LAST`, but `OWN` is `BIT(31)`
  and truncates off the byte on USB, leaving `FIRST | LAST = 0x0C`. RTL8192E (unlike RTL8192F / RTL8710B)
  does **not** invert the TX checksum (core.c:5554, :5628). Both are the opposite of the RTL8192FU port.
- **`TXDESC_OWN`** has two `#define`s (`BIT(31)` little-endian word, `BIT(7)` big-endian byte); the
  extractor keeps the first (`BIT(31)`), matching the `u8` truncation above.
- RX signal uses the 8723au phystats layout with `rtl8192e_cck_rssi` for CCK; OFDM is
  `(cck_sig_qual_ofdm_pwdb_all >> 1) - 110`. CFO tracking needs an associated STA vif and is omitted.
- C2H: 8192eu has no Bluetooth, so BT_INFO is unreachable; RA_REPORT only feeds cosmetic stats, so
  `rtl8xxxu_c2hcmd_callback` just drains the queue.

## What is not ported (deliberately)

- **Active monitor** (`enter_active_monitor`): `FAKE_MAC = UNIMPLEMENTED`. Forging a MAC for hardware
  ACKs needs careful `REG_MACID` programming best added with the card in hand.
- **Associated-station paths**: `update_rate_mask`, `report_connect`, `report_rssi` are no-ops, and
  `rtl8xxxu_track_cfo` only ports its unassociated branch. wifit3 never associates.
- **40/80 MHz tuning**: only the 20 MHz `config_channel` path (wifit3 tunes 20 MHz).

## Hardware verification (METHODOLOGY.md Steps 1 & 3), to be done by the user

1. On Linux with the card: `sudo python src/wifit3/scripts/capture.py` to record the mainline driver's
   `usbmon` cold-boot + monitor + channel sweep + injection.
2. Add `scripts/chips/rtl8192eu/verify_pcap.py` and replay the port against the capture.
3. `scripts/chips/rtl8192eu/test_hw.py`: RX smoke test, then RX baseline vs the Linux driver.
4. Run FINAL-CHECKLIST.md before registering the chip as supported.

## Firmware

`assets/rtl8192eu_nic.bin` — linux-firmware `rtlwifi/rtl8192eu_nic.bin`, 32286 B,
sha256 `b15bc955fba2fc3abc093affe62c9f7284a0b84d4f13f8ce55366488dc9aad8b`. See `docs/FIRMWARE.md`.

## Final checklist

Per `docs/porting/FINAL-CHECKLIST.md`. Hardware results are from a generic RTL8192EU dongle
(`0bda:818b`) via usbipd → WSL2 Kali (kernel 6.6.123), 2026-11-20, against the operator's own
network only.

1. **Verify gate** — NOT RUN. No `usbmon` capture of the mainline driver was taken, so
   `verify_pcap.py` is not written. The port was built from the pinned v6.6 C source (tables
   byte-identical to torvalds v6.6) and validated live instead (items 3–4). This is the main
   outstanding gate.
2. **Driver vs C** — EFUSE/identify branches follow the C for values the test card never takes,
   behind their real runtime checks: 8191EU 1T2R (bonding), `hi_pa` high-PA AGC table, external-PA
   PHY table. No card value is hard-coded where the C reads it from the chip. No module-level
   mutable state (all per-`priv`). RX delivers frames without the FCS. `FAKE_MAC = UNIMPLEMENTED`
   (active monitor not ported). Offline unit tests cover the TX/RX descriptors, EFUSE decode,
   firmware header and an end-to-end mock-transport bring-up.
3. **Hardware** — bring-up: one cold plug plus several reconnects succeeded on 1 card (each is a
   cold bring-up; `power_off` re-enumerates the device). ACK lab (`scripts/ack/*`, n=100/phase):
   NOT RUN — requires a second wifit3 card as prober/control, which the operator does not have.
4. **Grade** — Linux-vs-wifit3 RX baselines, the channel-hopping soak, and the TX-attack grade are
   NOT RUN (no second card; no baseline sweep taken). Live checks instead: RX ~2159 frames/15 s on
   ch 1 (27 SSIDs), ch 6/11 retune confirmed (distinct AP sets), RSSI −96..−60 dBm; TX 30 broadcast
   Probe Requests drew 51 Probe Responses addressed to our MAC (baseline 0). No bugs observed.
5. **Docs** — chip doc complete; this section recorded. `docs/SUPPORTED-HARDWARE.md` matrix row and
   detail section and the `README.md` supported-hardware row added.
6. **Credits & licensing** — the substantive `rtl8xxxu_8192e.c` contributors are added to
   `docs/CREDITS.md`. Firmware `rtl8192eu_nic.bin` is byte-identical to the installed
   linux-firmware copy; provenance in `docs/FIRMWARE.md`.
7. **Ship** — `uv run pytest` green, `uv run ruff check src/` clean. No real BSSIDs, MACs or SSIDs
   in code, tests, docs or commit messages.

## Scripts

- `scripts/chips/rtl8192eu/extract_tables.py` — regenerates `tables.py` from `rtl8xxxu_8192e.c`.
- `scripts/chips/rtl8192eu/extract_constants.py --source <dir>` — regenerates `constants.py` from the
  referenced `rtl8xxxu_regs.h` / `rtl8xxxu.h` `#define`s.
- `scripts/chips/rtl8192eu/test_hw.py` — RX-only cold-boot smoke test on one channel.
- `scripts/chips/rtl8192eu/test_tx_probe.py` — TX test: inject broadcast Probe Requests, count the
  Probe Responses addressed to our MAC (transmits; run only on a network you control).
- `scripts/chips/rtl8192eu/demo_scan.py` — drives the real wifit3 TUI headlessly (Textual Pilot) to
  scan live APs and save a screenshot.
