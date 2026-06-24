# RTL8188EUS

Cleanroom port of the mainline `rtl8xxxu` driver's 8188e fileops vector (kernel `8188e.c` +
shared `core.c`/`regs.h`). RTL8188EUS silicon: WiFi 4, 2.4 GHz only, 1T1R, firmware-based.
TP-Link TL-WN722N v2/v3 and other low-cost dongles. Not rtw88, not rtl8180/8187 despite the name.

## Status

Feature-complete on hardware (TL-WN722N v2/v3): cold init, FW boot, MAC/PHY/RF, channel tune,
RX, TX inject, EFUSE/TX-power, warm reattach. Both passive 4-way handshake capture and active
PMKID harvest land within seconds live. `verify_pcap rtl8188eus` is byte-exact against all 3
mainline captures (FW · MAC · BB/AGC/RF tables · crystal cap · IQK · LCK).

This is the *mainline-equivalent* port and it's RX-maxed there. The mainline kernel itself tops
out ~80% reception on this card with bad-window collapses; a sibling DKMS re-port
(`chips/rtl8188eus_dkms/`, branch `dkms/8188eu`) exists to win the rest. Only registered for
PID `0x2357:0x010C`; other vendor PIDs share the chip but aren't claimed yet.

## Gotchas

**This card has a weak radio.** RX is bimodal: healthy windows ~8 beacons/s (one-AP wire ceiling
~9.77/s), intermittently collapsing into bad windows where the canary is heard worse than farther
neighbours. The variance is the card, not the port — mainline does it too. Judge against a strong
reference AP.

**Monitor-mode RCR needs all accept-bits, not the kernel's station-mode value.** The mainline
`RCR_MONITOR` constant is the station value, missing `ACCEPT_DATA_FRAME`/`ACCEPT_CTRL_FRAME` —
the kernel toggles those in only via mac80211's `configure_filter` when monitor is requested.
Without them, beacons/probes are visible but the EAPOL re-handshake after a deauth and the AP's
M1 response are filtered out (deauth "looks like it didn't work", PMKID never harvested). Use
`0x7000_7B0F` (all 9 accept-bits + HTC_LOC_CTRL + 3 APPEND). `apply_monitor_rx_filter` must
reassert it on the warm path too — `_warm_reattach` skipping it was the trap.

**`enable_rf` lives in `start`, not `init_device`.** `REG_OFDM0_TRX_PATH_ENABLE` (+ RF_CTRL
re-assert + `REG_TXPAUSE=0`) is the OFDM-to-RX routing write; without it RCR is open but the
chip is deaf — zero bulk-IN URBs. Likewise `enable_cck_ofdm_block` (FPGA0_RF_MODE bits 24/25)
must be set or both baseband blocks stay dark.

**The RX descriptor is 24 bytes, not 16.** `rtl8xxxu_rxdesc16` is 20 B of endian-block bitfields
PLUS a `u32 tsfl` declared *outside* the endian block. Misreading it as 16 offset-shifts every
MPDU by 8.

**RSSI byte offsets are fragile.** `cck_sig_qual_ofdm_pwdb_all` is at byte 4 of the phy_stats,
not 6 — `struct phy_rx_agc_info` is a single u8 (`gain:7, trsw:1`), not 2 bytes, so assuming 2
pushes every later field up by 2. OFDM formula `(pwdb >> 1) - 110`; CCK reads a few dB low
(`rtl8188e_cck_rssi`'s LNA/VGA table not ported) but BSSIDs still parse.

**RF delay opcodes 0xF9..0xFE are sleeps, not SIPI writes.** The RADIO_A init table embeds them;
the kernel's `case` block handles them as `msleep`. SIPI-writing them corrupts PHY state.
8188e is 1T1R: only `REG_FPGA0_XA_LSSI_PARM` (path A) exists, RF data is 20-bit
(`0x000FFFFF` mask), path-B writes are rejected.

**Inter-frame alignment in a bulk-IN URB is `roundup(total, 128)`** — 8188e-specific, vs rtw88's
8-byte.

**TX endpoint convention: lowest-numbered bulk-OUT is the HIGH/MGMT lane.** Hard-picked EP 0x02 =
HIGH (MGMT), EP 0x03 = NORMAL (BE/BK) per the kernel's "lower endpoint = higher priority".

**Without EFUSE TX-power, frames radiate at near-zero.** The AGC registers hold reset defaults
(~zero) until `parse_efuse_8188eu` + `set_tx_power` run; the chip ACKs the URB but a phone 3 m
away hears nothing. EFUSE read must happen with FW running (between `start_firmware` and the MAC
init). Falls back to a hardcoded `0x22` (~17 dBm) per group if the read raises.

**Several silicon-ordering quirks the port must preserve:** write bit 2 of reg `0x0003`
(`SYS_FUNC+1`) before enabling the 8051 or it wedges; call `reset_8051` between FW-DL-ready and
the WINT_INIT_READY poll or the loaded image never runs; flip MAC TX/RX enable only *after*
`REG_TRXFF_BNDY` is set (88E TRXFF HW bug); FW chunk size is hard 196 B.

## Orientation

1T1R 2.4 GHz, firmware 8051, EFUSE-burned MAC + per-channel power. Cold chain is in
`driver._cold_bring_up`: FW (`firmware.download_firmware`/`start_firmware`) → EFUSE
(`efuse.parse_efuse_8188eu`) → MAC (`mac.post_fw_mac_init`) → PHY/RF (`phy.init_phy_bb`,
`phy.init_phy_rf_8188e`) → IQK/LCK (`iqk.py`) → channel (`chan.set_channel_2g_20mhz`) →
`phy.enable_rf`. RX path: `rx.parse_rxdesc16`/`iter_bulk_frames`; TX: `tx.send_mgmt_frame`.
Warm path: `mac.is_chip_warm` + `driver._warm_reattach`. Names match the kernel C — grep
`data_dumps/rtl8xxxu-source-v6.18/8188e.c` (the fileops vector at `8188e.c:1835`) to
cross-reference. The init tables are mechanically extracted from kernel source; re-run the
extractor if it updates.

## Scripts

- `verify_pcap.py` — byte gate against the 3 mainline cold-boot captures.
- `test_hw_rtl8188eus.py` — phased HW test (`--phase fw|mac|phy|channel|beacon|tx|efuse|warm|all`).
- `extract_phy_tables.py` — re-extracts BB/AGC/RF init tables from kernel source.

## Debug log

### 2026-05-18/19 — bring-up

RX came up after three fixes in three iterations: missing `enable_rf` (chip deaf, RCR open but
OFDM not routed), missing `enable_cck_ofdm_block` (baseband off), and the 24-vs-16-byte rxdesc
(`tsfl` outside the endian block, MPDU shifted by 8). 21 distinct beacon BSSIDs on ch1 in 5 s.
RSSI initially all -110: phy_stats byte-offset was off by 2 (`phy_rx_agc_info` is 1 u8). TX
inject worked first-shot on EP 0x02. The real injection blocker was monitor RCR — the mainline
constant is station-mode, so deauths flew but the re-handshake/PMKID M1 was filtered; widening to
all-accept-bits gave instant live handshake capture + PMKID harvest.

### 2026-06-06/07 — equivalence walk + mainline ceiling

Closed the gap between the best-effort minimum-RX port and mainline `rtl8xxxu_init_device`
function-by-function, byte-verified against all 3 captures: crystal cap, IQK (path-A, 270/413
ops — the replay serves the recorded measurement-reads so the ported algorithm reproduces the
recorded writes, disproving the earlier "runtime-computed, can't replay-match" note), LCK.
Fixed-ch1 passive A/B (same card): DKMS vendor 86-89%, mainline kernel 83%, our mainline port
~77% — we match mainline, whose 8188e ceiling is ~80% with min-3 collapses. The remaining
`init_device` tail (post-PHY block, runtime DIG) is TX/housekeeping + environment-dependent and
won't move beacons; the RX win is the DKMS re-port, so this port stopped here.

### 2026-05-25 — cross-driver gap audit

RX poll loop moved off the event loop to the shared `chips/rx_reader.py` RxReaderThread
(beacon rate ~3.5-6.5/s → 5.5-8.0/s; all 3 reconnects yielded a crackable pair). Encryption was
flapping WPA2→"WEP" on a known-WPA2 AP: a dropped/truncated beacon loses the RSN IE, the parser
falls back to the Privacy-bit "WEP" label, and the registry overwrote encryption every beacon.
Fixed cross-driver in `interface._on_frame_parsed` (keep strongest evidence, OPEN<WEP<WPA*);
awaiting HW re-confirm on this card (the weak radio that surfaced it).
