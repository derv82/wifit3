# Wifit3 — Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## Verify (offline pcap replay)

### mt76x2u verify_pcap — RESOLVED 2026-07-20 (CHECK D green, CHECK A-C frontier)
CHECK D is green and CHECK A-C reproduces 1091 cold-boot ops byte-for-byte (reset_wlan ->
mac_start), against the vendored v6.18 kernel source. Getting there fixed real port-vs-kernel
divergences (all HW re-validated: RX 16 BSSIDs + auto-ACK 96/100):
- `tx.py::_ieee80211_hdrlen` was 10 for all control frames; RTS/PS-Poll are 16 (no spurious
  hdr-pad). That fixed the 48-vs-44 CHECK D length divergence.
- `firmware.py` ROM-patch read now gated behind `rom_protect` (usb_mcu.c:80); WPDMA-idle poll
  before the post-FW wait_for_mac; US_CYC_CFG + TXOP_CTRL_CFG moved out of `mac_reset` to after
  `init_beacon_config` (usb_init.c:177); RX_FILTR_CFG cached after setaddr (usb_init.c:160);
  WCID/skey cleared via `write_copy` (mt76_wr_copy, single MULTI_WRITE, not per-word); full
  `mt76x2u_mac_stop` ported; `mac_reset_counters` added; `mac_start` reordered before the
  channel tune.
The gate accommodates two deliberate divergences (documented, opt-in): the AC_VO inject route
(CHECK D mask) and the monitor RX-filter (it clears DROP_UC_NOME; the kernel writes the base
filter + a separate configure_filter, which the gate masks + skips).

**The CHECK A-C FRONTIER is at the channel tune, and closing it to full green is blocked by two
separate, deeper issues (analyzed 2026-07-20, wrapper reconstruction attempted then reverted):**
1. *Config-callback wrapper.* The pcap runs `mt76x2u_config` -> `mt76x2u_set_channel`: an initial
   `mt76x2_phy_set_txpower` (op #1219) + `mt76x2_mac_stop` (config-time, mac.c:9) before
   `phy_set_channel`, then `mac_resume` after. wifit3 tunes bare. The wrapper *structure* was
   reproduced (the initial phy_set_txpower matched op #1219; `mac_stop_config` + `mac_resume`
   are ~30-line ports of mt76x2_mac.c:9 + mac.h). This part is portable.
2. *TX-power computation divergence (the real blocker).* wifit3's `phy_set_txpower` writes
   `MT_TX_ALC_CFG_0 = 0x23230000` (CH_INIT 0x23, **LIMIT 0**) but the wire has `0x2f2f1b1b`
   (CH_INIT 0x1b, **LIMIT 0x2f**). It is INSENSITIVE to `txpower_conf` (60..36 all give 0x23), so
   it is a genuine computation difference, not a power-level config. The missing LIMIT=0x2f is a
   likely latent bug in `phy_set_txpower` worth its own investigation (TX still works because the
   `set_txpower_enabled` gate can fall back to the static 0x3a3a3a3a initvals). Fix this first;
   then the wrapper reconstruction can proceed.
Also a runtime hypothesis: the missing config-time `mac_stop` before a re-tune may explain the
`cmd=31` SWITCH_CHANNEL timeouts seen on runtime re-tunes in ack_lab (wrap `set_channel` with
`mac_stop_config`/`mac_resume` and HW-check).

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
