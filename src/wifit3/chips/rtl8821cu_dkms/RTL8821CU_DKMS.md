# RTL8821CU (8821cu_dkms)

A self-contained port of the Realtek vendor/DKMS driver (`rtl8821cu-5.12.0.4`, in the capture
bundle). RTL8821C silicon: 1T1R, 2.4 + 5 GHz 802.11ac, HALMAC + PHYDM, Jaguar-2 phystatus,
firmware-based. Its power tables and init are specific enough that it doesn't share a base with
other drivers.

## Status

- Cold init and firmware boot: working on hardware.
- 5 GHz monitor RX: working.
- 2.4 GHz monitor RX: working, but only after a warm fix (see Gotchas); not yet re-confirmed on a
  fresh plug.
- `verify_pcap`: clean against the full cold-boot pcap.
- Not done: ZeroCD discovery (below), warm reattach, fresh-plug RX confirmation.

## Gotchas

**The card hides as a CD-ROM.** It enumerates as USB mass-storage ("ZeroCD") and must be
mode-switched to the Wi-Fi PID `0bda:c820` before any driver can bind. A user who plugs it in today
sees a CD-ROM and Wifit3 finds nothing, so the card is unusable end-to-end until the discovery
layer handles the switch. This is a manager-level problem that affects most Realtek USB adapters,
not just this one. The offline port and verify are unaffected — the pcap was captured already in
Wi-Fi mode.

**2.4 GHz RX hangs on one bit: RF18 bit16.** Set, every frame fails CRC; clear, the demod works.
The cold channel tune doesn't clear it, and an ordinary same-band hop can't either — the vendor's
channel-switch path never touches bit16. Only a band switch or an explicit warm rewrite clears it,
which is what `driver._relatch_2g_band` does after cold init. The vendor stack gets away with it
because airodump jumps to 5 GHz immediately, and the first 5→2.4 GHz transition clears the bit as a
side effect.

**This card's 2.4 GHz is genuinely weak**, even under the vendor driver — the kernel's own
fixed-channel capture shows only ~13–21 beacons over 15 s. Judge RX against a strong nearby
reference AP, not against this capture.

## Orientation

Start at `bringup.cold_bringup` — it runs init → power sequence → firmware → MAC → BB → RF in the
kernel's order. Channel tuning is `chan.set_channel` (it only switches bands when the band actually
changes). RSSI is in `rx.decode_rssi`, which parses the jgr2 phystatus format — it's a Jaguar-2
chip, and decoding it as Jaguar-1 was an early mistake.

Names match the vendor C, so grep the bundle's `driver-source/` to cross-reference.

## Scripts

- `verify_pcap.py` — the cold-boot byte gate.
- `band_state_probe.py` — HW RX diagnostic; this is what isolated the RF18 bit16 gate.
- `driver_rx_diag.py` — re-run after a fresh plug to confirm 2.4 GHz comes back.

## Debug log

### 2026-06-23 — 2.4 GHz RX root cause

The cold-boot pcap reproduces byte-for-byte, but on hardware 2.4 GHz RX was dead while 5 GHz
worked. `band_state_probe.py` (cold ch1 → 5 GHz → ch1) pinned it to RF18 bit16, perfectly
correlated with CRC failure across every run. The cold tune runs before RX-enable and the antenna
switch, and leaves the bit stuck set; the channel-switch path only ever clears BIT18/17 and byte0.
The fix replaced an earlier flaky 5 GHz-bounce (`_prime_2g_rx`) with a deterministic,
read-back-verified warm clear. Same session: RSSI had been decoding with a Jaguar-1 borrow;
switched to the real jgr2 format and it now reads sane (−60 to −84 dBm).

Not re-verified after the test card degraded (~8 cold boots without a replug, RF18 writes started
intermittently failing): fresh-plug 2.4 GHz revival, whether the phydm DIG watchdog sagging IGI
hurts beacon rate, and reader-vs-init USB ordering.
