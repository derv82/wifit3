# Wifit3 — Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## Verify (offline pcap replay)

### mt76x0u / mt76x2u verify_pcap red: pre-existing port-fidelity gaps
`verify_pcap.py mt76x2u` (and `mt76x0u`) fail on three divergences that predate the 2026-07
ACK redesign (git-confirmed: that work touched neither connect/prologue nor `tx.py`'s
endpoint/length logic, so it did not cause these):
- **op#1 prologue**: the driver's ASIC-version / warm-reattach read does not match the
  cold-boot capture's first op. A warm-vs-cold capture question.
- **CHECK D endpoint**: data-null frames inject on EP 0x07 (AC_VO), but the aireplay wire
  used EP 0x04 (AC_BE). `tx.py` hardcodes AC_VO for every frame regardless of AC.
- **CHECK D length**: `fc0=0xb4` frames build a 48-byte descriptor vs the wire's 44.

The one ACK-redesign byte (TXWI `ack_ctl` 0 to 1) is masked behind these; a retry-value
override (the ar9271 / 8821cu trick) does not apply, since none of the three is retry-based.
Note: the port was taken from the Linux kernel mt76, not morrownr's out-of-tree driver
(https://github.com/morrownr/mt76). A re-port from the OOT driver may be the cleaner fix.
_Location: `chips/mt76x2u` + `chips/mt76x0u`; `scripts/mt76x2u/verify_pcap.py` + `mt76x0u`._
