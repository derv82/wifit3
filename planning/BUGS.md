# Wifit3 — Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## Verify (offline pcap replay)

### mt76x2u verify_pcap — FULLY GREEN 2026-07-20
Both checks pass: CHECK A-C reproduces all **1215 cold-boot ops byte-for-byte** (reset_wlan
through the channel tune + mac_start) and CHECK D is green, against the vendored v6.18 source.
Real port-vs-kernel divergences fixed along the way (cold-boot + FW + MAC init HW re-validated:
RX 16 BSSIDs + auto-ACK 96/100): RTS `_ieee80211_hdrlen` (control frames are 16, not 10);
ROM-patch read gated behind `rom_protect`; WPDMA-before-mac wait order; US_CYC/TXOP relocated;
cached RX_FILTR read; WCID/skey via `write_copy`; full `mt76x2u_mac_stop`; `mac_reset_counters`;
`mac_start` before the tune. Two deliberate divergences are masked (documented): the AC_VO inject
route (CHECK D) and the monitor RX-filter (wifit3 clears DROP_UC_NOME; the kernel's separate
configure_filter is masked + skipped).

Closing the channel tune to green (the last stretch) reconstructed the mac80211 config-callback
flow the pcap runs (`mt76x2u_config` -> `mt76x2u_set_channel`), driving the real driver functions:
config(POWER) does an initial `phy_set_txpower` at mac80211's default channel (5 GHz ch36 for this
5g-injection capture); config(CHANNEL) wraps `phy_set_channel` in a config-time `mt76x2_mac_stop`
+ `mac_resume`. Inputs reverse-engineered from the wire: `dev->txpower_conf = 34` (power_level
17 dBm), `bt_rcal` off EEPROM 0x138 (== 0xff, so MCU_CAL_R is skipped), TSSI on. The earlier
"TX-power computation bug" note was WRONG: `phy_set_txpower` is correct (it RMWs, preserving LIMIT;
my standalone had served the RMW read as 0). The genuine driver fixes were op-structure ones
(same final values): `phy_set_txpower_low` two rmw_field ops for CH_INIT_0/1; `phy_set_band` three
rmw ops; `_adjust_high_lna_gain`/`_adjust_agc_gain` standalone-read-then-rmw; LDPC RX enable moved
before the calibrations. New: `mac.py::mac_stop_config` + `mac_resume`.

Open follow-up (not blocking): `connect()` tunes bare, but the config-callback path stops the MAC
before a re-tune. Wrapping runtime `set_channel` in `mac_stop_config`/`mac_resume` is the leading
hypothesis for the `cmd=31` SWITCH_CHANNEL timeouts seen on runtime re-tunes in ack_lab; HW-check.

### mt76x0u verify_pcap — CHECK D green (2026-07-20); CHECK A-C boot reproduction pending
CHECK D is green: the deliberate OFDM-vs-CCK rate (2.4 GHz inject) and AC_VO-vs-AC_BE route are
masked, mirroring mt76x2u. CHECK A-C is still red at the op#1 prologue. Characterized: the wire
cold-boots read 0x0080 -> reset_wlan (write 0x0080 x2) -> early reads (0x1000/0x0000/0x0024) ->
USB-DMA cfg (0x0238) -> warm-gate read (0x0730 @op#13) -> FW upload. wifit3 reads 0x0080 (matches
op#0) then does its warm-reattach probe (0x0730) straight away, so it is missing the reset_wlan +
early-init block, and `check_boot`'s `_drive` starts at `load_firmware`. The fix is the mt76x2u
playbook: anchor past the warm probe, restructure `_drive` to the wire's reset -> init -> FW order,
and reconcile the post-FW init op-by-op against `data_dumps/mt76-source-v6.18/mt76x0` (a
substantial effort like mt76x2u's, ~1000 ops). No EEPROM off-cursor needed here (0 breq-0x09 reads;
efuse is read via MT_EFUSE_CTRL).
_Location: `chips/mt76x0u`; `scripts/mt76x0u/verify_pcap.py`._
