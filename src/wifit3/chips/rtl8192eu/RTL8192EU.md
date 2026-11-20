# RTL8192EU

2.4 GHz, Wi-Fi 4, 2T2R (8192EU) or 1T2R (8191EU), USB 2.0. Mainline `rtl8xxxu`, `rtl8192eu_fops`.

**Status: ported from v6.19.14, RX + TX + active monitor on hardware; ungraded.**

`verify_pcap` passes against 6 mainline v6.19.14 `usbmon` captures (2 cards, 3 each) as of `60c9d6a2`:
99.92% of driver ops byte-matched; the 7 unmatched ops are mac80211 `configure_filter` re-writes of
REG_RCR.

| Commit | Change |
|---|---|
| `43e2683f` | `scripts/chips/rtl8192eu/verify_pcap.py`; `_bringup()` split out of `connect()` |
| `c2d9c27d` | v6.19.14 port. The divergences verify_pcap found, in order: REG_EFUSE_CTRL read before the EFUSE poll; RF init tables through `rtl8xxxu_init_phy_rf`; REG_FPGA0_POWER_SAVE toggle around RF writes; no set_mac/set_linktype in init_device; 20_NOHT SIFS; re-tune on the current channel; `TXDESC_OWN` BIT(7). Plus paths no capture reaches: firmware retry, MAC-table abort, RTL8191E, power_off, EFUSE check order, RX phy_stats. |
| `60c9d6a2` | active monitor (SPOOFABLE), beacons on MGNT, RX reader in C order |

Hardware (TL-WN821N `2357:0107`, Windows, ref AP on ch 1, 7925 as prober/sniffer):
- RX: ref AP 6.6–8.7 beacons/s (AP sends 9.8/s), 30 s runs, PR and this port alike.
- TX (tx_lab): broadcast 100/100 landed, unicast 7 copies (HW retry limit), FakeAP beacons 265/283.
- ACKs (rx_autoack): spoofed MAC ACKed with active monitor on, 0/100 off; bogus MAC 0/100.
- hop_errors: 0 errors, ~80 tunes/s; 6 open/close cycles clean.

Still open: an RX baseline vs the Linux driver, the soak, grading.
Note: our teardown `power_off` re-enumerates the USB device, so under usbipd the card must be
re-`attach`ed between runs.

## Linux Driver Source

- Upstream: mainline `rtl8xxxu` (not DKMS), `drivers/net/wireless/realtek/rtl8xxxu/`.
- Pinned: **v6.19.14** (`driver_sources/rtl8xxxu-source-v6.19.14/`: `8192e.c`, `core.c`, `rtl8xxxu.h`,
  `regs.h`). The port was first written against v6.6; `tables.py` and `constants.py` are still
  generated from v6.6 and are identical to v6.19.14 for everything the port uses.
- `C` line references in the port cite v6.19.14.
- v6.19.14 differences the port follows: `init_device` no longer programs REG_MACID / MSR (only
  `add_interface` does, which a monitor vif never calls).

## VID:PIDs (`rtl8192eu_fops`, core.c device table)

Tested (outside `CONFIG_RTL8XXXU_UNTESTED`): `0bda:818b`, `2357:0108` (TP-Link TL-WN822N v4),
`2001:3319` (D-Link DWA-131 rev E1), `2357:0109`. Untested-guarded: `2357:0107`, `2019:ab33`, `0bda:818c`.

## fops map (8192e.c:1721)

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

- **txdw0 (TX)** is a `u8` (rtl8xxxu.h:417); RTL8192E assigns `OWN | FIRST | LAST = 0x8C`
  (core.c:5470). RTL8192E (unlike RTL8192F / RTL8710B) does **not** invert the TX checksum. Both are
  the opposite of the RTL8192FU port.
- **`TXDESC_OWN`** is `BIT(7)`. rtl8xxxu.h:468-472 also has a dead `#if 0` `BIT(31)`; the constants
  extractor skips `#if 0` blocks.
- **`rtl_chip`**: 8192e.c identifies RTL8191E (1T2R bonding) or RTL8192E, and core.c branches on it at
  11 sites; both sides are ported. Only RTL8192E is tested on hardware.
- **Beacons** go on the MGNT queue, not TXDESC_QUEUE_BEACON as in core.c:5018: an injected frame on
  the beacon queue never airs (0/283 heard).
- **RX reader** starts where `rtl8xxxu_start` submits its RX URBs and pauses where `rtl8xxxu_stop`
  kills them.
- RX signal uses the 8723au phystats layout with `rtl8192e_cck_rssi` for CCK; OFDM is
  `(cck_sig_qual_ofdm_pwdb_all >> 1) - 110`. CFO tracking needs an associated STA vif and is omitted.
- C2H: 8192eu has no Bluetooth, so BT_INFO is unreachable; RA_REPORT only feeds cosmetic stats, so
  `rtl8xxxu_c2hcmd_callback` just drains the queue.

## What is not ported (deliberately)

- **Associated-station paths**: `update_rate_mask`, `report_connect`, `report_rssi` are no-ops, and
  `rtl8xxxu_track_cfo` only ports its unassociated branch. wifit3 never associates.
- **40/80 MHz tuning**: only the 20 MHz `config_channel` path (wifit3 tunes 20 MHz).

## Hardware verification

- Captures: Kali, kernel 6.19.14, mainline `rtl8xxxu`, `capture.py`. TP-Link TL-WN823N (`2357:0109`,
  stock module) and TL-WN821N (`2357:0107`, same source rebuilt with `CONFIG_RTL8XXXU_UNTESTED`), 3 each.
- `uv run python scripts/porting/verify_pcap.py rtl8192eu <capture>`: all 6 PASS (see Status).
- Still to do: RX baseline vs the Linux driver, soak, FINAL-CHECKLIST.md.

## Firmware

`assets/rtl8192eu_nic.bin` — linux-firmware `rtlwifi/rtl8192eu_nic.bin`, 32286 B,
sha256 `b15bc955fba2fc3abc093affe62c9f7284a0b84d4f13f8ce55366488dc9aad8b`. See `docs/FIRMWARE.md`.

## Final checklist

Per `docs/porting/FINAL-CHECKLIST.md`. Hardware results: TL-WN821N (`2357:0107`) on Windows, MT7925AU
as prober/sniffer, 2026-11-20, against the operator's own network only.

1. **verify_pcap** — PASS on 6 v6.19.14 captures (2 cards), 99.92% byte-matched, 7 REG_RCR writes
   waived as mac80211 `configure_filter`. The captures were taken on a native Linux host; WSL2's
   usbipd forward deadlocks the kernel driver's firmware download.
2. **Driver vs C** — EFUSE/identify branches follow the C for values the test card never takes,
   behind their real runtime checks: 8191EU 1T2R (bonding, `rtl_chip`), `hi_pa` high-PA AGC table,
   external-PA PHY table. Bring-up failure paths follow the C: firmware download retried up to 6
   times, MAC table write failure aborts, `power_off` carries on after a MAC-disable timeout. No card value is hard-coded where the C reads it from the chip. No module-level
   mutable state (all per-`priv`). RX delivers frames without the FCS. `FAKE_MAC = SPOOFABLE`.
   Offline unit tests cover the TX/RX descriptors, EFUSE decode, firmware header, active monitor,
   the failure paths and an end-to-end mock-transport bring-up.
3. **Hardware** — bring-up: repeated cold bring-ups on 1 card, 6 open/close cycles clean. ACK lab
   (`scripts/ack/rx_autoack.py`, 7925 prober, n=100/phase, 1 run): spoofed MAC ACKed with active
   monitor on, 0/100 off; bogus MAC 0/100. tx_lab: see Status.
4. **Grade** — Linux-vs-wifit3 RX baselines, the channel-hopping soak, and the TX-attack grade are
   NOT RUN. Live checks: see Status.
5. **Docs** — chip doc, `docs/SUPPORTED-HARDWARE.md` row and section, `README.md` row.
6. **Credits & licensing** — the substantive `rtl8xxxu_8192e.c` contributors are added to
   `docs/CREDITS.md`. Firmware `rtl8192eu_nic.bin` is byte-identical to the installed
   linux-firmware copy; provenance in `docs/FIRMWARE.md`.
7. **Ship** — `uv run pytest` green, `uv run ruff check src/` clean. No real BSSIDs, MACs or SSIDs
   in code, tests, docs or commit messages.

## Scripts

- `scripts/chips/rtl8192eu/verify_pcap.py` — replays the port against a mainline `usbmon` capture
  (run through `scripts/porting/verify_pcap.py rtl8192eu`).
- `scripts/chips/rtl8192eu/extract_tables.py` — regenerates `tables.py` from `rtl8xxxu_8192e.c`.
- `scripts/chips/rtl8192eu/extract_constants.py --source <dir>` — regenerates `constants.py` from the
  referenced `rtl8xxxu_regs.h` / `rtl8xxxu.h` `#define`s.
- `scripts/chips/rtl8192eu/test_hw.py` — RX-only cold-boot smoke test on one channel.
- `scripts/chips/rtl8192eu/test_tx_probe.py` — TX test: inject broadcast Probe Requests, count the
  Probe Responses addressed to our MAC (transmits; run only on a network you control).
- `scripts/chips/rtl8192eu/demo_scan.py` — drives the real wifit3 TUI headlessly (Textual Pilot) to
  scan live APs and save a screenshot.
