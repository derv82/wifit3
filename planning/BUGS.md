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
filter + a separate configure_filter, which the gate masks + skips). The remaining CHECK A-C
FRONTIER is a **capture-vs-v6.18 skew, not a port bug**: `set_channel_20mhz` matches the v6.18
source order, but the pcap runs the *wrapped* `mt76x2u_set_channel` (an initial phy_set_txpower
+ config-time `mt76x2_mac_stop` before `phy_set_channel`). Open follow-up: add that wrapper for
full parity (hypothesis: the missing mac_stop-before-retune may also explain the `cmd=31`
SWITCH_CHANNEL timeouts seen during runtime re-tunes in ack_lab).

### mt76x0u verify_pcap red: same treatment pending
`verify_pcap.py mt76x0u` is still red on the analogous divergences (prologue, AC route, plus a
2.4 GHz OFDM-vs-CCK inject-rate difference). Apply the mt76x2u playbook (anchor the prologue,
serve EEPROM off-cursor, mask the deliberate rate/route divergences, fix any real port gaps).
_Location: `chips/mt76x0u`; `scripts/mt76x0u/verify_pcap.py`._
