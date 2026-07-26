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

## rtl8821cu: warm-reattach comes up deaf on 2.4 GHz

If the card was last on 5 GHz, a warm bring-up (card left plugged, no cold replug) doesn't reset the
band to 2.4 — 2.4 GHz RX is silent until the channel hopper cycles onto a 2.4 channel. Scanning
self-heals in a few seconds; a Focus view pinned to a 2.4 GHz channel stays deaf (no hop to recover it).
A band switch or a cold replug clears it. Likely the 2.4 GHz band/channel re-tune is on the cold path
only, not the common attach tail (the warm-reattach rule: steady-state config must run on the warm path).

Two fix directions: (a) reset the band / re-tune on warm reattach in the DKMS port, or (b) re-port
8821cu from mainline rtw88 — morrownr (the DKMS maintainer) says the in-kernel rtw88 driver is better
than the DKMS one ([morrownr/8821cu-20210916 readme](https://github.com/morrownr/8821cu-20210916)).
There's currently no mainline 8821cu port in wifit3 (DKMS only), unlike 8822bu.
