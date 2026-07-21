# Wifit3: Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## mt7921au: 5 GHz cold-boot + TX byte-diverges from the wire

`scripts/verify_pcap.py mt7921au usb_dumps_new2/captures_mt7921u_5g-injection/capture-1.pcap`
REDs on two 5 GHz-only divergences (2.4 GHz on the same capture, and the whole PAU0F 2.4 GHz
cold boot, are byte-exact):

- cold boot: `SET_CHAN_DOMAIN` (MCU `0x4000f`) payload byte 204 is `0x02` in the port vs `0x00`
  on the wire — the port's static world-domain channel table does not match the 5 GHz-configured
  Linux domain.
- TX: a 5 GHz TXD diverges at byte 13 (`20008000` built vs `00000001` wire).

This refutes the `MT7921AU.md` 5 GHz-investigation prediction ("the 5 GHz byte-diff will MATCH,
blame RF"): the port does NOT match the 5 GHz capture. Root cause and fix are open; likely the
per-channel/regdomain path the monitor bring-up omits. Separate from the single-cursor verify_pcap
work that surfaced it.
