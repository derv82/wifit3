# Wifit3: Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## Verify (offline pcap replay)

### mt76x0u verify_pcap: CHECK D green (2026-07-20); CHECK A-C boot reproduction pending
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
