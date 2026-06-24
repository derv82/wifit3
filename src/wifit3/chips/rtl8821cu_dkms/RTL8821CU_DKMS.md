# RTL8821CU (8821cu_dkms)

A self-contained port of the Realtek vendor/DKMS driver (`rtl8821cu-5.12.0.4`, in the capture
bundle). RTL8821C silicon: 1T1R, 2.4 + 5 GHz 802.11ac, HALMAC + PHYDM, Jaguar-2 phystatus,
firmware-based. Its power tables and init are specific enough that it doesn't share a base with
other drivers.

## Status

- Cold init and firmware boot: working on hardware.
- **Bring-up was a coin toss (~80%+ of cold boots came up dead-RX on BOTH bands, fresh plug
  included) — root cause found: the power-sequence `DELAY` command was a no-op, dropping the 1 ms
  LDO settle in the card-enable flow. Fixed in `pwrseq.py`. NOT yet HW-validated** — the test card
  degraded past its replug threshold (~34 soft re-inits) before the fix landed, so a fresh-plug
  `bringup_cointoss.py` run is still owed to confirm the dead-rate collapses. See debug log.
- 5 GHz / 2.4 GHz monitor RX: working *when bring-up succeeds* — the coin toss gated everything, so
  earlier per-band RX findings were confounded by which launches happened to come up alive.
- `verify_pcap`: clean — but BLIND to timing (a DELAY emits no register op), which is exactly how
  the missing settle survived a byte-faithful port. See Gotchas.
- Not done: fresh-plug coin-toss validation, ZeroCD discovery, warm reattach, the
  degrades-after-~8-soft-reinits issue (separate from the coin toss).

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

**`verify_pcap` cannot see timing.** A power-sequence (or PHY-table) `DELAY` emits no register op,
so it is invisible in the capture — a byte-faithful port, and the gate, will happily drop required
settle delays and still PASS. The bring-up coin toss was exactly this. When you hit a coin toss with
*identical* register state between good and dead launches (`bringup_cointoss.py` proves it: same
CR/RCR/RXDMA/filter, different RX outcome), the culprit is a skipped delay/poll, not a missed write.

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

### 2026-06-24 — bring-up coin toss: the power-seq DELAY was a no-op

Bring-up RX was a coin toss — ~80%+ of cold boots came up dead on BOTH bands (a fresh plug too, per
the user), control path fully alive (registers read, FA counters tick) but the RX FIFO never filled.
`bringup_cointoss.py` reproduced it headlessly (e.g. 2 GOOD / 10 DEAD) and gave the decisive clue:
the RX-path register dump is IDENTICAL between good and dead launches (CR=0x6ff, RCR=0x90000001,
RXDMA_STATUS=0, filters all the same). Identical config + different outcome = a timing race, not a
missed register — which is why a "faithful" port that passes `verify_pcap` still failed.

The seam: the byte-gate matches the op SEQUENCE but is blind to delays between ops. Auditing the
bring-up for waits that emit no register op found it — `pwrseq._run_table` treated `_CMD_DELAY` as a
no-op ("replay strips it"), but `CARDEMU_TO_ACT` carries a 1 ms DELAY: the LDO settle after the
0x20[0]=1 power enable. Skip it → proceed before the rail settles → power-on lands marginal at
random. Vendor honors it at `halmac_common_88xx.c:3078` (`PLTFM_DELAY_US`). Fix: sleep offset us/ms;
gate stays green (a sleep is no op on the wire).

NOT HW-validated yet: by the time the bug was found the test card had degraded past its
~8-soft-reinit replug threshold (I was at ~34), so every launch was dead regardless and a 20 s rest
between boots didn't recover it. A fresh-plug `bringup_cointoss.py` run is owed. FW-download polls +
BB/AGC/RF tables were checked for other skipped delays — none. The degrade-after-N-boots behaviour
is a separate, still-open issue (likely its own missing reset on soft re-init).

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
