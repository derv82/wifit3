# RTL8192EU

Realtek RTL8192EU: WiFi 4 (802.11n), 2.4 GHz only, **2T2R**, firmware-based (8051), USB 2.0.
Cleanroom port of the mainline `rtl8xxxu` driver's 8192e fileops vector (`8192e.c` + shared
`core.c`/`regs.h`) — not rtw88, and not the 1T1R RTL8188EU or the RTL8192CU silicon.

## Captured Wireless Card

- Realtek RTL8192EU, 2.4 GHz only, 2 TX / 2 RX chains, USB 2.0 (high-speed).
- Captured unit: TP-Link TL-WN823N v2/v3, `VID:PID 2357:0109`, on a USB 2.0 host (Bus 001).
- Captures (not vendored — outside the repo):
  `C:\Users\Heinz\wifit3-port\captures\captures_rtl8xxxu\` (capture-1) and
  `C:\Users\Heinz\wifit3-port\captures\extracted2\captures_rtl8xxxu\` (capture-2).
  Recorded on Kali with the mainline `rtl8xxxu` kernel driver, kernel `6.19.14+kali-amd64`.
  - `capture-1.pcap` — 4,085,544 B, sha256
    `03016e902d7f57e22d8acb40587ca06366d53e5853aaa305bb7fbcd7410be789`.
    Cold plug-in; airodump-ng hop + fixed-ch1; `iw dev wlan0 set channel` sweep 1–14; **no TX**.
  - `capture-2.pcap` — 3,499,184 B, sha256
    `3cbb2a3aa7ee277647536936cbf639967f8a7825f39cd74c9400fc50e208899b`.
    As capture-1, plus `aireplay-ng --test` and one deauth on ch 4
    (BSSID `C0:06:C3:C9:EC:0B`, client `D0:3F:27:22:3E:EF`).
- Both captures carry the same cold bring-up + ch 1–14 sweep. They come from the kernel driver,
  not from this port; there is no wifit3-produced capture (see [Known Problems](#known-problems)).

## Linux Driver Source

- Upstream: mainline `drivers/net/wireless/realtek/rtl8xxxu/` — 8192e-specific code in `8192e.c`,
  the shared core in `core.c` / `regs.h`.
- Type: mainline.
- **Commit not pinnable.** Vendored (gitignored) at `driver_sources/rtl8xxxu-source-v6.19/` with
  no commit hash and no `VERSION.md`; the source base is kernel v6.19, and the captures came from
  `6.19.14+kali-amd64`. Every `file:line` citation below resolves against that vendored tree.
- The 8192eu fileops vector is `rtl8192eu_fops` (`8192e.c:1721-1767`).

## Python Port Details

- VID/PID: registered for every ID whose `.driver_info` is `rtl8192eu_fops` (`core.c:7948-8222`):
  `0bda:818b`, `0bda:818c`, `2357:0107`/`0108`/`0109`/`0126`, `2001:3312`/`3319`,
  `2019:ab33`, `2c4e:0100`/`0104`. The captured TL-WN823N v2/v3 is `2357:0109`.
- Selected by `chips/rtl8192eu/__init__.py` (`SUPPORTED_IDS` + lazy `import_driver()`); no
  `_FAMILIES` row is needed (there is no mainline/DKMS pair for this part).
- Status: **ported, byte-verified against `capture-1`, and run on hardware** (cold bring-up + 2.4 GHz
  monitor scan, 2026-10-17). The cold bring-up (power-on → queue pages → FW → EFUSE → MAC →
  BB/AGC/RF → TX boundary/LLT → RX path → adaptive + beacon → TX power → LC/IQK →
  `enable_rf`), 2.4 GHz / 20 MHz monitor RX, MGMT inject (deauth) and hardware active monitor
  (`FAKE_MAC = SPOOFABLE`, `REG_MACID` re-pointed) are implemented and unit-tested against a mocked
  USB transport (`tests/chips/rtl8192eu/`, 145 tests).
  `scripts/chips/rtl8192eu/verify_pcap.py` replays the cold boot against `capture-1` and PASSes all
  seven gates (FW blob, MAC+PHY, queue-pre-FW, post-PHY, beacon, LCK, IQK — 2538 replayed ops
  across seven disjoint regions; see [Scripts](#scripts)). `scripts/chips/rtl8192eu/test_hw_rtl8192eu.py
  --phase scan` brings the card up for real and sees 13 BSSIDs over 1–13 (see
  [Debug log](#debug-log)). **Injection is proven on the air** (`--phase inject`, 2026-10-17: a real AP
  ACKed 48/50 frames, bogus-MAC control 1/50); the hardware auto-ACK of a forged MAC is not yet
  bench-verified — that needs a second radio.
- Related port: `rtl8188eus` (same `rtl8xxxu` core; 1T1R sibling).
- Non-obvious in the port (would cost a maintainer time):
  - **`txdesc40` (40 B) + `rxdesc24` (24 B) + `fill_txdesc_v2`** — *not* the 8188e's `txdesc32` /
    `rxdesc16` / `fill_txdesc_v3` (`8192e.c:1744,1749-1750`). MGMT rate/retry live in txdw3/txdw4;
    the 40-byte descriptor's checksum is XOR-16 over its **first 32 bytes only**.
  - **Monitor RCR is `0x7000780F`, with no `ACCEPT_CRC32`/`ACCEPT_ICV`.** The kernel writes the
    station RCR `0x7000600E` during init (`core.c:4130-4133`) and the monitor RCR `0x7000702F` from
    `rtl8xxxu_configure_filter` (`core.c:6884-6897`); this port is monitor-only, so it writes its own
    monitor superset once. It keeps `ACCEPT_DATA_FRAME` (bit 11, needed for ToDS EAPOL because the
    8192e fops has no `.init_reg_rxfltmap`) and drops `ACCEPT_PM` (bit 5), the two bits that differ
    from the kernel's monitor value. The `rtl8188eus` sibling computes the same `0x7000780F`.
    CRC/ICV-failed frames are HW-dropped; nothing downstream consumes them.
  - **Firmware uploads as 4096-byte pages written in 128-byte vendor blocks**
    (`.writeN_block_size = 128`, `8192e.c:1748`); each page is selected via `REG_MCU_FW_DL + 2`
    and the payload is `len(blob) - 32` (the 32-byte `rtl8xxxu_firmware_header` is stripped).
  - **Auto-LLT** (`.llt_init = rtl8xxxu_auto_llt_table`, `8192e.c:1729`): set `AUTO_LLT_INIT_LLT`
    and poll it clear — not the 8188e's 176 hand-written LLT entries.
  - **Init order is not the 8188e's.** Queue pages + DMA lanes + `REG_TRXFF_BNDY+2` run *before*
    the FW upload (`core.c:3952-3965`); the TX buffer boundary, PBP skip, auto-LLT and gen2 USB
    quirks run *after* the PHY init (`core.c:4021-4054`); the beacon/TSF block sits immediately
    after `REG_ACKTO` (`core.c:4197-4211`). The 8188e's "hold MAC TX/RX until after
    `REG_TRXFF_BNDY`" workaround does **not** apply: `rtl8192eu_power_on` already sets both bits
    (`core.c:1368` on the wire, `REG_CR = 0x6ff`), so there is no second `REG_CR` write. The cold
    path also does **not** tune a channel: the kernel only sets the TX power in `init_device`
    (`core.c:4242`) and tunes in `rtl8xxxu_config` (`core.c:6785-6793`, power *then* radio), which
    is what `driver.set_channel` mirrors. `current_channel` is therefore `None` until the first
    tune.
  - **TX queues come from the endpoints, not a constant.** `REG_NORMAL_SIE_EP_TX` (0xfe66) reads 0
    on this part, so `mac.config_endpoints` falls back to the bulk-OUT endpoint count
    (`core.c:1717-1744`, `8192e.c:500-507`). The TL-WN823N exposes three, which yields
    high + normal + low; `REG_RQPN = 0x80de0c08` (low queue included) is the wire value — the
    earlier hard-coded two-queue split produced `0x80ea0c08` and was wrong.
  - **2T2R everywhere** (`rf_paths = 2`, `8192e.c:490`): both RF paths are tuned, calibrated and
    TX-powered. Path B has its own XB HSSI/LSSI register set; `write_rfreg` brackets every SIPI
    write with `REG_FPGA0_POWER_SAVE` BIT(17), or the write silently fails.
  - **`RCR_MONITOR` must be reasserted on the warm path.** `_finish_attach` calls
    `apply_monitor_rx_filter` on both cold + warm, because `_warm_reattach` skips the cold RX-enable
    block and would otherwise leave a non-promiscuous RCR that drops ToDS (client→AP) EAPOL.
  - **CCK RSSI depends on the AGC-report type.** `phy.sync_cck_agc_report` reads `cck_agc_report_type`
    from `REG_FPGA0_XA_HSSI_PARM2` bit 9 and selects the CCK LNA gain table; OFDM uses
    `(pwdb >> 1) - 110`. The 8192eu phy-stats offsets are byte 4 (pwdb) / byte 5 (CCK AGC), as in
    the 8188e port, but the CCK table is 8192e-specific.

## Known Problems

- **Injection is proven on the air, but narrow.** `--phase inject --count 50` (2026-10-17) fired unicast
  MGMT at a real AP on ch 4 and got **48/50 of that AP's ACKs back** (bogus-MAC control 1/50): injection
  reaches the air, the frames are well-formed, and the RX ACK tap admits the AP's replies. Not covered:
  data frames, aggregation, per-rate/TX-power behaviour, and any BSSID other than the one tested.
- **TX is MGMT-inject only.** `tx.build_tx_desc_mgmt` / `send_mgmt_frame` cover deauth (and any
  MGMT frame). Data frames, aggregation and TX-report consumption are out of scope (`tx.py`).
- **40/80 MHz unported.** Only 2.4 GHz / 20 MHz (`chan.set_channel_2g_20mhz`); the chandef fields
  the 40/80 branches need are never set on this monitor path.
- **Active monitor is implemented but not bench-proven.** `enter_active_monitor` writes the forged
  MAC to `REG_MACID` (6 byte writes, `rtl8xxxu_set_mac`, `core.c:3558-3582`) and caches the previous
  six bytes on the first call so `exit_active_monitor` restores them; `FAKE_MAC = SPOOFABLE` unblocks
  EvilTwin's twin host. The hardware auto-ACK of a forged RA has **not** been measured: the verdict
  instrument is `scripts/ack/rx_autoack.py --test-card 8192 --channel <ch>` with a second prober card.
  The single-card `test_hw_active_monitor.py` ran on this silicon (spoofed 0/100, monitor-off 0/100,
  bogus 0/100) — a single radio cannot hear its own ACK, so that is inconclusive, not a negative.
  Note `REG_MACID` reads `00:00:00:00:00:00` after bring-up: the kernel only programs it in
  `rtl8xxxu_set_mac` when an interface is added, and this port is monitor-only, so a twin/STA MAC is
  the first thing written there.
- **EFUSE is required, on the cold *and* warm path.** `_cold_bring_up` and `_warm_reattach` both
  raise `BringUpError("efuse", ...)` when `efuse.read_and_parse` fails, mirroring the kernel's
  `-EINVAL`. The warm path re-reads it because `set_channel` re-applies the per-group TX power on
  every tune (`core.c:6791`), and the `0x22` defaults would otherwise clobber the calibration the
  previous session programmed. A chip that cannot be read needs a replug, not silent
  under-powered TX.

## Driver Entry Points

Feature → where to start reading. Names match the mainline C; grep the vendored
`driver_sources/rtl8xxxu-source-v6.19/`.

- Bring-up: `driver.connect` → `_cold_bring_up` / `_warm_reattach` (mirrors `rtl8xxxu_init_device`
  `core.c:3918-4412` plus the `enable_rf` tail of `rtl8xxxu_start`, `core.c:7376`).
- Power-on: `driver._power_on` (`rtl8192eu_power_on`, `8192e.c:1547-1593`) — LDO/crystal AFE trim,
  `disabled_to_emu`, `emu_to_active`, `REG_CR` MAC enable.
- Firmware: `firmware.download_firmware` / `start_firmware` / `reset_8051` / `firmware_self_reset`.
- EFUSE: `efuse.read_and_parse` → `efuse.parse_efuse_8192eu` (`8192e.c:588-637`) — per-path CCK +
  HT40-1S bases, per-TX-count OFDM/HT20/HT40 diffs, MAC, crystal cap.
- Queue init: `mac.pre_fw_queue_init` (`config_endpoints` → `init_queue_reserved_page` +
  `init_queue_priority` + `REG_TRXFF_BNDY+2`), run before the FW upload (`core.c:3952-3962`).
- MAC init: `mac.post_fw_mac_init` (`apply_mac_init_table`, `core.c:3982`).
- Post-PHY MAC: `mac.post_phy_mac_init` (TX buffer boundary + `set_pbp` skip + auto-LLT +
  `phy.usb_quirks`, `core.c:4021-4054`).
- Active monitor: `driver.enter_active_monitor` / `exit_active_monitor` → `mac.set_mac_addr`
  (`REG_MACID`, `core.c:3558-3582`); the pre-spoof MAC is read back from the register, so the warm
  path restores without an EFUSE parse.
- BB / RF init: `phy.post_mac_init_phy` (`init_phy_bb` + `set_crystal_cap` + `init_phy_rf_8192e`);
  tables in `phy_tables.py`.
- RX path enable: `mac.enable_rx_data_path` (DRVINFO_SZ + HIMR=0 + `RCR_MONITOR` + MAR);
  `mac.init_adaptive_controls`.
- Channel tune: `chan.set_channel_2g_20mhz` (`rtl8xxxu_gen2_config_channel`); SIPI read in
  `chan.read_rfreg`.
- TX power: `phy.set_tx_power` (`rtl8192e_set_tx_power`, `8192e.c:512-586`) +
  `phy.channel_to_group_8192e` (`rtl8xxxu_gen2_channel_to_group`).
- Calibration: `iqk.phy_lc_calibrate` (`rtl8723a_phy_lc_calibrate`) then `iqk.phy_iq_calibrate`
  (`rtl8192eu_phy_iq_calibrate`, 2T2R path A then path B).
- RF enable: `phy.enable_rf` (`rtl8192e_enable_rf`, `8192e.c:1629-1673`), after init.
- RX: `rx.parse_rxdesc24` / `rx.iter_bulk_frames` / `rx.parse_phystats_rssi` +
  `rx._rtl8192e_cck_rssi`; the reader is the shared `chips/rx_reader.py` `RxReaderThread`.
- TX / inject: `tx.build_tx_desc_mgmt` + `tx.calc_tx_desc_csum` + `tx.send_mgmt_frame`;
  `driver._inject_frame` (bcast inferred from addr1's I/G bit; MGMT bulk-OUT is the lowest EP).
- Warm path: `mac.is_chip_warm` + `driver._warm_reattach` (bulk-IN smoke test surfaces "please
  replug" when the pipe is wedged).

## Scripts

- `scripts/chips/rtl8192eu/extract_phy_tables.py` — re-extracts the MAC / BB / AGC (std + high-PA)
  / RADIO A / RADIO B tables in `phy_tables.py` from `8192e.c`. Re-run it; do not hand-edit tables.
- `scripts/chips/rtl8192eu/verify_pcap.py` — the byte gate: replays the cold boot against
  `driver_captures/captures_rtl8xxxu/capture-1.pcap` (mainline `rtl8xxxu`, kernel 6.19.14) through
  the shared Realtek engine (`scripts/porting/rtw88_pcap_replay.py`). Seven gates: **FW blob**
  (252 chunks / 32 254 B == `assets/rtl8192eu_nic.bin` payload), **queue-pre-FW** (5 ops),
  **MAC + PHY** (1295 ops), **post-PHY** TX boundary/LLT/quirks (10 ops), **beacon** (5 ops),
  **LCK** (14 ops) and **IQK** (957 ops) — the port's own `mac`/`phy`/`iqk` functions run against a
  `ReplayTransport` serving the recorded chip reads, so every emitted write must match the wire.
  A missing anchor is a failure, not a skip. The gates are seven disjoint anchored regions, not one
  continuous stream: the RX-enable + adaptive-controls run is deliberately **not** gated because it
  writes this port's monitor `REG_RCR`, not the kernel's station RCR (see above).
  Requires `tshark` on `PATH`. Run:
  `uv run python scripts/chips/rtl8192eu/verify_pcap.py`.
- `scripts/chips/rtl8192eu/test_hw_rtl8192eu.py` — on-hardware smoke test (card bound to WinUSB,
  no VM holding it). `--phase scan` (default) runs the real `driver.connect()` cold bring-up then
  hops 1–13 collecting beacons; `--phase open|bringup|warm` isolate one step each; `--phase inject`
  fires unicast MGMT frames at a real AP and counts that AP's ACKs (the single-card proof that TX
  reaches the air), with a nobody-owns-this-MAC control. Needs the card physically replugged for a
  true cold state (a software detach from a VM does not power-cycle).
  Run: `uv run python scripts/chips/rtl8192eu/test_hw_rtl8192eu.py --phase scan`
  or `... --phase inject --count 50`.
- `scripts/chips/rtl8192eu/test_hw_active_monitor.py` — single-card probe for the forged-MAC
  auto-ACK (injects at the spoofed RA and counts the ACKs, with active-monitor-off and bogus-MAC
  controls). The same radio transmits and receives, so a ~0 result is inconclusive; the definitive
  test is `scripts/ack/rx_autoack.py` with a second prober card. Run:
  `uv run python scripts/chips/rtl8192eu/test_hw_active_monitor.py --count 100`.

## Debug log

- 2026-10-17: port written; no on-hardware run yet. The cold chain, 2.4 GHz monitor RX and MGMT
  inject are unit-tested (`tests/chips/rtl8192eu/`, mocked USB).
- 2026-10-17: `verify_pcap.py` added; gate PASSes `capture-1` (FW blob + MAC/PHY + LCK + IQK,
  2518 ops byte-for-byte). Two wire-driven fixes came out of it: the shared replay engine now
  rejects standard `SET_ADDRESS` (bRequest 5, bmRequestType 0x00) that collided with the vendor
  request number and has no `wValue`; and the IQK anchor is the `0x0c50`→`0x0c58`→`0x085c`
  opener triple, not the lone `0x085c` read.
- 2026-10-17: **first on-hardware run — PASS** (`test_hw_rtl8192eu.py --phase scan`, WinUSB, cold
  after a physical replug). `connect()` ran the full cold chain in 13.6 s and came up `online`.
  The port's parsed state agreed with the wire gate: `crystal cap = 0x1f` (same value the capture's
  `REG_MAC_PHY_CTRL` write carried), a real EFUSE MAC `98:48:27:aa:b1:fd`, and non-default per-group
  TX power (CCK `0x26 0x26 0x26 0x28 0x28 0x28`, HT40-1s `0x2a ×4 + 0x00`) — so the EFUSE read and
  TX-power path are exercised, not the `0x22` fallback. A 1–13 @ 0.5 s sweep saw **12 BSSIDs / 92
  beacons**, including the known injection target `c0:06:c3:c9:ec:0b` (`TP-Link_FA80`, ch 4, −35 dBm).
  RX monitor path is therefore live on silicon. Still untested on hardware: `enable_rf`→TX
  (inject), the two RF *transmit* paths, and CCK RSSI table selection (no CCK-modulated frames in
  the sample). Note `driver.is_warm` reads `True` after ANY successful attach (`_finish_attach`
  sets it on both paths) — it means "online", not "took the warm path"; use `mac.is_chip_warm` to
  probe chip state.
- 2026-10-17: **adversarial review → cold-path fidelity pass.** Re-dumping `capture-1` (8160
  control ops) showed `_cold_bring_up` had drifted from `rtl8xxxu_init_device` in four places, and
  the old gates could not see any of them. Fixed: queue pages + DMA lanes + `REG_TRXFF_BNDY+2` now
  run **before** the FW upload (`core.c:3952-3962`); the TX buffer boundary / PBP / auto-LLT /
  gen2-USB-quirks block moved **after** the PHY init (`core.c:4021-4054`); the five-write
  beacon/TSF block (`REG_BEACON_CTRL` … `REG_BEACON_TCFG`, `core.c:4199-4211`) was missing and is
  now written right after `REG_ACKTO`; and the redundant `REG_CR` read-modify-write was dropped —
  `_power_on` already writes `0x6ff`, so the extra op was not on the wire. The queue flags are now
  derived from `REG_NORMAL_SIE_EP_TX` + the bulk-OUT endpoint count; the old hard-coded two-queue
  split emitted `REG_RQPN = 0x80ea0c08`, while the wire says `0x80de0c08` (three queues). Three new
  replay gates (queue-pre-FW, post-PHY, beacon) plus the old four now cover 2538 replayed ops across
  seven disjoint regions; a missing gate anchor is a failure, not a skip.
  EFUSE failure now aborts the bring-up instead of silently using fallback TX power.
- 2026-10-17: **second review pass — correctness fixes.** (1) The cold path no longer tunes a
  channel: `rtl8xxxu_init_device` only sets TX power (`core.c:4242`) and the tune belongs to
  `rtl8xxxu_config` (`core.c:6785-6793`, power *then* radio); `set_channel` now mirrors that order
  and `current_channel` stays `None` until the first tune. (2) `enter_active_monitor` captures
  `REG_MACID` only on the first call, so a second `enter` without an `exit` can no longer make
  `exit` restore the *forged* MAC. (3) `_warm_reattach` now reads the EFUSE too — otherwise the
  `0x22` defaults were re-applied by every `set_channel` on the warm path. The RX-enable +
  adaptive-controls block stays un-gated on purpose: it writes this port's monitor RCR, not the
  kernel's station RCR.
- 2026-10-17: **hardware active monitor ported.** `mac.set_mac_addr` (`core.c:3558-3582`) writes the
  forged MAC to `REG_MACID`; `driver.enter_active_monitor` caches the previous six register bytes and
  `exit_active_monitor` restores them, so the warm path (no EFUSE parse) still restores the burned
  MAC. `FAKE_MAC` is now `SPOOFABLE`, which unblocks EvilTwin. Unit-tested only: the HW auto-ACK of
  a forged RA still needs `scripts/ack/rx_autoack.py --test-card 8192` + a second prober card.
- 2026-10-17: **on-hardware re-run after the fidelity pass — PASS.** `--phase scan` on a cold card ran
  the *reordered* chain end-to-end in 17.8 s: `crystal cap = 0x1f`, EFUSE MAC `98:48:27:aa:b1:fd`,
  real per-group TX power (`0x26…` / `0x2a…`), `current_channel = None` (the radio is now tuned by
  the first `set_channel`, matching `rtl8xxxu_config`), and 13 BSSIDs / 92 beacons over 1–13 with the
  target `TP-Link_FA80` at −27 dBm. So the pre-FW queue block, the post-PHY boundary/LLT block and
  the new beacon block all come up on silicon.
- 2026-10-17: **injection proven on the air.** `test_hw_rtl8192eu.py --phase inject --count 50` armed
  the RX ACK tap, tuned to ch 4 and fired 50 unicast MGMT frames at `c0:06:c3:c9:ec:0b`
  (`TP-Link_FA80`): the AP ACKed **48/50**, while the same frames addressed to a MAC nobody owns drew
  1/50. That is the single-card proof that TX reaches the air and that the ACK tap admits the AP's
  replies. The hardware auto-ACK of a forged MAC stays the one unmeasured link (needs a second radio).
- 2026-10-17: **`test_hw_active_monitor.py` on hardware — inconclusive by construction.** The forged
  MAC armed and read back correctly (`enter`/`exit`/double-arm all restored `REG_MACID`), and all 500
  injected frames transferred, but the ACK counts were 0 for the spoofed MAC, the monitor-off control
  **and** the bogus-MAC control. A single radio cannot receive the ACK it just triggered, so this
  proves nothing about the HW auto-ACK; `scripts/ack/rx_autoack.py` with a second card is the verdict
  instrument. `--phase inject` was added in the same pass to prove TX on the air against a real AP.
